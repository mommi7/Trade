"""
Cecchino Pro - sistema di trading signal monitoring.

App Flask semplice: alla prima apertura chiede solo l'indirizzo email a cui
mandare gli alert (nessun login Google, nessuna configurazione OAuth).
Poi Scanner, Portafoglio, Alert e Storico. Un thread in background e un
endpoint /api/cron/tick (per trigger esterni gratuiti come GitHub Actions)
analizzano i titoli ogni ora e mandano una mail quando un segnale cambia o
un alert scatta.
"""
import json
import os
import re
import smtplib
import sqlite3
import ssl
import statistics
import threading
import time
from datetime import datetime
from email.message import EmailMessage

import requests
from flask import Flask, jsonify, render_template_string, request

import config

DB_PATH = "signals.db"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)

# Cache in memoria dell'ultima analisi calcolata per ogni ticker attivo,
# così le API leggono dati già pronti invece di richiamare Yahoo ad ogni click.
LAST_ANALYSIS = {}
CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS tickers (
            id INTEGER PRIMARY KEY,
            ticker TEXT UNIQUE,
            qty REAL DEFAULT 0,
            paid REAL DEFAULT 0,
            custom_buy REAL,
            custom_sell REAL,
            active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY,
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
            ticker TEXT,
            condition TEXT,
            price REAL,
            triggered INTEGER DEFAULT 0,
            created DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()

    # Seed dei ticker di default solo se la tabella è vuota (prima esecuzione).
    row = conn.execute("SELECT COUNT(*) AS n FROM tickers").fetchone()
    if row["n"] == 0:
        for t in config.DEFAULT_TICKERS:
            levels = config.DEFAULT_LEVELS.get(t, {})
            conn.execute(
                "INSERT INTO tickers (ticker, qty, paid, custom_buy, custom_sell, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (
                    t,
                    levels.get("qty", 0),
                    levels.get("paid", 0),
                    levels.get("buy"),
                    levels.get("sell"),
                ),
            )
        conn.commit()

    # Migrazione: il ticker corretto di Taiwan Semiconductor è "TSM", non
    # "TSMC" (mai stato un simbolo valido su nessun mercato).
    conn.execute(
        "UPDATE tickers SET ticker = 'TSM' WHERE ticker = 'TSMC' "
        "AND NOT EXISTS (SELECT 1 FROM tickers WHERE ticker = 'TSM')"
    )
    conn.commit()
    conn.close()


def get_alert_email():
    conn = get_db()
    row = conn.execute("SELECT email FROM settings WHERE id = 1").fetchone()
    conn.close()
    return row["email"] if row else None


def set_alert_email(email):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (id, email) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET email = excluded.email",
        (email,),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Impostazioni - indirizzo email per gli alert
# --------------------------------------------------------------------------
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({"email": get_alert_email()})


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Email non valida"}), 400
    set_alert_email(email)
    return jsonify({"ok": True, "email": email})


# --------------------------------------------------------------------------
# Dati di mercato (Yahoo Finance via requests, con fallback su Stooq)
# --------------------------------------------------------------------------
# Molti hosting cloud gratuiti (Render, Railway, ecc.) condividono pool di IP
# che Yahoo Finance a volte blocca o limita in modo aggressivo (429/999),
# mentre in locale/Raspberry Pi funziona quasi sempre. Per non lasciare
# l'app rotta in quel caso, si tiene una sessione con cookie "scaldati" e,
# se Yahoo fallisce comunque, si prova Stooq come seconda fonte gratuita.
YAHOO_SESSION = requests.Session()
YAHOO_SESSION.headers.update(
    {
        "User-Agent": YAHOO_HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    }
)
_yahoo_warmed = False


