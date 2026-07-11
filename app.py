"""
Cecchino Pro - sistema di trading signal monitoring.

App Flask multi-utente: login con Google, poi Scanner, Portafoglio, Alert e
Storico. Ogni utente vede solo i propri titoli e riceve le mail di alert
al proprio indirizzo Google. Un endpoint /api/cron/tick permette a un
trigger esterno gratuito (es. GitHub Actions) di far girare il controllo
orario anche quando l'app è ospitata su un servizio cloud gratuito che va
in sleep.
"""
import json
import os
import smtplib
import sqlite3
import ssl
import statistics
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import config

DB_PATH = "signals.db"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=config.GOOGLE_CLIENT_ID,
    client_secret=config.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Cache in memoria dell'ultima analisi calcolata per ogni (utente, ticker),
# così le API leggono dati già pronti invece di richiamare Yahoo ad ogni click.
LAST_ANALYSIS = {}
CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            google_sub TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            created DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tickers (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            ticker TEXT,
            qty REAL DEFAULT 0,
            paid REAL DEFAULT 0,
            custom_buy REAL,
            custom_sell REAL,
            active INTEGER DEFAULT 1,
            UNIQUE(user_id, ticker)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            ticker TEXT,
            signal TEXT,
            price REAL,
            score INTEGER,
            rsi REAL,
            reasons TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            ticker TEXT,
            condition TEXT,
            price REAL,
            triggered INTEGER DEFAULT 0,
            created DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()


def seed_default_tickers(conn, user_id):
    """Precompila il portafoglio di un nuovo utente con i titoli di default."""
    for t in config.DEFAULT_TICKERS:
        levels = config.DEFAULT_LEVELS.get(t, {})
        conn.execute(
            "INSERT OR IGNORE INTO tickers (user_id, ticker, qty, paid, custom_buy, custom_sell, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                user_id,
                t,
                levels.get("qty", 0),
                levels.get("paid", 0),
                levels.get("buy"),
                levels.get("sell"),
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Login con Google
# --------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Non autenticato"}), 401
        return f(*args, **kwargs)

    return wrapper


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = google.authorize_access_token()
    userinfo = token.get("userinfo")
    if not userinfo:
        return "Login con Google fallito.", 400

    google_sub = userinfo["sub"]
    email = userinfo["email"]
    name = userinfo.get("name", email)

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (google_sub, email, name) VALUES (?, ?, ?)",
            (google_sub, email, name),
        )
        conn.commit()
        user_id = cur.lastrowid
        seed_default_tickers(conn, user_id)
    else:
        user_id = row["id"]
        conn.execute("UPDATE users SET email = ?, name = ? WHERE id = ?", (email, name, user_id))
        conn.commit()
    conn.close()

    session["user_id"] = user_id
    session["email"] = email
    session["name"] = name
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# --------------------------------------------------------------------------
# Dati di mercato (Yahoo Finance via requests, niente yfinance)
# --------------------------------------------------------------------------
def fetch_yahoo(ticker):
    """Fetch diretto senza yfinance. Prova 2 server, torna None se falliscono entrambi."""
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
            r = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            j = r.json()
            result = j["chart"]["result"][0]
            closes = [c for c in result["indicators"]["quote"][0]["close"] if c]
            if not closes:
                continue
            meta = result["meta"]
            return {
                "closes": closes,
                "price": float(meta.get("regularMarketPrice", closes[-1])),
                "currency": meta.get("currency", "USD"),
                "name": meta.get("shortName", ticker),
            }
        except Exception as e:
            print(f"Yahoo {base} fallito per {ticker}: {e}")
    return None


# --------------------------------------------------------------------------
# Indicatori e logica dei segnali
# --------------------------------------------------------------------------
def compute_rsi(closes, period=14):
    """RSI 14 con smoothing di Wilder."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = statistics.mean(gains[:period])
    avg_loss = statistics.mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_signal(closes, price, custom_buy=None, custom_sell=None):
    """Calcola segnale BUY/HOLD/SELL, score -100/+100 e motivazioni in italiano."""
    score = 0
    reasons = []

    rsi = compute_rsi(closes, 14)
    if rsi < 30:
        score += 35
        reasons.append(f"RSI {rsi:.0f} — fortemente ipervenduto")
    elif rsi < 40:
        score += 20
        reasons.append(f"RSI {rsi:.0f} — ipervenduto")
    elif rsi > 75:
        score -= 35
        reasons.append(f"RSI {rsi:.0f} — fortemente ipercomprato")
    elif rsi > 65:
        score -= 20
        reasons.append(f"RSI {rsi:.0f} — ipercomprato")

    ma50 = statistics.mean(closes[-50:])
    ma200 = statistics.mean(closes[-200:]) if len(closes) >= 200 else statistics.mean(closes)

    if price > ma50:
        score += 15
        reasons.append(f"Sopra MA50 ({ma50:.0f})")
    else:
        score -= 15
        reasons.append(f"Sotto MA50 ({ma50:.0f})")

    if price > ma200:
        score += 10
    else:
        score -= 10

    high52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    low52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    dist_from_high = (price - high52) / high52 * 100

    if dist_from_high < -25:
        score += 25
        reasons.append(f"{dist_from_high:.0f}% dal massimo 52W — sconto forte")
    elif dist_from_high < -15:
        score += 15
        reasons.append(f"{dist_from_high:.0f}% dal massimo")
    elif dist_from_high > -3:
        score -= 25
        reasons.append("Vicino al massimo — rischio distribuzione")

    day_chg = 0.0
    if len(closes) >= 2 and closes[-2]:
        day_chg = (closes[-1] - closes[-2]) / closes[-2] * 100
        if day_chg > 12:
            score -= 25
            reasons.append(f"Spike +{day_chg:.1f}% oggi — considera vendita parziale")
        elif day_chg < -10:
            score += 20
            reasons.append(f"Calo {day_chg:.1f}% oggi — possibile dip da comprare")

    if custom_buy and price <= custom_buy:
        score += 45
        reasons.append(f"⭐ SOTTO LA TUA SOGLIA DI ACQUISTO ({custom_buy})")
    if custom_sell and price >= custom_sell:
        score -= 45
        reasons.append(f"⭐ SOPRA LA TUA SOGLIA DI VENDITA ({custom_sell})")

    score = max(-100, min(100, score))

    if score >= 25:
        signal, color = "BUY", "#22c55e"
    elif score <= -25:
        signal, color = "SELL", "#ef4444"
    else:
        signal, color = "HOLD", "#eab308"

    return {
        "signal": signal,
        "color": color,
        "score": score,
        "reasons": reasons,
        "rsi": round(rsi, 1),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "high52": round(high52, 2),
        "low52": round(low52, 2),
        "dist_high52": round(dist_from_high, 1),
        "dist_low52": round((price - low52) / low52 * 100, 1) if low52 else 0.0,
        "day_chg": round(day_chg, 1),
    }


def analyze_ticker(ticker, custom_buy=None, custom_sell=None):
    """Recupera i dati e calcola il segnale per un ticker. Non solleva mai eccezioni."""
    try:
        data = fetch_yahoo(ticker)
        if not data or len(data["closes"]) < 2:
            return {"ticker": ticker, "error": f"Impossibile recuperare dati per {ticker}"}

        sig = compute_signal(data["closes"], data["price"], custom_buy, custom_sell)
        result = {
            "ticker": ticker,
            "name": data["name"],
            "currency": data["currency"],
            "price": round(data["price"], 2),
            "updated": datetime.now().isoformat(timespec="seconds"),
            **sig,
        }
        return result
    except Exception as e:
        print(f"Errore analisi {ticker}: {e}")
        return {"ticker": ticker, "error": str(e)}


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def send_mail(to_email, subject, body):
    if not config.EMAIL_FROM or not config.EMAIL_PASSWORD:
        print(f"Mail non inviata (credenziali mittente mancanti): {subject}")
        return
    if not to_email:
        print(f"Mail non inviata (nessun destinatario): {subject}")
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = config.EMAIL_FROM
        msg["To"] = to_email
        msg.set_content(body)

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(config.EMAIL_FROM, config.EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"Errore invio mail: {e}")


def notify_signal_change(to_email, ticker, old_signal, new_result):
    subject = f"🎯 CECCHINO: {ticker} {old_signal} → {new_result['signal']}"
    reasons_txt = "\n".join(f"• {r}" for r in new_result["reasons"])
    body = (
        f"Ticker: {ticker}\n"
        f"Vecchio segnale: {old_signal}\n"
        f"Nuovo segnale: {new_result['signal']}\n"
        f"Prezzo: {new_result['price']} {new_result['currency']}\n"
        f"RSI: {new_result['rsi']}\n"
        f"Score: {new_result['score']}\n"
        f"Motivazioni:\n{reasons_txt}\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    send_mail(to_email, subject, body)


def notify_alert(to_email, ticker, condition, threshold, price):
    label = "sopra" if condition == "above" else "sotto"
    subject = f"🔔 CECCHINO ALERT: {ticker} {label} {threshold}"
    body = (
        f"Ticker: {ticker}\n"
        f"Condizione: prezzo {label} {threshold}\n"
        f"Prezzo attuale: {price}\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    send_mail(to_email, subject, body)


# --------------------------------------------------------------------------
# Storico segnali + Alert
# --------------------------------------------------------------------------
def record_signal_if_changed(conn, user_id, to_email, ticker, result):
    """Se il segnale è cambiato rispetto all'ultimo registrato, lo salva e invia mail."""
    last = conn.execute(
        "SELECT signal FROM signals WHERE user_id = ? AND ticker = ? ORDER BY timestamp DESC LIMIT 1",
        (user_id, ticker),
    ).fetchone()
    old_signal = last["signal"] if last else None

    if old_signal != result["signal"]:
        conn.execute(
            "INSERT INTO signals (user_id, ticker, signal, price, score, rsi, reasons) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                ticker,
                result["signal"],
                result["price"],
                result["score"],
                result["rsi"],
                json.dumps(result["reasons"], ensure_ascii=False),
            ),
        )
        conn.commit()
        if old_signal is not None:
            notify_signal_change(to_email, ticker, old_signal, result)