def _warm_yahoo_session():
    """Visita la home Yahoo una volta per ottenere i cookie di consenso;
    riduce (non elimina) i blocchi 429 dai datacenter cloud."""
    global _yahoo_warmed
    if _yahoo_warmed:
        return
    try:
        YAHOO_SESSION.get("https://fc.yahoo.com", timeout=8)
    except Exception:
        pass
    _yahoo_warmed = True


def fetch_yahoo(ticker):
    """Fetch diretto senza yfinance. Prova 2 server, torna None se falliscono entrambi."""
    _warm_yahoo_session()
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=2y"
            r = YAHOO_SESSION.get(url, timeout=12)
            if r.status_code != 200:
                print(f"Yahoo {base} HTTP {r.status_code} per {ticker}: {r.text[:200]!r}")
                continue
            j = r.json()
            result = j["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            raw_closes = quote.get("close", [])
            raw_volumes = quote.get("volume", [])
            paired = [(c, v or 0) for c, v in zip(raw_closes, raw_volumes) if c]
            if not paired:
                continue
            closes = [p[0] for p in paired]
            volumes = [p[1] for p in paired]
            meta = result["meta"]
            return {
                "closes": closes,
                "volumes": volumes,
                "price": float(meta.get("regularMarketPrice", closes[-1])),
                "currency": meta.get("currency", "USD"),
                "name": meta.get("shortName", ticker),
            }
        except Exception as e:
            print(f"Yahoo {base} fallito per {ticker}: {e}")
    return None


def fetch_stooq(ticker):
    """Fallback gratuito senza autenticazione, usato se Yahoo è bloccato.
    Copertura minore (soprattutto titoli USA) e niente nome/valuta precisi."""
    symbol = ticker.lower()
    if "." not in symbol:
        symbol += ".us"
    try:
        url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
        r = requests.get(url, headers=YAHOO_HEADERS, timeout=12)
        if r.status_code != 200 or not r.text.startswith("Date,"):
            print(f"Stooq HTTP {r.status_code} per {ticker}: {r.text[:120]!r}")
            return None
        closes, volumes = [], []
        for line in r.text.strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 6:
                try:
                    closes.append(float(parts[4]))
                    volumes.append(float(parts[5]))
                except ValueError:
                    continue
        closes = closes[-504:]  # circa 2 anni di sedute
        volumes = volumes[-504:]
        if len(closes) < 2:
            return None
        return {
            "closes": closes,
            "volumes": volumes,
            "price": closes[-1],
            "currency": "USD",
            "name": ticker.upper(),
        }
    except Exception as e:
        print(f"Stooq fallito per {ticker}: {e}")
        return None


def fetch_twelvedata(ticker):
    """Terzo fallback, con API key gratuita (twelvedata.com). Usato solo se
    TWELVEDATA_API_KEY è impostata: utile quando l'hosting cloud ha l'IP
    bloccato sia da Yahoo che da Stooq (capita su alcuni piani gratuiti)."""
    if not config.TWELVEDATA_API_KEY:
        return None
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": ticker,
            "interval": "1day",
            "outputsize": 260,
            "apikey": config.TWELVEDATA_API_KEY,
        }
        r = requests.get(url, params=params, timeout=12)
        if r.status_code != 200:
            print(f"Twelve Data HTTP {r.status_code} per {ticker}: {r.text[:200]!r}")
            return None
        j = r.json()
        if j.get("status") == "error" or "values" not in j:
            print(f"Twelve Data errore per {ticker}: {j.get('message', j)}")
            return None
        values = list(reversed(j["values"]))  # dal più vecchio al più recente
        closes, volumes = [], []
        for v in values:
            try:
                closes.append(float(v["close"]))
                volumes.append(float(v.get("volume") or 0))
            except (TypeError, ValueError, KeyError):
                continue
        if len(closes) < 2:
            return None
        return {
            "closes": closes,
            "volumes": volumes,
            "price": closes[-1],
            "currency": "USD",
            "name": ticker.upper(),
        }
    except Exception as e:
        print(f"Twelve Data fallito per {ticker}: {e}")
        return None