def check_alerts(conn, user_id, to_email, ticker, price):
    rows = conn.execute(
        "SELECT * FROM alerts WHERE user_id = ? AND ticker = ? AND triggered = 0",
        (user_id, ticker),
    ).fetchall()
    for a in rows:
        hit = (a["condition"] == "above" and price >= a["price"]) or (
            a["condition"] == "below" and price <= a["price"]
        )
        if hit:
            conn.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (a["id"],))
            conn.commit()
            notify_alert(to_email, ticker, a["condition"], a["price"], price)


def analyze_and_store(user_id, to_email, ticker, custom_buy, custom_sell):
    """Analizza un ticker del portafoglio di un utente, aggiorna cache, storico ed alert."""
    result = analyze_ticker(ticker, custom_buy, custom_sell)
    with CACHE_LOCK:
        LAST_ANALYSIS[(user_id, ticker)] = result

    if "error" in result:
        return result

    conn = get_db()
    try:
        record_signal_if_changed(conn, user_id, to_email, ticker, result)
        check_alerts(conn, user_id, to_email, ticker, result["price"])
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------
# Monitor (ogni ora via thread locale, oppure via /api/cron/tick esterno)
# --------------------------------------------------------------------------
def refresh_all_portfolio():
    """Aggiorna i titoli attivi di TUTTI gli utenti. Usato dal thread e dal cron esterno."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.*, u.email AS user_email FROM tickers t "
            "JOIN users u ON u.id = t.user_id WHERE t.active = 1"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            analyze_and_store(
                row["user_id"], row["user_email"], row["ticker"], row["custom_buy"], row["custom_sell"]
            )
        except Exception as e:
            print(f"Errore aggiornamento {row['ticker']} (user {row['user_id']}): {e}")


def refresh_user_portfolio(user_id):
    """Aggiorna solo i titoli attivi di UN utente (usato dal pulsante manuale)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT t.*, u.email AS user_email FROM tickers t "
            "JOIN users u ON u.id = t.user_id WHERE t.active = 1 AND t.user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            analyze_and_store(
                row["user_id"], row["user_email"], row["ticker"], row["custom_buy"], row["custom_sell"]
            )
        except Exception as e:
            print(f"Errore aggiornamento {row['ticker']} (user {row['user_id']}): {e}")


def monitor_loop():
    """Thread di riserva per esecuzioni locali/24-7 reali (es. Raspberry Pi).
    Su hosting cloud gratuito che va in sleep, usa /api/cron/tick invece."""
    time.sleep(10)  # Attende l'avvio di Flask
    while True:
        try:
            refresh_all_portfolio()
        except Exception as e:
            print(f"Errore monitor (riprova in 5 min): {e}")
            time.sleep(300)
            continue
        time.sleep(3600)


_monitor_started = False


def start_background_monitor():
    global _monitor_started
    with CACHE_LOCK:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=monitor_loop, daemon=True).start()


# --------------------------------------------------------------------------
# API - Cron esterno (GitHub Actions o altro, gratuito, per il tick orario)
# --------------------------------------------------------------------------
@app.route("/api/cron/tick", methods=["POST"])
def api_cron_tick():
    secret = request.headers.get("X-Cron-Secret", "")
    if not config.CRON_SECRET or secret != config.CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    refresh_all_portfolio()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API - Portafoglio
# --------------------------------------------------------------------------
@app.route("/api/portfolio", methods=["GET"])
@login_required
def api_portfolio_list():
    user_id = session["user_id"]
    to_email = session["email"]
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tickers WHERE user_id = ? AND active = 1 ORDER BY ticker", (user_id,)
    ).fetchall()
    conn.close()

    out = []
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get((user_id, row["ticker"]))
        if cached is None:
            cached = analyze_and_store(user_id, to_email, row["ticker"], row["custom_buy"], row["custom_sell"])

        item = dict(row)
        item["analysis"] = cached
        if cached and "error" not in cached and row["qty"]:
            pnl_eur = (cached["price"] - (row["paid"] / row["qty"] if row["qty"] else 0)) * row["qty"]
            cost = row["paid"]
            pnl_pct = ((cached["price"] * row["qty"] - cost) / cost * 100) if cost else 0
            item["pnl_eur"] = round(pnl_eur, 2)
            item["pnl_pct"] = round(pnl_pct, 2)
        else:
            item["pnl_eur"] = None
            item["pnl_pct"] = None
        out.append(item)
    return jsonify(out)