def fetch_market_data(ticker):
    """Yahoo come fonte primaria, poi Stooq, poi Twelve Data (se configurata)."""
    return fetch_yahoo(ticker) or fetch_stooq(ticker) or fetch_twelvedata(ticker)


# --------------------------------------------------------------------------
# Ricerca ticker per suggerimenti (Scanner / Portafoglio / Alert)
# --------------------------------------------------------------------------
# Elenco locale di titoli comuni: garantisce suggerimenti istantanei anche
# quando la ricerca live su Yahoo è bloccata (stesso problema di fetch_yahoo).
COMMON_TICKERS = [
    {"symbol": "AAPL", "name": "Apple Inc."},
    {"symbol": "MSFT", "name": "Microsoft Corp."},
    {"symbol": "GOOGL", "name": "Alphabet Inc. (Google)"},
    {"symbol": "AMZN", "name": "Amazon.com Inc."},
    {"symbol": "NVDA", "name": "NVIDIA Corp."},
    {"symbol": "TSLA", "name": "Tesla Inc."},
    {"symbol": "META", "name": "Meta Platforms Inc."},
    {"symbol": "MU", "name": "Micron Technology"},
    {"symbol": "ASML", "name": "ASML Holding"},
    {"symbol": "TSM", "name": "Taiwan Semiconductor Manufacturing (TSMC)"},
    {"symbol": "SNDK", "name": "SanDisk Corp."},
    {"symbol": "AMD", "name": "Advanced Micro Devices"},
    {"symbol": "INTC", "name": "Intel Corp."},
    {"symbol": "AVGO", "name": "Broadcom Inc."},
    {"symbol": "ORCL", "name": "Oracle Corp."},
    {"symbol": "CRM", "name": "Salesforce Inc."},
    {"symbol": "ADBE", "name": "Adobe Inc."},
    {"symbol": "NFLX", "name": "Netflix Inc."},
    {"symbol": "DIS", "name": "Walt Disney Co."},
    {"symbol": "KO", "name": "Coca-Cola Co."},
    {"symbol": "PEP", "name": "PepsiCo Inc."},
    {"symbol": "JPM", "name": "JPMorgan Chase & Co."},
    {"symbol": "V", "name": "Visa Inc."},
    {"symbol": "MA", "name": "Mastercard Inc."},
    {"symbol": "WMT", "name": "Walmart Inc."},
    {"symbol": "HD", "name": "Home Depot Inc."},
    {"symbol": "PG", "name": "Procter & Gamble Co."},
    {"symbol": "JNJ", "name": "Johnson & Johnson"},
    {"symbol": "UNH", "name": "UnitedHealth Group"},
    {"symbol": "XOM", "name": "Exxon Mobil Corp."},
    {"symbol": "CVX", "name": "Chevron Corp."},
    {"symbol": "BAC", "name": "Bank of America Corp."},
    {"symbol": "PFE", "name": "Pfizer Inc."},
    {"symbol": "T", "name": "AT&T Inc."},
    {"symbol": "VZ", "name": "Verizon Communications"},
    {"symbol": "CSCO", "name": "Cisco Systems"},
    {"symbol": "QCOM", "name": "Qualcomm Inc."},
    {"symbol": "TXN", "name": "Texas Instruments"},
    {"symbol": "IBM", "name": "IBM Corp."},
    {"symbol": "GE", "name": "General Electric Co."},
    {"symbol": "BA", "name": "Boeing Co."},
    {"symbol": "CAT", "name": "Caterpillar Inc."},
    {"symbol": "MCD", "name": "McDonald's Corp."},
    {"symbol": "NKE", "name": "Nike Inc."},
    {"symbol": "SBUX", "name": "Starbucks Corp."},
    {"symbol": "COST", "name": "Costco Wholesale"},
    {"symbol": "LOW", "name": "Lowe's Companies"},
    {"symbol": "UPS", "name": "United Parcel Service"},
    {"symbol": "GS", "name": "Goldman Sachs Group"},
    {"symbol": "MS", "name": "Morgan Stanley"},
    {"symbol": "AXP", "name": "American Express Co."},
    {"symbol": "UBER", "name": "Uber Technologies"},
    {"symbol": "ABNB", "name": "Airbnb Inc."},
    {"symbol": "SHOP", "name": "Shopify Inc."},
    {"symbol": "PYPL", "name": "PayPal Holdings"},
    {"symbol": "SQ", "name": "Block Inc. (Square)"},
    {"symbol": "SNOW", "name": "Snowflake Inc."},
    {"symbol": "PLTR", "name": "Palantir Technologies"},
    {"symbol": "COIN", "name": "Coinbase Global"},
    {"symbol": "ARM", "name": "Arm Holdings"},
    {"symbol": "SMCI", "name": "Super Micro Computer"},
    {"symbol": "STM", "name": "STMicroelectronics"},
    {"symbol": "NXPI", "name": "NXP Semiconductors"},
    {"symbol": "ON", "name": "ON Semiconductor"},
    {"symbol": "MRVL", "name": "Marvell Technology"},
    {"symbol": "LRCX", "name": "Lam Research"},
    {"symbol": "KLAC", "name": "KLA Corp."},
    {"symbol": "AMAT", "name": "Applied Materials"},
    {"symbol": "TER", "name": "Teradyne Inc."},
    {"symbol": "ENTG", "name": "Entegris Inc."},
]


def search_local_tickers(query):
    ql = query.strip().lower()
    if not ql:
        return []
    matches = [
        t for t in COMMON_TICKERS if ql in t["symbol"].lower() or ql in t["name"].lower()
    ]
    matches.sort(key=lambda t: (not t["symbol"].lower().startswith(ql), t["symbol"]))
    return matches


def search_yahoo_tickers(query):
    _warm_yahoo_session()
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search"
        r = YAHOO_SESSION.get(
            url, params={"q": query, "quotesCount": 8, "newsCount": 0}, timeout=8
        )
        if r.status_code != 200:
            return []
        quotes = r.json().get("quotes", [])
        out = []
        for q in quotes:
            symbol = q.get("symbol")
            name = q.get("shortname") or q.get("longname")
            if symbol and name:
                out.append({"symbol": symbol, "name": name})
        return out
    except Exception as e:
        print(f"Ricerca Yahoo fallita per '{query}': {e}")
        return []


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


def compute_signal(closes, price, custom_buy=None, custom_sell=None, volumes=None):
    """Calcola segnale BUY/HOLD/SELL, score -100/+100 e motivazioni in italiano.
    Tutto è automatico (RSI, medie mobili, massimi/minimi 52W, volumi): le
    soglie personali sono solo un avviso extra opzionale, non governano il
    segnale principale."""
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

    vol_ratio = None
    if volumes and len(volumes) >= 11:
        avg_vol20 = statistics.mean(volumes[-21:-1]) if len(volumes) >= 21 else statistics.mean(volumes[:-1])
        last_vol = volumes[-1]
        if avg_vol20 > 0:
            vol_ratio = last_vol / avg_vol20
            if vol_ratio > 1.8 and day_chg > 2:
                score += 10
                reasons.append(f"Volume {vol_ratio:.1f}x la media — conferma rialzista")
            elif vol_ratio > 1.8 and day_chg < -2:
                score -= 10
                reasons.append(f"Volume {vol_ratio:.1f}x la media — conferma ribassista")

    # Soglie personali: solo un avviso extra facoltativo che l'utente può
    # impostare a piacere, non sono richieste (il segnale sopra è già
    # calcolato in automatico dall'analisi tecnica).
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
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        # Zone di supporto/resistenza calcolate in automatico dal range a 52
        # settimane: usate come suggerimento se l'utente non imposta soglie sue.
        "suggested_buy": round(low52 * 1.05, 2),
        "suggested_sell": round(high52 * 0.97, 2),
    }