@app.route("/api/portfolio", methods=["POST"])
@login_required
def api_portfolio_add():
    user_id = session["user_id"]
    to_email = session["email"]
    data = request.get_json(force=True)
    ticker = (data.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "Ticker mancante"}), 400

    qty = float(data.get("qty") or 0)
    paid = float(data.get("paid") or 0)
    custom_buy = data.get("custom_buy")
    custom_sell = data.get("custom_sell")
    custom_buy = float(custom_buy) if custom_buy not in (None, "") else None
    custom_sell = float(custom_sell) if custom_sell not in (None, "") else None

    conn = get_db()
    conn.execute(
        """
        INSERT INTO tickers (user_id, ticker, qty, paid, custom_buy, custom_sell, active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id, ticker) DO UPDATE SET
            qty = excluded.qty,
            paid = excluded.paid,
            custom_buy = excluded.custom_buy,
            custom_sell = excluded.custom_sell,
            active = 1
        """,
        (user_id, ticker, qty, paid, custom_buy, custom_sell),
    )
    conn.commit()
    conn.close()

    result = analyze_and_store(user_id, to_email, ticker, custom_buy, custom_sell)
    return jsonify(result)


@app.route("/api/portfolio/<ticker>", methods=["DELETE"])
@login_required
def api_portfolio_remove(ticker):
    user_id = session["user_id"]
    ticker = ticker.strip().upper()
    conn = get_db()
    conn.execute("UPDATE tickers SET active = 0 WHERE user_id = ? AND ticker = ?", (user_id, ticker))
    conn.commit()
    conn.close()
    with CACHE_LOCK:
        LAST_ANALYSIS.pop((user_id, ticker), None)
    return jsonify({"ok": True})


@app.route("/api/portfolio/refresh", methods=["POST"])
@login_required
def api_portfolio_refresh():
    refresh_user_portfolio(session["user_id"])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API - Scanner
# --------------------------------------------------------------------------
@app.route("/api/scan/<ticker>", methods=["GET"])
@login_required
def api_scan(ticker):
    user_id = session["user_id"]
    ticker = ticker.strip().upper()
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM tickers WHERE user_id = ? AND ticker = ? AND active = 1", (user_id, ticker)
    ).fetchone()
    conn.close()

    custom_buy = row["custom_buy"] if row else None
    custom_sell = row["custom_sell"] if row else None
    result = analyze_ticker(ticker, custom_buy, custom_sell)
    return jsonify(result)


# --------------------------------------------------------------------------
# API - Alert
# --------------------------------------------------------------------------
@app.route("/api/alerts", methods=["GET"])
@login_required
def api_alerts_list():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM alerts WHERE user_id = ? ORDER BY created DESC", (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts", methods=["POST"])
@login_required
def api_alerts_add():
    data = request.get_json(force=True)
    ticker = (data.get("ticker") or "").strip().upper()
    condition = data.get("condition")
    price = data.get("price")

    if not ticker or condition not in ("above", "below") or price in (None, ""):
        return jsonify({"error": "Dati alert non validi"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO alerts (user_id, ticker, condition, price, triggered) VALUES (?, ?, ?, ?, 0)",
        (session["user_id"], ticker, condition, float(price)),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@login_required
def api_alerts_remove(alert_id):
    conn = get_db()
    conn.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, session["user_id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API - Storico
# --------------------------------------------------------------------------
@app.route("/api/history", methods=["GET"])
@login_required
def api_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM signals WHERE user_id = ? ORDER BY timestamp DESC LIMIT 200",
        (session["user_id"],),
    ).fetchall()
    conn.close()

    history = []
    for r in rows:
        item = dict(r)
        try:
            item["reasons"] = json.loads(item["reasons"]) if item["reasons"] else []
        except Exception:
            item["reasons"] = []
        history.append(item)

    # Statistiche semplici: per ogni cambio BUY->SELL o SELL->BUY, verifica se
    # il prezzo si è poi mosso nella direzione attesa rispetto al segnale successivo sullo stesso ticker.
    stats = {"total_changes": len(history), "buy_to_sell": 0, "sell_to_buy": 0}
    by_ticker = {}
    for r in reversed(rows):  # ordine cronologico
        prev = by_ticker.get(r["ticker"])
        if prev == "BUY" and r["signal"] == "SELL":
            stats["buy_to_sell"] += 1
        elif prev == "SELL" and r["signal"] == "BUY":
            stats["sell_to_buy"] += 1
        by_ticker[r["ticker"]] = r["signal"]

    return jsonify({"history": history, "stats": stats})


# --------------------------------------------------------------------------
# Pagina web
# --------------------------------------------------------------------------
@app.route("/")
def index():
    if "user_id" not in session:
        return render_template_string(LOGIN_HTML)
    return render_template_string(INDEX_HTML, user_email=session.get("email"), user_name=session.get("name"))


LOGIN_HTML = r"""
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Cecchino Pro</title>
<style>
body {
  margin: 0;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0a0e14;
  color: #e8eef5;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.box { max-width: 360px; text-align: center; padding: 24px; }
h1 { font-size: 24px; margin-bottom: 8px; }
p { color: #8a97a8; font-size: 14px; line-height: 1.5; }
a.google-btn {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin-top: 24px;
  background: #3b82f6;
  color: white;
  padding: 12px 22px;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 600;
  font-size: 15px;
}
</style>
</head>
<body>
  <div class="box">
    <h1>🎯 Cecchino Pro</h1>
    <p>Accedi con il tuo account Google per gestire il portafoglio e ricevere via mail gli alert BUY/SELL sui tuoi titoli.</p>
    <a class="google-btn" href="/login">Accedi con Google</a>
  </div>
</body>
</html>
"""

INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Cecchino Pro</title>
<style>
:root {
  --bg: #0a0e14;
  --card: #141a24;
  --card2: #1c2531;
  --border: #26303d;
  --text: #e8eef5;
  --dim: #8a97a8;
  --buy: #22c55e;
  --hold: #eab308;
  --sell: #ef4444;
  --blue: #3b82f6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
#app {
  max-width: 700px;
  margin: 0 auto;
  padding: 14px 14px 84px 14px;
}
.topbar { display: flex; justify-content: space-between; align-items: center; margin: 6px 0 16px 0; }
h1 { font-size: 20px; margin: 0; }
.user-info { font-size: 12px; color: var(--dim); text-align: right; }
.user-info a { color: var(--dim); }
.tab-view { display: none; }
.tab-view.active { display: block; }

.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  margin-bottom: 12px;
  position: relative;
  overflow: hidden;
}
.card.stripe { padding-left: 18px; }
.card.stripe::before {
  content: "";
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 5px;
}
.card.stripe.BUY::before { background: var(--buy); }
.card.stripe.HOLD::before { background: var(--hold); }
.card.stripe.SELL::before { background: var(--sell); }

.row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.dim { color: var(--dim); font-size: 13px; }
.ticker-name { font-weight: 700; font-size: 17px; }

.pill {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 13px;
}
.pill.BUY { background: rgba(34,197,94,0.18); color: var(--buy); }
.pill.HOLD { background: rgba(234,179,8,0.18); color: var(--hold); }
.pill.SELL { background: rgba(239,68,68,0.18); color: var(--sell); }

.metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-top: 12px;
}
.metric { background: var(--card2); border-radius: 8px; padding: 8px; text-align: center; }
.metric .val { font-weight: 700; font-size: 14px; }
.metric .lbl { color: var(--dim); font-size: 11px; margin-top: 2px; }