def analyze_ticker(ticker, custom_buy=None, custom_sell=None):
    """Recupera i dati e calcola il segnale per un ticker. Non solleva mai eccezioni."""
    try:
        data = fetch_market_data(ticker)
        if not data or len(data["closes"]) < 2:
            return {"ticker": ticker, "error": f"Impossibile recuperare dati per {ticker}"}

        sig = compute_signal(
            data["closes"], data["price"], custom_buy, custom_sell, data.get("volumes")
        )
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
def send_mail(subject, body):
    to_email = get_alert_email()
    if not config.EMAIL_FROM or not config.EMAIL_PASSWORD:
        print(f"Mail non inviata (credenziali mittente mancanti): {subject}")
        return
    if not to_email:
        print(f"Mail non inviata (nessuna email impostata nelle Impostazioni): {subject}")
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


def notify_signal_change(ticker, old_signal, new_result):
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
    send_mail(subject, body)


def notify_alert(ticker, condition, threshold, price):
    label = "sopra" if condition == "above" else "sotto"
    subject = f"🔔 CECCHINO ALERT: {ticker} {label} {threshold}"
    body = (
        f"Ticker: {ticker}\n"
        f"Condizione: prezzo {label} {threshold}\n"
        f"Prezzo attuale: {price}\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    send_mail(subject, body)


# --------------------------------------------------------------------------
# Storico segnali + Alert
# --------------------------------------------------------------------------
def record_signal_if_changed(conn, ticker, result):
    """Se il segnale è cambiato rispetto all'ultimo registrato, lo salva e invia mail."""
    last = conn.execute(
        "SELECT signal FROM signals WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    old_signal = last["signal"] if last else None

    if old_signal != result["signal"]:
        conn.execute(
            "INSERT INTO signals (ticker, signal, price, score, rsi, reasons) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
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
            notify_signal_change(ticker, old_signal, result)


def check_alerts(conn, ticker, price):
    rows = conn.execute(
        "SELECT * FROM alerts WHERE ticker = ? AND triggered = 0", (ticker,)
    ).fetchall()
    for a in rows:
        hit = (a["condition"] == "above" and price >= a["price"]) or (
            a["condition"] == "below" and price <= a["price"]
        )
        if hit:
            conn.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (a["id"],))
            conn.commit()
            notify_alert(ticker, a["condition"], a["price"], price)


def analyze_and_store(ticker, custom_buy, custom_sell):
    """Analizza un ticker del portafoglio, aggiorna cache, storico ed alert."""
    result = analyze_ticker(ticker, custom_buy, custom_sell)
    with CACHE_LOCK:
        LAST_ANALYSIS[ticker] = result

    if "error" in result:
        return result

    conn = get_db()
    try:
        record_signal_if_changed(conn, ticker, result)
        check_alerts(conn, ticker, result["price"])
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------
# Monitor (ogni ora via thread locale, oppure via /api/cron/tick esterno)
# --------------------------------------------------------------------------
def refresh_all_portfolio():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM tickers WHERE active = 1").fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            analyze_and_store(row["ticker"], row["custom_buy"], row["custom_sell"])
        except Exception as e:
            print(f"Errore aggiornamento {row['ticker']}: {e}")


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
def api_portfolio_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tickers WHERE active = 1 ORDER BY ticker").fetchall()
    conn.close()

    out = []
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get(row["ticker"])
        if cached is None:
            cached = analyze_and_store(row["ticker"], row["custom_buy"], row["custom_sell"])

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
def api_portfolio_add():
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
        INSERT INTO tickers (ticker, qty, paid, custom_buy, custom_sell, active)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(ticker) DO UPDATE SET
            qty = excluded.qty,
            paid = excluded.paid,
            custom_buy = excluded.custom_buy,
            custom_sell = excluded.custom_sell,
            active = 1
        """,
        (ticker, qty, paid, custom_buy, custom_sell),
    )
    conn.commit()
    conn.close()

    result = analyze_and_store(ticker, custom_buy, custom_sell)
    return jsonify(result)


@app.route("/api/portfolio/<ticker>", methods=["DELETE"])
def api_portfolio_remove(ticker):
    ticker = ticker.strip().upper()
    conn = get_db()
    conn.execute("UPDATE tickers SET active = 0 WHERE ticker = ?", (ticker,))
    conn.commit()
    conn.close()
    with CACHE_LOCK:
        LAST_ANALYSIS.pop(ticker, None)
    return jsonify({"ok": True})


@app.route("/api/portfolio/refresh", methods=["POST"])
def api_portfolio_refresh():
    refresh_all_portfolio()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API - Scanner
# --------------------------------------------------------------------------
@app.route("/api/scan/<ticker>", methods=["GET"])
def api_scan(ticker):
    ticker = ticker.strip().upper()
    conn = get_db()
    row = conn.execute("SELECT * FROM tickers WHERE ticker = ? AND active = 1", (ticker,)).fetchone()
    conn.close()

    custom_buy = row["custom_buy"] if row else None
    custom_sell = row["custom_sell"] if row else None
    result = analyze_ticker(ticker, custom_buy, custom_sell)
    return jsonify(result)


@app.route("/api/search/<query>", methods=["GET"])
def api_search(query):
    """Suggerimenti ticker per nome o simbolo: lista locale (sempre disponibile)
    unita ai risultati live di Yahoo (se raggiungibile)."""
    query = query.strip()
    if not query:
        return jsonify([])

    merged = {t["symbol"]: t for t in search_local_tickers(query)}
    for t in search_yahoo_tickers(query):
        merged.setdefault(t["symbol"], t)

    ql = query.lower()
    ranked = sorted(merged.values(), key=lambda t: (not t["symbol"].lower().startswith(ql), t["symbol"]))
    return jsonify(ranked[:8])


# --------------------------------------------------------------------------
# API - Alert
# --------------------------------------------------------------------------
@app.route("/api/alerts", methods=["GET"])
def api_alerts_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM alerts ORDER BY created DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/alerts", methods=["POST"])
def api_alerts_add():
    data = request.get_json(force=True)
    ticker = (data.get("ticker") or "").strip().upper()
    condition = data.get("condition")
    price = data.get("price")

    if not ticker or condition not in ("above", "below") or price in (None, ""):
        return jsonify({"error": "Dati alert non validi"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO alerts (ticker, condition, price, triggered) VALUES (?, ?, ?, 0)",
        (ticker, condition, float(price)),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def api_alerts_remove(alert_id):
    conn = get_db()
    conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API - Storico
# --------------------------------------------------------------------------
@app.route("/api/history", methods=["GET"])
def api_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY timestamp DESC LIMIT 200"
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
    return render_template_string(INDEX_HTML, alert_email=get_alert_email() or "")


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
h1 { font-size: 20px; margin: 6px 0 16px 0; }
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

.ticker-field { position: relative; }
.suggest-box {
  position: absolute;
  left: 0; right: 0; top: calc(100% + 4px);
  background: var(--card2);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
  z-index: 50;
  max-height: 240px;
  overflow-y: auto;
  display: none;
}
.suggest-box.open { display: block; }
.suggest-item {
  padding: 9px 10px;
  cursor: pointer;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.suggest-item:last-child { border-bottom: none; }
.suggest-item:hover, .suggest-item.active { background: var(--border); }
.suggest-item b { color: var(--text); }
.suggest-item span { color: var(--dim); margin-left: 6px; }

#email-gate {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 999;
  padding: 20px;
}
#email-gate .box { max-width: 360px; text-align: center; }
#email-gate h1 { font-size: 24px; }
#email-gate p { color: var(--dim); font-size: 14px; line-height: 1.5; }
#email-gate input { width: 100%; margin-top: 18px; text-align: center; }
#email-gate button { width: 100%; margin-top: 10px; }
#email-gate .err { color: var(--sell); font-size: 13px; margin-top: 8px; min-height: 16px; }
</style>
</head>
<body>

<div id="email-gate" style="display:none">
  <div class="box">
    <h1>🎯 Cecchino Pro</h1>
    <p>Inserisci la tua email: è l'indirizzo a cui arriveranno gli alert BUY/SELL.</p>
    <input id="gate-email" type="email" placeholder="tuamail@esempio.com">
    <button onclick="saveGateEmail()">Inizia</button>
    <div class="err" id="gate-err"></div>
  </div>
</div>

<div id="app">
  <div class="row">
    <h1>🎯 Cecchino Pro</h1>
    <span class="dim" id="email-display" onclick="openEmailEdit()" style="cursor:pointer"></span>
  </div>

  <!-- SCANNER -->
  <div class="tab-view active" id="tab-scanner">
    <div class="card">
      <div class="row">
        <div class="ticker-field" style="flex:1">
          <input id="scan-input" placeholder="Ticker o nome (es. MU, Micron)" style="width:100%" autocapitalize="characters" autocomplete="off">
          <div class="suggest-box" id="scan-suggest"></div>
        </div>
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
        <div class="ticker-field full">
          <input id="pf-ticker" placeholder="Ticker o nome" style="width:100%" autocomplete="off">
          <div class="suggest-box" id="pf-suggest"></div>
        </div>
        <input id="pf-qty" placeholder="Quantità" type="number" step="any">
        <input id="pf-paid" placeholder="Totale pagato €" type="number" step="any">
        <input id="pf-buy" placeholder="Soglia acquisto (opzionale)" type="number" step="any">
        <input id="pf-sell" placeholder="Soglia vendita (opzionale)" type="number" step="any">
        <div class="dim full" style="font-size:12px">🤖 Il segnale BUY/HOLD/SELL è già calcolato in automatico dall'analisi tecnica (RSI, medie mobili, massimi/minimi 52 settimane, volumi). Le soglie qui sopra sono solo un avviso extra a un prezzo preciso, se vuoi: lasciale vuote e ci pensa l'algoritmo.</div>
        <button class="full" onclick="addToPortfolio()">Salva</button>
      </div>
    </div>
    <div id="portfolio-list"></div>
  </div>

  <!-- ALERT -->
  <div class="tab-view" id="tab-alerts">
    <div class="card">
      <div class="form-grid">
        <div class="ticker-field full">
          <input id="al-ticker" placeholder="Ticker o nome" style="width:100%" autocomplete="off">
          <div class="suggest-box" id="al-suggest"></div>
        </div>
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
let CURRENT_EMAIL = {{ alert_email|tojson }};

function refreshEmailUI() {
  const gate = document.getElementById('email-gate');
  const display = document.getElementById('email-display');
  if (!CURRENT_EMAIL) {
    gate.style.display = 'flex';
    display.textContent = '';
  } else {
    gate.style.display = 'none';
    display.textContent = '✉️ ' + CURRENT_EMAIL;
  }
}

async function saveGateEmail() {
  const email = document.getElementById('gate-email').value.trim();
  const errEl = document.getElementById('gate-err');
  errEl.textContent = '';
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email}),
  });
  const data = await res.json();
  if (!res.ok) {
    errEl.textContent = data.error || 'Errore';
    return;
  }
  CURRENT_EMAIL = data.email;
  refreshEmailUI();
}

function openEmailEdit() {
  const next = prompt('Email per gli alert:', CURRENT_EMAIL || '');
  if (next === null) return;
  fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: next.trim()}),
  }).then(res => res.json()).then(data => {
    if (data.email) {
      CURRENT_EMAIL = data.email;
      refreshEmailUI();
    } else if (data.error) {
      alert(data.error);
    }
  });
}

refreshEmailUI();

function attachTickerSuggest(inputId, boxId, onPick) {
  const input = document.getElementById(inputId);
  const box = document.getElementById(boxId);
  let timer = null;
  let items = [];
  let activeIdx = -1;

  function close() {
    box.classList.remove('open');
    box.innerHTML = '';
    items = [];
    activeIdx = -1;
  }

  function render() {
    if (items.length === 0) { close(); return; }
    box.innerHTML = items.map((it, i) => `
      <div class="suggest-item${i === activeIdx ? ' active' : ''}" data-idx="${i}">
        <b>${it.symbol}</b><span>${it.name}</span>
      </div>`).join('');
    box.classList.add('open');
    box.querySelectorAll('.suggest-item').forEach(el => {
      el.addEventListener('mousedown', (e) => {
        e.preventDefault();
        const it = items[parseInt(el.dataset.idx, 10)];
        input.value = it.symbol;
        close();
        if (onPick) onPick(it);
      });
    });
  }

  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(timer);
    if (q.length < 1) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/search/${encodeURIComponent(q)}`);
        items = await res.json();
        activeIdx = -1;
        render();
      } catch (e) { close(); }
    }, 250);
  });

  input.addEventListener('keydown', (e) => {
    if (!box.classList.contains('open')) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, items.length - 1); render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
    else if (e.key === 'Enter' && activeIdx >= 0) { e.preventDefault(); input.value = items[activeIdx].symbol; close(); if (onPick) onPick(items[activeIdx]); }
    else if (e.key === 'Escape') { close(); }
  });

  input.addEventListener('blur', () => setTimeout(close, 150));
}

attachTickerSuggest('scan-input', 'scan-suggest');
attachTickerSuggest('pf-ticker', 'pf-suggest');
attachTickerSuggest('al-ticker', 'al-suggest');

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
      <div class="metric"><div class="val">${a.vol_ratio != null ? a.vol_ratio + 'x' : '—'}</div><div class="lbl">Volume/media</div></div>
      <div class="metric"><div class="val">${a.suggested_buy}</div><div class="lbl">Zona acquisto 🤖</div></div>
      <div class="metric"><div class="val">${a.suggested_sell}</div><div class="lbl">Zona vendita 🤖</div></div>
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

async function scanTicker() {
  const ticker = document.getElementById('scan-input').value.trim().toUpperCase();
  if (!ticker) return;
  document.getElementById('scan-result').innerHTML = '<div class="spinner">Analisi in corso…</div>';
  const res = await fetch(`/api/scan/${ticker}`);
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
  const res = await fetch('/api/portfolio');
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
  await fetch('/api/portfolio', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  document.getElementById('add-form').style.display = 'none';
  ['pf-ticker','pf-qty','pf-paid','pf-buy','pf-sell'].forEach(id => document.getElementById(id).value = '');
  loadPortfolio();
}

async function removeFromPortfolio(ticker) {
  await fetch(`/api/portfolio/${ticker}`, { method: 'DELETE' });
  loadPortfolio();
}

async function refreshPortfolio() {
  document.getElementById('portfolio-list').innerHTML = '<div class="spinner">Aggiornamento in corso…</div>';
  await fetch('/api/portfolio/refresh', { method: 'POST' });
  loadPortfolio();
}

async function loadAlerts() {
  document.getElementById('alerts-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/alerts');
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
  await fetch('/api/alerts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  document.getElementById('al-ticker').value = '';
  document.getElementById('al-price').value = '';
  loadAlerts();
}

async function removeAlert(id) {
  await fetch(`/api/alerts/${id}`, { method: 'DELETE' });
  loadAlerts();
}

async function loadHistory() {
  document.getElementById('history-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/history');
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