.reasons { margin-top: 10px; font-size: 13px; color: var(--text); line-height: 1.5; }
.reasons div { margin-bottom: 2px; }

input, select, button {
  font-size: 15px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--card2);
  color: var(--text);
  padding: 10px;
}
input::placeholder { color: var(--dim); }
button {
  background: var(--blue);
  border: none;
  color: white;
  font-weight: 600;
  cursor: pointer;
}
button.secondary { background: var(--card2); color: var(--text); border: 1px solid var(--border); }
button.danger { background: rgba(239,68,68,0.18); color: var(--sell); border: 1px solid var(--sell); }

.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
.form-grid input { width: 100%; }
.full { grid-column: 1 / -1; }

nav.bottom {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  background: var(--card);
  border-top: 1px solid var(--border);
  display: flex;
}
nav.bottom button {
  flex: 1;
  background: transparent;
  border-radius: 0;
  color: var(--dim);
  padding: 12px 4px;
  font-size: 12px;
  font-weight: 600;
}
nav.bottom button.active { color: var(--blue); }

.pnl-pos { color: var(--buy); }
.pnl-neg { color: var(--sell); }
.spinner { color: var(--dim); font-size: 13px; }
</style>
</head>
<body>
<div id="app">
  <div class="topbar">
    <h1>🎯 Cecchino Pro</h1>
    <div class="user-info">{{ user_name }}<br>{{ user_email }} · <a href="/logout">Esci</a></div>
  </div>

  <!-- SCANNER -->
  <div class="tab-view active" id="tab-scanner">
    <div class="card">
      <div class="row">
        <input id="scan-input" placeholder="Ticker (es. MU, ASML)" style="flex:1" autocapitalize="characters">
        <button onclick="scanTicker()">Analizza</button>
      </div>
    </div>
    <div id="scan-result"></div>
  </div>

  <!-- PORTAFOGLIO -->
  <div class="tab-view" id="tab-portfolio">
    <div class="row" style="margin-bottom:10px">
      <button class="secondary" onclick="refreshPortfolio()">🔄 Aggiorna prezzi</button>
      <button onclick="toggleAddForm()">+ Aggiungi</button>
    </div>
    <div class="card" id="add-form" style="display:none">
      <div class="form-grid">
        <input id="pf-ticker" placeholder="Ticker" class="full">
        <input id="pf-qty" placeholder="Quantità" type="number" step="any">
        <input id="pf-paid" placeholder="Totale pagato €" type="number" step="any">
        <input id="pf-buy" placeholder="Soglia acquisto" type="number" step="any">
        <input id="pf-sell" placeholder="Soglia vendita" type="number" step="any">
        <button class="full" onclick="addToPortfolio()">Salva</button>
      </div>
    </div>
    <div id="portfolio-list"></div>
  </div>

  <!-- ALERT -->
  <div class="tab-view" id="tab-alerts">
    <div class="card">
      <div class="form-grid">
        <input id="al-ticker" placeholder="Ticker" class="full">
        <select id="al-cond">
          <option value="below">Sotto</option>
          <option value="above">Sopra</option>
        </select>
        <input id="al-price" placeholder="Prezzo soglia" type="number" step="any">
        <button class="full" onclick="addAlert()">Aggiungi Alert</button>
      </div>
    </div>
    <div id="alerts-list"></div>
  </div>

  <!-- STORICO -->
  <div class="tab-view" id="tab-history">
    <div class="card" id="history-stats"></div>
    <div id="history-list"></div>
  </div>
</div>

<nav class="bottom">
  <button id="nav-scanner" class="active" onclick="showTab('scanner')">🔍 Scanner</button>
  <button id="nav-portfolio" onclick="showTab('portfolio')">💼 Portafoglio</button>
  <button id="nav-alerts" onclick="showTab('alerts')">🔔 Alert</button>
  <button id="nav-history" onclick="showTab('history')">📜 Storico</button>
</nav>

<script>
function showTab(name) {
  document.querySelectorAll('.tab-view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.bottom button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'portfolio') loadPortfolio();
  if (name === 'alerts') loadAlerts();
  if (name === 'history') loadHistory();
}

function renderAnalysisCard(a, extraButtons) {
  if (a.error) {
    return `<div class="card"><div class="row"><b>${a.ticker}</b><span class="dim">${a.error}</span></div></div>`;
  }
  const metrics = `
    <div class="metrics">
      <div class="metric"><div class="val">${a.rsi}</div><div class="lbl">RSI 14</div></div>
      <div class="metric"><div class="val">${a.ma50}</div><div class="lbl">MA50</div></div>
      <div class="metric"><div class="val">${a.score}</div><div class="lbl">Score</div></div>
      <div class="metric"><div class="val">${a.dist_high52}%</div><div class="lbl">da Max 52W</div></div>
      <div class="metric"><div class="val">${a.dist_low52}%</div><div class="lbl">da Min 52W</div></div>
      <div class="metric"><div class="val">${a.updated.slice(11,16)}</div><div class="lbl">Aggiornato</div></div>
    </div>`;
  const reasons = `<div class="reasons">${a.reasons.map(r => `<div>• ${r}</div>`).join('')}</div>`;
  return `
    <div class="card stripe ${a.signal}">
      <div class="row">
        <div>
          <div class="ticker-name">${a.ticker} <span class="dim">${a.name || ''}</span></div>
          <div class="dim">${a.price} ${a.currency} (${a.day_chg >= 0 ? '+' : ''}${a.day_chg}%)</div>
        </div>
        <span class="pill ${a.signal}">${a.signal}</span>
      </div>
      ${metrics}
      ${reasons}
      ${extraButtons || ''}
    </div>`;
}

async function apiFetch(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.reload();
    throw new Error('Non autenticato');
  }
  return res;
}

async function scanTicker() {
  const ticker = document.getElementById('scan-input').value.trim().toUpperCase();
  if (!ticker) return;
  document.getElementById('scan-result').innerHTML = '<div class="spinner">Analisi in corso…</div>';
  const res = await apiFetch(`/api/scan/${ticker}`);
  const a = await res.json();
  const btn = a.error ? '' : `<button style="margin-top:10px" onclick="quickAdd('${a.ticker}', ${a.price})">+ Aggiungi al portafoglio</button>`;
  document.getElementById('scan-result').innerHTML = renderAnalysisCard(a, btn);
}

function quickAdd(ticker, price) {
  showTab('portfolio');
  toggleAddForm(true);
  document.getElementById('pf-ticker').value = ticker;
}

function toggleAddForm(forceOpen) {
  const el = document.getElementById('add-form');
  el.style.display = (forceOpen || el.style.display === 'none') ? 'block' : 'none';
}

async function loadPortfolio() {
  document.getElementById('portfolio-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await apiFetch('/api/portfolio');
  const items = await res.json();
  renderPortfolioList(items);
}

function renderPortfolioList(items) {
  const container = document.getElementById('portfolio-list');
  if (items.length === 0) {
    container.innerHTML = '<div class="dim">Nessun titolo in portafoglio.</div>';
    return;
  }
  container.innerHTML = items.map(item => {
    const a = item.analysis || {};
    let pnlHtml = '';
    if (item.pnl_eur !== null && item.pnl_eur !== undefined) {
      const cls = item.pnl_eur >= 0 ? 'pnl-pos' : 'pnl-neg';
      pnlHtml = `<div class="dim">Qty: ${item.qty} · Pagato: €${item.paid} · P&amp;L: <span class="${cls}">${item.pnl_eur >= 0 ? '+' : ''}${item.pnl_eur}€ (${item.pnl_pct}%)</span></div>`;
    }
    const extra = pnlHtml + `<button class="danger" style="margin-top:10px" onclick="removeFromPortfolio('${item.ticker}')">Rimuovi</button>`;
    return renderAnalysisCard(a.error ? {ticker: item.ticker, error: a.error} : a, extra);
  }).join('');
}

async function addToPortfolio() {
  const body = {
    ticker: document.getElementById('pf-ticker').value,
    qty: document.getElementById('pf-qty').value,
    paid: document.getElementById('pf-paid').value,
    custom_buy: document.getElementById('pf-buy').value,
    custom_sell: document.getElementById('pf-sell').value,
  };
  if (!body.ticker) return;
  await apiFetch('/api/portfolio', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  document.getElementById('add-form').style.display = 'none';
  ['pf-ticker','pf-qty','pf-paid','pf-buy','pf-sell'].forEach(id => document.getElementById(id).value = '');
  loadPortfolio();
}

async function removeFromPortfolio(ticker) {
  await apiFetch(`/api/portfolio/${ticker}`, { method: 'DELETE' });
  loadPortfolio();
}

async function refreshPortfolio() {
  document.getElementById('portfolio-list').innerHTML = '<div class="spinner">Aggiornamento in corso…</div>';
  await apiFetch('/api/portfolio/refresh', { method: 'POST' });
  loadPortfolio();
}

async function loadAlerts() {
  document.getElementById('alerts-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await apiFetch('/api/alerts');
  const alerts = await res.json();
  if (alerts.length === 0) {
    document.getElementById('alerts-list').innerHTML = '<div class="dim">Nessun alert configurato.</div>';
    return;
  }
  document.getElementById('alerts-list').innerHTML = alerts.map(a => `
    <div class="card">
      <div class="row">
        <div>
          <b>${a.ticker}</b>
          <span class="dim">${a.condition === 'above' ? 'sopra' : 'sotto'} ${a.price}</span>
          ${a.triggered ? '<span class="pill HOLD">scattato</span>' : ''}
        </div>
        <button class="danger" onclick="removeAlert(${a.id})">Rimuovi</button>
      </div>
    </div>`).join('');
}

async function addAlert() {
  const body = {
    ticker: document.getElementById('al-ticker').value,
    condition: document.getElementById('al-cond').value,
    price: document.getElementById('al-price').value,
  };
  if (!body.ticker || !body.price) return;
  await apiFetch('/api/alerts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  document.getElementById('al-ticker').value = '';
  document.getElementById('al-price').value = '';
  loadAlerts();
}

async function removeAlert(id) {
  await apiFetch(`/api/alerts/${id}`, { method: 'DELETE' });
  loadAlerts();
}

async function loadHistory() {
  document.getElementById('history-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await apiFetch('/api/history');
  const data = await res.json();
  document.getElementById('history-stats').innerHTML = `
    <div class="row"><span>Cambi totali</span><b>${data.stats.total_changes}</b></div>
    <div class="row"><span>BUY → SELL</span><b>${data.stats.buy_to_sell}</b></div>
    <div class="row"><span>SELL → BUY</span><b>${data.stats.sell_to_buy}</b></div>`;
  if (data.history.length === 0) {
    document.getElementById('history-list').innerHTML = '<div class="dim">Nessun cambio di segnale ancora registrato.</div>';
    return;
  }
  document.getElementById('history-list').innerHTML = data.history.map(h => `
    <div class="card stripe ${h.signal}">
      <div class="row">
        <div>
          <b>${h.ticker}</b> <span class="pill ${h.signal}">${h.signal}</span>
          <div class="dim">${h.price} · RSI ${h.rsi} · score ${h.score}</div>
        </div>
        <span class="dim">${h.timestamp}</span>
      </div>
      <div class="reasons">${h.reasons.map(r => `<div>• ${r}</div>`).join('')}</div>
    </div>`).join('');
}
</script>
</body>
</html>
"""


# Inizializza il DB e il thread di riserva ad ogni import del modulo, cosi
# funziona sia con "python app.py" sia con un application server come
# gunicorn (che importa "app" senza eseguire il blocco __main__).
init_db()
start_background_monitor()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
