"""
Cecchino Pro - sistema di trading signal monitoring.

App Flask semplice: alla prima apertura chiede solo l'indirizzo email a cui
mandare gli alert (nessun login Google, nessuna configurazione OAuth).
Poi Scanner, Portafoglio, Alert e Storico. Un thread in background e un
endpoint /api/cron/tick (per trigger esterni gratuiti come GitHub Actions)
analizzano i titoli ogni ora e mandano una mail quando un segnale cambia o
un alert scatta.
"""
import base64
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

# Cache in memoria dell'ultimo giro dello screener di mercato (vedi
# run_market_screener più sotto), servita da /api/opportunities.
SCREENER_CACHE = {"results": [], "updated": None}

# Cache in memoria dell'ultimo giro della watchlist ingresso/stop/target
# (vedi check_watch_levels più sotto), servita da /api/watchlist.
WATCHLIST_CACHE = {}


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

        CREATE TABLE IF NOT EXISTS watch_levels (
            id INTEGER PRIMARY KEY,
            ticker TEXT UNIQUE,
            name TEXT,
            entry_low REAL,
            entry_high REAL,
            stop_price REAL,
            target_low REAL,
            target_high REAL,
            reference_price REAL,
            entry_notified INTEGER DEFAULT 0,
            stop_notified INTEGER DEFAULT 0,
            target_notified INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watch_log (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            signal_type TEXT,
            price REAL,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS screener_signals (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            signal TEXT,
            reasons TEXT,
            week_key TEXT,
            notified INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bottleneck_cache (
            ticker TEXT PRIMARY KEY,
            data_json TEXT,
            fetched_at REAL
        );

        CREATE TABLE IF NOT EXISTS bottleneck_scan (
            ticker TEXT PRIMARY KEY,
            result_json TEXT,
            scanned_at REAL
        );

        CREATE TABLE IF NOT EXISTS bottleneck_decisions (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            verdict_a TEXT,
            verdict_b TEXT,
            engine_a_json TEXT,
            engine_b_json TEXT,
            thresholds_json TEXT,
            price_at_decision REAL,
            price_3m REAL,
            price_6m REAL,
            price_12m REAL,
            checked_3m INTEGER DEFAULT 0,
            checked_6m INTEGER DEFAULT 0,
            checked_12m INTEGER DEFAULT 0,
            created DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            url TEXT UNIQUE,
            title TEXT,
            publisher TEXT,
            event_type TEXT,
            severity INTEGER,
            confidence REAL,
            published_at REAL,
            logged_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            price REAL,
            technical_json TEXT,
            fundamental_json TEXT,
            bottleneck_json TEXT,
            news_json TEXT,
            technical_score REAL,
            fundamental_score REAL,
            bottleneck_score REAL,
            news_score REAL,
            final_score REAL,
            decision TEXT,
            previous_decision TEXT,
            changed INTEGER DEFAULT 0,
            reason_codes TEXT,
            filter_version TEXT,
            data_sources TEXT,
            price_3m REAL,
            price_6m REAL,
            price_12m REAL,
            return_3m REAL,
            return_6m REAL,
            return_12m REAL,
            checked_3m INTEGER DEFAULT 0,
            checked_6m INTEGER DEFAULT 0,
            checked_12m INTEGER DEFAULT 0,
            outcome TEXT
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

    # Migrazioni additive per stop-loss dinamico e screener di mercato:
    # SQLite non ha "ADD COLUMN IF NOT EXISTS", si prova e si ignora
    # l'errore se la colonna esiste già (idempotente ad ogni avvio).
    for ddl in [
        "ALTER TABLE tickers ADD COLUMN high_water_mark REAL DEFAULT 0",
        "ALTER TABLE tickers ADD COLUMN stop_notified_level REAL DEFAULT 0",
        "ALTER TABLE tickers ADD COLUMN stop_triggered INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN last_screener_sent TEXT",
        "ALTER TABLE settings ADD COLUMN last_screener_results TEXT",
        "ALTER TABLE settings ADD COLUMN telegram_chat_id TEXT",
        "ALTER TABLE settings ADD COLUMN last_verdict_sent TEXT",
        "ALTER TABLE settings ADD COLUMN last_verdict_text TEXT",
        "ALTER TABLE tickers ADD COLUMN owner TEXT",
        "ALTER TABLE settings ADD COLUMN last_weekly_screener_sent TEXT",
        "ALTER TABLE settings ADD COLUMN last_weekly_screener_results TEXT",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # colonna già esistente

    conn.commit()
    conn.close()


def seed_screener_universe():
    """Tagga nella tabella tickers i titoli "owned" dello screener a 25 con
    il loro proprietario (mohamed/micaela/shared), per il calcolo della
    concentrazione settoriale. Non tocca qty/paid se il titolo esiste già
    (es. importato da foto): imposta solo owner. Se il titolo non esiste
    ancora lo crea con qty=0/paid=0 — l'utente dovrà aggiornarli a mano o
    con l'import da foto per avere P&L e concentrazione corretti."""
    conn = get_db()
    try:
        for spec in config.SCREENER_UNIVERSE:
            if spec["category"] != "owned":
                continue
            existing = conn.execute(
                "SELECT id FROM tickers WHERE ticker = ?", (spec["ticker"],)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE tickers SET owner = ? WHERE ticker = ?", (spec["owner"], spec["ticker"])
                )
            else:
                conn.execute(
                    "INSERT INTO tickers (ticker, qty, paid, active, owner) VALUES (?, 0, 0, 1, ?)",
                    (spec["ticker"], spec["owner"]),
                )
        conn.commit()
    finally:
        conn.close()


SCREENER_BY_TICKER = {s["ticker"]: s for s in config.SCREENER_UNIVERSE}


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


def get_telegram_chat_id():
    conn = get_db()
    row = conn.execute("SELECT telegram_chat_id FROM settings WHERE id = 1").fetchone()
    conn.close()
    return row["telegram_chat_id"] if row else None


def set_telegram_chat_id(chat_id):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (id, telegram_chat_id) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET telegram_chat_id = excluded.telegram_chat_id",
        (chat_id,),
    )
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# Impostazioni - indirizzo email e chat Telegram per gli alert
# --------------------------------------------------------------------------
_TELEGRAM_BOT_INFO_CACHE = {"data": None, "at": 0}


def get_telegram_bot_info():
    """Username/nome del bot via Telegram getMe, per mostrare "@NomeBot" e
    costruire il link t.me/NomeBot corretto (mai inventato/hardcoded).
    Cache di processo: il bot non cambia username durante l'esecuzione."""
    if not config.TELEGRAM_BOT_TOKEN:
        return None
    now = time.time()
    if _TELEGRAM_BOT_INFO_CACHE["data"] and now - _TELEGRAM_BOT_INFO_CACHE["at"] < 3600:
        return _TELEGRAM_BOT_INFO_CACHE["data"]
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getMe"
        r = requests.get(url, timeout=8)
        j = r.json()
        if not j.get("ok"):
            return None
        result = j["result"]
        info = {"username": result.get("username"), "name": result.get("first_name")}
        _TELEGRAM_BOT_INFO_CACHE["data"] = info
        _TELEGRAM_BOT_INFO_CACHE["at"] = now
        return info
    except Exception as e:
        print(f"Telegram getMe fallito: {e}")
        return None


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    bot_info = get_telegram_bot_info()
    return jsonify({
        "email": get_alert_email(),
        "telegram_chat_id": get_telegram_chat_id(),
        "telegram_bot_configured": bool(config.TELEGRAM_BOT_TOKEN),
        "telegram_bot_username": bot_info["username"] if bot_info else None,
        "telegram_bot_name": bot_info["name"] if bot_info else None,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    """Email facoltativa: Telegram è il canale principale (vedi gate
    d'ingresso), la mail resta un extra opzionale. Un campo vuoto è valido
    (significa "nessuna mail"), non un errore."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    if email and not EMAIL_RE.match(email):
        return jsonify({"error": "Email non valida"}), 400
    set_alert_email(email)
    return jsonify({"ok": True, "email": email})


@app.route("/api/settings/telegram", methods=["POST"])
def api_settings_telegram_set():
    data = request.get_json(force=True)
    chat_id = str(data.get("chat_id") or "").strip()
    set_telegram_chat_id(chat_id or None)
    return jsonify({"ok": True, "telegram_chat_id": chat_id or None})


@app.route("/api/settings/telegram/detect", methods=["POST"])
def api_settings_telegram_detect():
    """Trova automaticamente il chat_id di chi ha scritto per ultimo al bot,
    così l'utente non deve cercarlo a mano (basta mandare un messaggio al bot
    prima di premere questo pulsante)."""
    if not config.TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "Bot Telegram non configurato (manca TELEGRAM_BOT_TOKEN)"}), 400
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
        r = requests.get(url, timeout=10)
        j = r.json()
        if not j.get("ok"):
            return jsonify({"error": f"Errore Telegram: {j.get('description', 'sconosciuto')}"}), 400
        updates = j.get("result", [])
        if not updates:
            return jsonify({"error": "Nessun messaggio trovato: scrivi prima qualcosa al bot su Telegram, poi riprova"}), 404
        last = updates[-1]
        chat = (last.get("message") or last.get("channel_post") or {}).get("chat", {})
        chat_id = chat.get("id")
        name = chat.get("first_name") or chat.get("title") or ""
        if not chat_id:
            return jsonify({"error": "Chat non trovata nell'ultimo messaggio"}), 404
        set_telegram_chat_id(str(chat_id))
        return jsonify({"ok": True, "telegram_chat_id": str(chat_id), "name": name})
    except Exception as e:
        return jsonify({"error": f"Errore di rete: {e}"}), 500


@app.route("/api/settings/telegram/test", methods=["POST"])
def api_settings_telegram_test():
    """Manda un messaggio di prova al chat_id già collegato, per dare
    conferma concreta ("è arrivato davvero") invece di fidarsi solo dello
    stato salvato lato server."""
    chat_id = get_telegram_chat_id()
    if not chat_id:
        return jsonify({"error": "Nessuna chat Telegram collegata"}), 400
    if not config.TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "Bot Telegram non configurato (manca TELEGRAM_BOT_TOKEN)"}), 400
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": "✅ Cecchino Pro — messaggio di prova. Se lo leggi, il collegamento funziona.",
        }, timeout=10)
        j = r.json()
        if not j.get("ok"):
            return jsonify({"error": j.get("description", "invio fallito")}), 400
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"Errore di rete: {e}"}), 500


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


def is_crypto_ticker(ticker):
    """Riconosce simboli tipo BTC-USD, ETH-USD (formato Yahoo per le crypto)."""
    return bool(re.match(r"^[A-Z0-9]{2,10}-[A-Z]{3,4}$", ticker.upper()))


def fetch_stooq(ticker):
    """Fallback gratuito senza autenticazione, usato se Yahoo è bloccato.
    Copertura minore (soprattutto titoli USA) e niente nome/valuta precisi."""
    if is_crypto_ticker(ticker):
        symbol = ticker.lower().replace("-", "")  # BTC-USD -> btcusd
    else:
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


# Il piano free di Twelve Data limita a 8 richieste/minuto: senza un limite
# lato nostro, il thread di background e un caricamento pagina concorrente
# possono sommarsi e far scattare 429 solo su alcuni ticker (sintomo:
# "funziona per alcuni titoli e non per altri" dopo un riavvio). Si
# serializza qui con una finestra scorrevole, restando sotto il limite.
_TWELVEDATA_LOCK = threading.Lock()
_TWELVEDATA_CALL_TIMES = []
_TWELVEDATA_MAX_PER_MINUTE = 6


def _throttle_twelvedata():
    with _TWELVEDATA_LOCK:
        now = time.time()
        while _TWELVEDATA_CALL_TIMES and now - _TWELVEDATA_CALL_TIMES[0] > 60:
            _TWELVEDATA_CALL_TIMES.pop(0)
        if len(_TWELVEDATA_CALL_TIMES) >= _TWELVEDATA_MAX_PER_MINUTE:
            wait = 60 - (now - _TWELVEDATA_CALL_TIMES[0]) + 0.5
            if wait > 0:
                time.sleep(wait)
            now = time.time()
            while _TWELVEDATA_CALL_TIMES and now - _TWELVEDATA_CALL_TIMES[0] > 60:
                _TWELVEDATA_CALL_TIMES.pop(0)
        _TWELVEDATA_CALL_TIMES.append(time.time())


def fetch_twelvedata(ticker):
    """Terzo fallback, con API key gratuita (twelvedata.com). Usato solo se
    TWELVEDATA_API_KEY è impostata: utile quando l'hosting cloud ha l'IP
    bloccato sia da Yahoo che da Stooq (capita su alcuni piani gratuiti)."""
    if not config.TWELVEDATA_API_KEY:
        return None
    symbol = ticker.replace("-", "/") if is_crypto_ticker(ticker) else ticker
    try:
        _throttle_twelvedata()
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": symbol,
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
# Fondamentali per il Bottleneck Filter (Motore A + Motore B).
# Endpoint non ufficiale di Yahoo (quoteSummary), stesso usato internamente
# da yfinance: nessuna chiave richiesta, ma i moduli possono mancare per
# molti titoli (specie fuori USA) — ogni campo può tornare None, e questo è
# gestito a valle come "dato non disponibile", mai come bocciatura.
def fetch_yahoo_fundamentals(ticker):
    _warm_yahoo_session()
    modules = (
        "defaultKeyStatistics,financialData,summaryDetail,price,"
        "incomeStatementHistoryQuarterly,cashflowStatementHistoryQuarterly,"
        "recommendationTrend,calendarEvents"
    )
    data = None
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
            r = YAHOO_SESSION.get(url, params={"modules": modules}, timeout=12)
            if r.status_code != 200:
                continue
            result = r.json()["quoteSummary"]["result"]
            if not result:
                continue
            data = result[0]
            break
        except Exception as e:
            print(f"Yahoo fundamentals {base} fallito per {ticker}: {e}")
    if data is None:
        return None

    def raw(mod, field):
        v = (data.get(mod) or {}).get(field)
        if isinstance(v, dict):
            return v.get("raw")
        return v

    out = {
        "pe": raw("summaryDetail", "trailingPE") or raw("defaultKeyStatistics", "trailingPE"),
        "market_cap": raw("price", "marketCap"),
        "fifty_two_week_high": raw("summaryDetail", "fiftyTwoWeekHigh"),
        "fcf_ttm": raw("financialData", "freeCashflow"),
        "ebitda_ttm": raw("financialData", "ebitda"),
        "total_debt": raw("financialData", "totalDebt"),
        "total_cash": raw("financialData", "totalCash"),
        "analyst_coverage": raw("financialData", "numberOfAnalystOpinions"),
        "recommendation_key": raw("financialData", "recommendationKey"),
        "gross_margin_pct": None,
        "operating_margin_pct": raw("financialData", "operatingMargins"),
        "revenue_growth_yoy_pct": raw("financialData", "revenueGrowth"),
    }
    if out["operating_margin_pct"] is not None:
        out["operating_margin_pct"] *= 100
    if out["revenue_growth_yoy_pct"] is not None:
        out["revenue_growth_yoy_pct"] *= 100
    gm = raw("financialData", "grossMargins")
    if gm is not None:
        out["gross_margin_pct"] = gm * 100

    # Ricavi/EBITDA trimestrali (fino a 4 trimestri) per la dislocazione e
    # per R&D/capex — non tutti i titoli espongono questo modulo.
    q_income = (data.get("incomeStatementHistoryQuarterly") or {}).get("incomeStatementHistory") or []
    quarters = []
    for q in q_income[:4]:
        quarters.append({
            "revenue": (q.get("totalRevenue") or {}).get("raw"),
            "ebit": (q.get("ebit") or {}).get("raw"),
            "rnd": (q.get("researchDevelopment") or {}).get("raw"),
        })
    out["quarters"] = quarters

    q_cash = (data.get("cashflowStatementHistoryQuarterly") or {}).get("cashflowStatements") or []
    capex_values = [
        (q.get("capitalExpenditures") or {}).get("raw")
        for q in q_cash[:4]
        if (q.get("capitalExpenditures") or {}).get("raw") is not None
    ]
    out["capex_ttm"] = sum(capex_values) if capex_values else None

    # Prossimo earnings (per il catalizzatore, regola 8)
    earnings_dates = ((data.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
    out["next_earnings_date"] = None
    if earnings_dates:
        ts = earnings_dates[0].get("raw") if isinstance(earnings_dates[0], dict) else None
        if ts:
            out["next_earnings_date"] = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")

    return out


def fetch_yahoo_history(ticker, rng="3y"):
    """Serie storica settimanale (leggera) per massimo 52w e rendimento a 3
    anni del Bottleneck Filter. Fonte singola Yahoo: se fallisce, i filtri
    che ne dipendono risultano "dato non disponibile", non "bocciati"."""
    _warm_yahoo_session()
    for base in ["query1", "query2"]:
        try:
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}"
            r = YAHOO_SESSION.get(url, params={"interval": "1wk", "range": rng}, timeout=12)
            if r.status_code != 200:
                continue
            result = r.json()["chart"]["result"][0]
            quote = result["indicators"]["quote"][0]
            closes = [c for c in quote.get("close", []) if c]
            if len(closes) >= 2:
                return closes
        except Exception as e:
            print(f"Yahoo history fallita per {ticker}: {e}")
    return None


_BOTTLENECK_MEM_CACHE = {}


def get_fundamentals_cached(ticker):
    """Cache 24h su DB (persiste tra riavvii/redeploy) + memoria di processo."""
    now = time.time()
    mem = _BOTTLENECK_MEM_CACHE.get(ticker)
    if mem and now - mem["at"] < config.BOTTLENECK_CACHE_TTL_SECONDS:
        return mem["data"]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_json, fetched_at FROM bottleneck_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row and now - row["fetched_at"] < config.BOTTLENECK_CACHE_TTL_SECONDS:
            fund = json.loads(row["data_json"])
            _BOTTLENECK_MEM_CACHE[ticker] = {"data": fund, "at": row["fetched_at"]}
            return fund

        fund = fetch_yahoo_fundamentals(ticker)
        market = fetch_market_data(ticker)
        history_3y = fetch_yahoo_history(ticker, "3y")
        high_52w = None
        return_3y_pct = None
        if history_3y:
            high_52w = max(history_3y[-52:]) if len(history_3y) >= 2 else None
            if history_3y[0]:
                return_3y_pct = (history_3y[-1] - history_3y[0]) / history_3y[0] * 100
        if fund and fund.get("fifty_two_week_high"):
            high_52w = fund["fifty_two_week_high"]  # dato Yahoo diretto, preferito se disponibile
        combined = {
            "fundamentals": fund,
            "market": market,
            "high_52w": high_52w,
            "return_3y_pct": return_3y_pct,
        }
        conn.execute(
            "INSERT INTO bottleneck_cache (ticker, data_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET data_json = excluded.data_json, fetched_at = excluded.fetched_at",
            (ticker, json.dumps(combined), now),
        )
        conn.commit()
        _BOTTLENECK_MEM_CACHE[ticker] = {"data": combined, "at": now}
        return combined
    finally:
        conn.close()


def _mk_filter(key, label, value, threshold, cmp, unit=""):
    """Costruisce il risultato di un filtro di Motore A. Se il valore è
    assente, lo stato è "missing" — mai "fail": un dato non disponibile
    non è una bocciatura (vedi prompt Bottleneck Filter)."""
    if value is None:
        status = "missing"
    elif cmp == "gte":
        status = "pass" if value >= threshold else "fail"
    elif cmp == "lte":
        status = "pass" if value <= threshold else "fail"
    else:  # "gt"
        status = "pass" if value > threshold else "fail"
    return {"key": key, "label": label, "status": status, "value": value, "threshold": threshold, "unit": unit}


def _score_from_bands(value, bands, default=0):
    """bands: lista di (soglia, punteggio) ordinata decrescente. Ritorna il
    punteggio della prima soglia raggiunta, None se value è None."""
    if value is None:
        return None
    for threshold, score in bands:
        if value >= threshold:
            return score
    return default


def _quarterly_ebit_yoy(quarters):
    """Variazione % dell'EBIT (proxy di EBITDA trimestrale, Yahoo non espone
    EBITDA per trimestro gratis) tra il trimestre più recente e quello di
    4 trimestri fa — approssima il confronto anno su anno."""
    if not quarters or len(quarters) < 4:
        return None
    recent, year_ago = quarters[0].get("ebit"), quarters[3].get("ebit")
    if recent is None or year_ago is None or year_ago == 0:
        return None
    return (recent - year_ago) / abs(year_ago) * 100


def compute_dislocation(drawdown_pct, revenue_yoy_pct, ebitda_yoy_pct):
    """Regola 4 (il filtro centrale): calo prezzo / calo peggiore tra ricavi
    ed EBITDA sugli ultimi 4 trimestri. Se i fondamentali crescono mentre il
    prezzo scende, punteggio massimo (99 = valore alto convenzionale per
    l'ordinamento, non infinito per restare serializzabile in JSON)."""
    if drawdown_pct is None or revenue_yoy_pct is None or ebitda_yoy_pct is None:
        return None
    worst = min(revenue_yoy_pct, ebitda_yoy_pct)
    if worst >= 0:
        return 99.0 if drawdown_pct > 0 else 0.0
    if drawdown_pct <= 0:
        return 0.0
    return drawdown_pct / abs(worst)


def _combine_engine_a_status(filters):
    statuses = [f["status"] for f in filters]
    fails = statuses.count("fail")
    if fails >= 2:
        return "ESCLUSO"
    if fails == 1:
        return "A_UN_FILTRO"
    if "missing" in statuses:
        return "DATI_INCOMPLETI"
    return "IDONEO"


def compute_engine_a(combined, thresholds):
    """Motore A: gli 8 filtri quantitativi che giudicano l'azienda. Mai
    mescolato col Motore B nel verdetto finale."""
    t = thresholds
    fund = combined.get("fundamentals") or {}
    market = combined.get("market") or {}
    price = market.get("price")
    high_52w = combined.get("high_52w")

    drawdown_pct = (high_52w - price) / high_52w * 100 if price and high_52w else None
    return_3y_pct = combined.get("return_3y_pct")
    pe = fund.get("pe")
    revenue_yoy = fund.get("revenue_growth_yoy_pct")
    ebitda_yoy = _quarterly_ebit_yoy(fund.get("quarters"))
    dislocation = compute_dislocation(drawdown_pct, revenue_yoy, ebitda_yoy)
    fcf = fund.get("fcf_ttm")

    net_debt_ebitda = None
    total_debt, total_cash, ebitda = fund.get("total_debt"), fund.get("total_cash"), fund.get("ebitda_ttm")
    if total_debt is not None and total_cash is not None and ebitda:
        net_debt_ebitda = (total_debt - total_cash) / ebitda

    coverage, rec = fund.get("analyst_coverage"), fund.get("recommendation_key")
    coverage_status = "missing"
    if coverage is not None:
        coverage_ok = coverage >= t["min_analyst_coverage"] and rec not in ("sell", "strong_sell")
        coverage_status = "pass" if coverage_ok else "fail"

    next_earnings = fund.get("next_earnings_date")
    catalyst_status = "missing"
    if next_earnings:
        try:
            days_to = (datetime.strptime(next_earnings, "%Y-%m-%d").date() - datetime.now().date()).days
            catalyst_status = "pass" if 0 <= days_to <= t["catalyst_window_days"] else "fail"
        except ValueError:
            pass

    filters = [
        _mk_filter("drawdown", "Drawdown da massimo 52 settimane", drawdown_pct, t["drawdown_min_pct"], "gte", "%"),
        _mk_filter("return_3y", "Rendimento 3 anni (tetto)", return_3y_pct, t["return_3y_max_pct"], "lte", "%"),
        _mk_filter("pe", "P/E (tetto)", pe, t["pe_max"], "lte", "x"),
        _mk_filter("dislocation", "Dislocazione prezzo/fondamentali", dislocation, t["dislocation_min"], "gte", "x"),
        _mk_filter("fcf", "Free cash flow TTM positivo", fcf, 0, "gt", "€"),
        _mk_filter("net_debt_ebitda", "Debito netto / EBITDA", net_debt_ebitda, t["net_debt_ebitda_max"], "lte", "x"),
        {"key": "analyst_coverage", "label": "Copertura analisti + consenso non Sell", "status": coverage_status,
         "value": f"{coverage} analisti, consenso {rec}" if coverage is not None else None,
         "threshold": f">= {t['min_analyst_coverage']} analisti, non Sell", "unit": ""},
        {"key": "catalyst", "label": "Catalizzatore (prossimi earnings) entro 3 mesi", "status": catalyst_status,
         "value": next_earnings, "threshold": f"entro {t['catalyst_window_days']} giorni", "unit": ""},
    ]
    return {"filters": filters, "status": _combine_engine_a_status(filters), "dislocation_value": dislocation}


def compute_engine_b(combined, thresholds):
    """Motore B: Bottleneck Filter personale, 5 sotto-punteggi 0-10.
    Nota metodologica onesta: "quota di mercato" e "massimo storico" non
    sono disponibili gratis in modo affidabile — bottleneckPurity usa solo
    il gross margin, hypeFactor usa il massimo delle ultime 52 settimane
    come proxy del massimo storico (non il vero all-time high)."""
    fund = combined.get("fundamentals") or {}
    market = combined.get("market") or {}
    price = market.get("price")
    high_52w = combined.get("high_52w")

    gm = fund.get("gross_margin_pct")
    om = fund.get("operating_margin_pct")
    quarters = fund.get("quarters") or []
    growth_pct = fund.get("revenue_growth_yoy_pct")

    rnd_pct = None
    if quarters and quarters[0].get("rnd") is not None and quarters[0].get("revenue"):
        rnd_pct = quarters[0]["rnd"] / quarters[0]["revenue"] * 100
    capex_pct = None
    capex_ttm = fund.get("capex_ttm")
    revenue_ttm = sum(q["revenue"] for q in quarters if q.get("revenue") is not None) or None
    if capex_ttm is not None and revenue_ttm:
        capex_pct = abs(capex_ttm) / revenue_ttm * 100

    margin_bands = [(70, 10), (60, 8), (50, 6), (40, 4), (30, 2)]
    supply_parts = [p for p in [
        _score_from_bands(rnd_pct, [(20, 10), (15, 8), (10, 6), (5, 4), (2, 2)]),
        _score_from_bands(capex_pct, [(15, 10), (10, 8), (7, 6), (4, 4), (2, 2)]),
    ] if p is not None]
    moat_parts = [p for p in [
        _score_from_bands(gm, margin_bands),
        _score_from_bands(om, [(30, 10), (25, 8), (20, 6), (15, 4), (10, 2)]),
    ] if p is not None]

    hype = None
    if price and high_52w:
        distance_pct = max(0.0, (high_52w - price) / high_52w * 100)
        hype = round(max(0.0, 10 - min(distance_pct, 100) / 10), 1)

    scores = {
        "bottleneckPurity": _score_from_bands(gm, margin_bands),
        "supplyConstraint": round(sum(supply_parts) / len(supply_parts), 1) if supply_parts else None,
        "growthDocumented": (round(max(0, min(10, growth_pct / thresholds["growth_min_pct"] * 10)), 1)
                              if growth_pct is not None else None),
        "moatStrength": round(sum(moat_parts) / len(moat_parts), 1) if moat_parts else None,
        "hypeFactor": hype,
    }

    missing = [k for k, v in scores.items() if v is None]
    if missing:
        return {"scores": scores, "total": None, "verdict": "DATI_INCOMPLETI", "missing": missing}

    total = round(sum(scores.values()), 1)
    hype_attendi_max = thresholds.get("hype_attendi_max", 7)
    if total >= thresholds["buy_score_min"] and scores["hypeFactor"] <= thresholds["hype_max"] \
            and (growth_pct or 0) >= thresholds["growth_min_pct"]:
        verdict = "COMPRA"
    elif total >= thresholds["watch_score_min"] or thresholds["hype_max"] < scores["hypeFactor"] <= hype_attendi_max:
        # ATTENDI anche con somma alta se l'hype è 5-7: "eccellente azienda,
        # entry non ancora giusto" — mai COMPRA solo perché il totale è alto.
        verdict = "ATTENDI"
    else:
        verdict = "PASSA"
    return {"scores": scores, "total": total, "verdict": verdict, "missing": []}


def compute_portfolio_constraints(ticker, sector, thresholds, owner=None):
    """Livello 3, sempre applicato DOPO i due motori e mai mescolato con
    essi: un titolo può essere idoneo sui dati e comunque sbagliato per il
    portafoglio corrente (troppo concentrato su quel titolo/settore, o
    scende sotto la soglia minima di difensivi)."""
    conn = get_db()
    try:
        if owner:
            rows = conn.execute(
                "SELECT * FROM tickers WHERE active = 1 AND qty > 0 AND (owner = ? OR owner = 'shared')",
                (owner,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tickers WHERE active = 1 AND qty > 0").fetchall()
    finally:
        conn.close()

    total = sector_value = defensive_value = ticker_value = 0.0
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get(row["ticker"])
        price = cached["price"] if cached and "error" not in cached else row["paid"]
        value = (price or 0) * (row["qty"] or 0)
        total += value
        r_sector = SCREENER_BY_TICKER.get(row["ticker"], {}).get("sector")
        if r_sector == sector:
            sector_value += value
        if r_sector in config.DEFENSIVE_SECTORS:
            defensive_value += value
        if row["ticker"] == ticker:
            ticker_value += value

    if total <= 0:
        return {"checks": [{"key": "portfolio_empty", "label": "Portafoglio vuoto o senza posizioni valorizzate",
                             "status": "missing", "value": None, "threshold": None}],
                "blocked": False, "blocked_by": []}

    checks = [{
        "key": "max_pct_per_stock", "label": "Max % per titolo",
        "status": "pass" if ticker_value / total * 100 <= thresholds["max_pct_per_stock"] else "fail",
        "value": round(ticker_value / total * 100, 1), "threshold": thresholds["max_pct_per_stock"],
    }]
    if sector:
        sector_pct = sector_value / total * 100
        checks.append({
            "key": "max_pct_per_sector", "label": f"Max % settore ({sector})",
            "status": "pass" if sector_pct <= thresholds["max_pct_per_sector"] else "fail",
            "value": round(sector_pct, 1), "threshold": thresholds["max_pct_per_sector"],
        })
    defensive_pct = defensive_value / total * 100
    checks.append({
        "key": "min_pct_defensive", "label": "Min % difensivi",
        "status": "pass" if defensive_pct >= thresholds["min_pct_defensive"] else "fail",
        "value": round(defensive_pct, 1), "threshold": thresholds["min_pct_defensive"],
    })
    blocked_by = [c["key"] for c in checks if c["status"] == "fail"]
    return {"checks": checks, "blocked": bool(blocked_by), "blocked_by": blocked_by}


def analyze_bottleneck(ticker, thresholds=None, owner=None):
    """Esegue entrambi i motori + il livello di portafoglio per un ticker,
    con le soglie correnti (default se non passate). Ritorna la struttura
    completa mostrata dalla card UI."""
    th = thresholds or config.BOTTLENECK_DEFAULTS
    combined = get_fundamentals_cached(ticker)
    if not combined or not (combined.get("market") or {}).get("price"):
        return {"ticker": ticker, "error": "Dati non disponibili per questo ticker"}

    spec = SCREENER_BY_TICKER.get(ticker, {})
    engine_a = compute_engine_a(combined, th["engine_a"])
    engine_b = compute_engine_b(combined, th["engine_b"])
    portfolio = compute_portfolio_constraints(ticker, spec.get("sector"), th["portfolio"], owner=owner)

    return {
        "ticker": ticker,
        "name": (combined.get("market") or {}).get("name", ticker),
        "price": (combined.get("market") or {}).get("price"),
        "sector": spec.get("sector"),
        "engine_a": engine_a,
        "engine_b": engine_b,
        "portfolio": portfolio,
        "blocked_by_level": (
            "motore_a" if engine_a["status"] == "ESCLUSO" else
            "motore_b" if engine_b["verdict"] == "PASSA" else
            "portafoglio" if portfolio["blocked"] else
            None
        ),
    }


def save_bottleneck_decision(result, thresholds):
    """Registro delle decisioni: salva il verdetto corrente per poter
    ricalcolare in futuro se aveva ragione (pagina /accuratezza)."""
    if result.get("error"):
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO bottleneck_decisions "
            "(ticker, verdict_a, verdict_b, engine_a_json, engine_b_json, thresholds_json, price_at_decision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                result["ticker"], result["engine_a"]["status"], result["engine_b"]["verdict"],
                json.dumps(result["engine_a"]), json.dumps(result["engine_b"]),
                json.dumps(thresholds), result["price"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recheck_bottleneck_decisions():
    """Job periodico (chiamato dal monitor e da /api/cron/tick): per ogni
    decisione salvata che ha superato 3/6/12 mesi e non è stata ancora
    ricontrollata a quella scadenza, registra il prezzo attuale. Alimenta
    /accuratezza — quali filtri escludono titoli che poi salgono davvero."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM bottleneck_decisions WHERE checked_3m = 0 OR checked_6m = 0 OR checked_12m = 0"
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now()
    for row in rows:
        try:
            created = datetime.strptime(row["created"].split(".")[0], "%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            continue
        age_days = (now - created).days
        updates = {}
        if age_days >= 90 and not row["checked_3m"]:
            updates["price_3m"], updates["checked_3m"] = _bottleneck_price_now(row["ticker"]), 1
        if age_days >= 180 and not row["checked_6m"]:
            updates["price_6m"], updates["checked_6m"] = _bottleneck_price_now(row["ticker"]), 1
        if age_days >= 365 and not row["checked_12m"]:
            updates["price_12m"], updates["checked_12m"] = _bottleneck_price_now(row["ticker"]), 1
        if not updates:
            continue
        conn = get_db()
        try:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE bottleneck_decisions SET {set_clause} WHERE id = ?",
                         (*updates.values(), row["id"]))
            conn.commit()
        finally:
            conn.close()


def _bottleneck_price_now(ticker):
    data = fetch_market_data(ticker)
    return data["price"] if data else None


def compute_bottleneck_accuracy():
    """Per ogni filtro di Motore A e per il verdetto di Motore B, tra le
    decisioni ricontrollate: quante volte ha escluso un titolo poi salito e
    quante volte uno poi sceso. Serve a capire quali filtri discriminano
    davvero (vedi pagina /accuratezza)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM bottleneck_decisions WHERE checked_3m = 1 OR checked_6m = 1 OR checked_12m = 1"
        ).fetchall()
    finally:
        conn.close()

    per_filter = {}  # filter_key -> {"escluso_salito": n, "escluso_sceso": n}

    def bump(key, went_up):
        stat = per_filter.setdefault(key, {"escluso_salito": 0, "escluso_sceso": 0})
        stat["escluso_salito" if went_up else "escluso_sceso"] += 1

    for row in rows:
        for horizon in ("3m", "6m", "12m"):
            if not row[f"checked_{horizon}"] or row[f"price_{horizon}"] is None or not row["price_at_decision"]:
                continue
            went_up = row[f"price_{horizon}"] > row["price_at_decision"]
            try:
                engine_a = json.loads(row["engine_a_json"])
            except (TypeError, ValueError):
                engine_a = {"filters": []}
            for f in engine_a.get("filters", []):
                if f["status"] == "fail":
                    bump(f["key"], went_up)
            if row["verdict_b"] == "PASSA":
                bump("motore_b_passa", went_up)

    out = []
    for key, stat in per_filter.items():
        n = stat["escluso_salito"] + stat["escluso_sceso"]
        out.append({
            "filter": key, "n": n,
            "escluso_salito": stat["escluso_salito"], "escluso_sceso": stat["escluso_sceso"],
            "pct_salito_dopo_esclusione": round(stat["escluso_salito"] / n * 100, 1) if n else None,
        })
    out.sort(key=lambda x: x["n"], reverse=True)

    suggestions = []
    for stat in out:
        if stat["n"] >= 5 and stat["pct_salito_dopo_esclusione"] and stat["pct_salito_dopo_esclusione"] >= 60:
            suggestions.append(
                f"Il filtro \"{stat['filter']}\" ha escluso titoli poi saliti nel "
                f"{stat['pct_salito_dopo_esclusione']}% dei casi ({stat['n']} osservazioni): "
                f"valuta di allentarne la soglia."
            )
    return {"per_filter": out, "suggestions": suggestions}


def compute_decision_accuracy():
    """Statistiche del Decision Engine: win rate e rendimento medio/mediano
    per le decisioni BUY/SELL già ricontrollate a 3/6/12 mesi. sample_size
    è sempre esplicito: sotto config.MIN_ACCURACY_SAMPLE_SIZE ritorna
    insufficient_sample invece di una percentuale che darebbe un falso
    senso di affidabilità."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE decision IN ('BUY', 'SELL') "
            "AND (checked_3m = 1 OR checked_6m = 1 OR checked_12m = 1)"
        ).fetchall()
    finally:
        conn.close()

    by_decision = {"BUY": [], "SELL": []}
    for row in rows:
        for horizon in ("3m", "6m", "12m"):
            ret = row[f"return_{horizon}"]
            if ret is not None:
                by_decision[row["decision"]].append(ret)

    def stats_for(returns, decision):
        n = len(returns)
        if n < config.MIN_ACCURACY_SAMPLE_SIZE:
            return {"sample_size": n, "insufficient_sample": True}
        wins = sum(1 for r in returns if (r > 0 if decision == "BUY" else r < 0))
        s = sorted(returns)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {
            "sample_size": n, "insufficient_sample": False,
            "win_rate_pct": round(wins / n * 100, 1),
            "avg_return_pct": round(sum(returns) / n, 2),
            "median_return_pct": round(median, 2),
        }

    return {
        "buy": stats_for(by_decision["BUY"], "BUY"),
        "sell": stats_for(by_decision["SELL"], "SELL"),
        "min_sample_size": config.MIN_ACCURACY_SAMPLE_SIZE,
    }


# --------------------------------------------------------------------------
# Cambio EUR/USD (per convertire i prezzi di mercato, quasi sempre in USD,
# nei valori in € mostrati da Trade Republic durante l'import da foto)
# --------------------------------------------------------------------------
_FX_CACHE = {"rate": None, "at": 0}
_FX_TTL_SECONDS = 3600


def get_eurusd_rate():
    """Quanti USD per 1 EUR. Cache di un'ora, fallback 1.08 se irraggiungibile
    (approssimazione dichiarata, meglio di bloccare l'import)."""
    now = time.time()
    if _FX_CACHE["rate"] and now - _FX_CACHE["at"] < _FX_TTL_SECONDS:
        return _FX_CACHE["rate"]
    data = fetch_yahoo("EURUSD=X")
    rate = data["price"] if data else 1.08
    _FX_CACHE["rate"] = rate
    _FX_CACHE["at"] = now
    return rate


def to_eur(amount, currency):
    if amount is None:
        return None
    if currency == "EUR":
        return amount
    return amount / get_eurusd_rate()


# --------------------------------------------------------------------------
# Commento AI opzionale (Google Gemini, free tier senza carta di credito)
# --------------------------------------------------------------------------
def generate_ai_commentary(ticker, result):
    """Sintesi in italiano generata da un LLM sopra ai dati tecnici già
    calcolati. Puramente opzionale: se GEMINI_API_KEY non è impostata non fa
    nessuna chiamata di rete e ritorna None senza rallentare nulla."""
    if not config.GEMINI_API_KEY:
        return None
    try:
        prompt = (
            "Sei un assistente che spiega in italiano, in 2-3 frasi semplici e dirette "
            "(niente disclaimer legali, niente ripetizioni), perché un titolo ha ricevuto "
            f"questo segnale di trading. Ticker: {ticker}. Segnale: {result['signal']} "
            f"(score {result['score']}/100). Prezzo: {result['price']} {result['currency']} "
            f"({result['day_chg']:+.1f}% oggi). RSI14: {result['rsi']}. MA50: {result['ma50']}. "
            f"MA200: {result['ma200']}. Distanza dal massimo 52 settimane: {result['dist_high52']}%. "
            f"Motivazioni tecniche già calcolate: {'; '.join(result['reasons']) or 'nessuna in particolare'}."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={config.GEMINI_API_KEY}"
        )
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        if r.status_code != 200:
            print(f"Gemini HTTP {r.status_code} per {ticker}: {r.text[:200]!r}")
            return None
        j = r.json()
        text = j["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip()
    except Exception as e:
        print(f"Gemini fallito per {ticker}: {e}")
        return None


def extract_portfolio_from_image(image_bytes, mime_type):
    """Manda una foto (es. screenshot Trade Republic) a Gemini Vision e torna
    una lista di posizioni estratte: [{ticker, name, value_eur, pnl_eur}].
    Richiede GEMINI_API_KEY. Non solleva mai eccezioni: torna [] se qualcosa
    va storto, con il dettaglio stampato nei log per debug."""
    if not config.GEMINI_API_KEY:
        return {"error": "Commento AI non configurato: manca GEMINI_API_KEY"}
    try:
        prompt = (
            "Questa immagine è uno screenshot di un'app di investimenti (es. Trade Republic) "
            "che mostra un elenco di posizioni in portafoglio. Per ogni posizione visibile, "
            "estrai: il ticker di borsa standard (es. AAPL, ASML, 8031.T per Mitsui Tokyo), "
            "il nome dell'azienda, il valore attuale della posizione in euro, e il "
            "guadagno/perdita mostrato (in euro se c'è il simbolo €, altrimenti in percentuale "
            "preceduta dal segno %). Rispondi SOLO con un array JSON valido, senza testo attorno, "
            "in questo formato esatto: "
            '[{"ticker": "AAPL", "name": "Apple", "value_eur": 1234.56, '
            '"pnl_eur": 12.30, "pnl_pct": null}]. '
            "Usa pnl_eur se il valore mostrato ha il simbolo €, altrimenti usa pnl_pct e lascia "
            "pnl_eur a null. Se non riesci a leggere un valore, usa null. Se non trovi nessuna "
            "posizione, rispondi con []."
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={config.GEMINI_API_KEY}"
        )
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                ]
            }]
        }
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code != 200:
            print(f"Gemini Vision HTTP {r.status_code}: {r.text[:300]!r}")
            return {"error": "L'AI non è riuscita a leggere l'immagine (errore di rete/quota)"}
        j = r.json()
        text = j["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Gemini a volte avvolge il JSON in ```json ... ``` nonostante il prompt: ripulisco.
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        positions = json.loads(text)
        if not isinstance(positions, list):
            return {"error": "Risposta AI non nel formato atteso"}
        return {"positions": positions}
    except json.JSONDecodeError as e:
        print(f"Gemini Vision: JSON non valido: {e}")
        return {"error": "L'AI ha risposto in un formato non leggibile, riprova con una foto più nitida"}
    except Exception as e:
        print(f"Gemini Vision fallito: {e}")
        return {"error": f"Errore durante la lettura della foto: {e}"}


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
    {"symbol": "NOW", "name": "ServiceNow Inc."},
    {"symbol": "PANW", "name": "Palo Alto Networks"},
    {"symbol": "CRWD", "name": "CrowdStrike Holdings"},
    {"symbol": "SNPS", "name": "Synopsys Inc."},
    {"symbol": "CDNS", "name": "Cadence Design Systems"},
    {"symbol": "ORCL", "name": "Oracle Corp."},
    # Crypto (formato Yahoo: TICKER-USD)
    {"symbol": "BTC-USD", "name": "Bitcoin"},
    {"symbol": "ETH-USD", "name": "Ethereum"},
    {"symbol": "SOL-USD", "name": "Solana"},
    {"symbol": "BNB-USD", "name": "BNB"},
    {"symbol": "XRP-USD", "name": "XRP"},
    {"symbol": "ADA-USD", "name": "Cardano"},
    {"symbol": "DOGE-USD", "name": "Dogecoin"},
    {"symbol": "AVAX-USD", "name": "Avalanche"},
    {"symbol": "LINK-USD", "name": "Chainlink"},
    {"symbol": "DOT-USD", "name": "Polkadot"},
]

# Universo di titoli "bottleneck" (monopoli/quasi-monopoli tecnologici) su
# cui gira lo screener automatico di mercato: la stessa logica di
# compute_signal(), applicata a candidati che NON sono ancora in
# portafoglio, per suggerire nuovi acquisti oltre ai titoli già tracciati.
BOTTLENECK_UNIVERSE = [
    "NVDA", "ORCL", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "ASML",
    "MU", "AMD", "ADBE", "CRM", "NOW", "PANW", "CRWD", "SNPS", "CDNS",
    "LRCX", "KLAC", "AMAT", "TXN", "QCOM", "INTC", "ARM", "MRVL", "NXPI",
    "STM", "ON", "TER", "ENTG", "IBM", "CSCO",
]

# Mappa ticker -> settore approssimativo, usata solo per il controllo di
# concentrazione nel verdetto giornaliero (vedi generate_daily_verdict).
# Non esaustiva: un ticker non in elenco viene contato come "Altro".
SECTOR_MAP = {
    "NVDA": "Tech", "ORCL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "GOOG": "Tech",
    "AMZN": "Tech", "META": "Tech", "AVGO": "Tech", "TSM": "Tech", "ASML": "Tech",
    "MU": "Tech", "AMD": "Tech", "ADBE": "Tech", "CRM": "Tech", "NOW": "Tech",
    "PANW": "Tech", "CRWD": "Tech", "SNPS": "Tech", "CDNS": "Tech", "LRCX": "Tech",
    "KLAC": "Tech", "AMAT": "Tech", "TXN": "Tech", "QCOM": "Tech", "INTC": "Tech",
    "ARM": "Tech", "MRVL": "Tech", "NXPI": "Tech", "STM": "Tech", "ON": "Tech",
    "TER": "Tech", "ENTG": "Tech", "IBM": "Tech", "CSCO": "Tech", "AAPL": "Tech",
    "SNDK": "Tech", "SMCI": "Tech", "SHOP": "Tech", "PYPL": "Tech", "SQ": "Tech",
    "SNOW": "Tech", "PLTR": "Tech", "UBER": "Tech", "ABNB": "Tech",
    "JNJ": "Difensivo/Healthcare", "PFE": "Difensivo/Healthcare", "UNH": "Difensivo/Healthcare",
    "KO": "Difensivo/Consumer", "PEP": "Difensivo/Consumer", "PG": "Difensivo/Consumer",
    "WMT": "Difensivo/Consumer", "COST": "Difensivo/Consumer", "MCD": "Difensivo/Consumer",
    "T": "Difensivo/Telecom", "VZ": "Difensivo/Telecom",
    "XOM": "Energia", "CVX": "Energia",
    "JPM": "Finanziari", "BAC": "Finanziari", "GS": "Finanziari", "MS": "Finanziari",
    "V": "Pagamenti", "MA": "Pagamenti", "AXP": "Pagamenti",
    "BA": "Industriali", "CAT": "Industriali", "GE": "Industriali", "LMT": "Difesa",
    "RTX": "Difesa", "BIP": "Infrastrutture", "8031": "Trading/Giappone",
    "BTC-USD": "Crypto", "ETH-USD": "Crypto", "SOL-USD": "Crypto",
}


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
        result["ai_commentary"] = generate_ai_commentary(ticker, result)
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


def send_telegram(text, chat_id=None):
    """Manda un messaggio sul bot Telegram configurato. Molto più semplice
    della mail: nessuna password per le app, consegna istantanea, niente
    filtro spam. Facoltativo: se non configurato, non fa nulla."""
    chat_id = chat_id or get_telegram_chat_id()
    if not config.TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"Errore invio Telegram: {e}")


def broadcast(subject, body):
    """Manda su tutti i canali configurati (mail e/o Telegram). Se nessuno è
    configurato non succede nulla, l'app continua a funzionare comunque."""
    send_mail(subject, body)
    send_telegram(f"{subject}\n\n{body}")


# notify_signal_change è stata rimossa: mandava un alert Telegram/mail
# indipendente basato solo sul segnale tecnico, in parallelo a
# notify_decision_change (Decision Engine) — potevano arrivare due
# messaggi diversi per lo stesso ticker (es. tecnico "BUY", Decision
# Engine "HOLD" per copertura dati insufficiente). Ora l'unica fonte di
# alert è il Decision Engine, l'unica decisione autorevole dell'app.


def notify_alert(ticker, condition, threshold, price):
    label = "sopra" if condition == "above" else "sotto"
    subject = f"🔔 CECCHINO ALERT: {ticker} {label} {threshold}"
    body = (
        f"Ticker: {ticker}\n"
        f"Condizione: prezzo {label} {threshold}\n"
        f"Prezzo attuale: {price}\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    broadcast(subject, body)


def notify_stop_raised(ticker, new_stop, high_water_mark):
    subject = f"📈 CECCHINO: {ticker} — stop loss alzato a {new_stop}"
    body = (
        f"Ticker: {ticker}\n"
        f"Nuovo massimo raggiunto: {high_water_mark}\n"
        f"Stop loss suggerito aggiornato: {new_stop} "
        f"(-{TRAILING_STOP_PCT * 100:.0f}% dal massimo)\n\n"
        "Il titolo è salito: alzare lo stop protegge il guadagno accumulato "
        "senza doverlo decidere ogni volta a mano.\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    broadcast(subject, body)


def notify_stop_triggered(ticker, stop_level, price):
    subject = f"⚠️ CECCHINO STOP LOSS: {ticker} sotto {stop_level}"
    body = (
        f"Ticker: {ticker}\n"
        f"Prezzo attuale: {price}\n"
        f"Stop loss suggerito: {stop_level}\n\n"
        "Il prezzo ha rotto la soglia di protezione: valuta di vendere per "
        "limitare le perdite o proteggere il guadagno accumulato.\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    broadcast(subject, body)


# --------------------------------------------------------------------------
# Stop loss dinamico (trailing stop)
# --------------------------------------------------------------------------
# Percentuale sotto il massimo storico raggiunto dall'acquisto: valore fisso
# semplice e trasparente (non un parametro nascosto), tipico di uno stop a
# trailing usato dai trader retail per posizioni "buy and hold" azionarie.
TRAILING_STOP_PCT = 0.10


def check_trailing_stop(conn, ticker, price):
    """Solo per titoli effettivamente posseduti (qty > 0): aggiorna il
    massimo storico dall'acquisto e lo stop-loss a trailing (10% sotto il
    massimo). Avvisa quando lo stop sale di almeno il 3% (per proteggere
    un guadagno crescente senza spam) o quando il prezzo lo rompe."""
    row = conn.execute(
        "SELECT qty, high_water_mark, stop_notified_level, stop_triggered "
        "FROM tickers WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if row is None or not row["qty"]:
        return None

    high_water_mark = max(row["high_water_mark"] or 0, price)
    stop_level = round(high_water_mark * (1 - TRAILING_STOP_PCT), 2)
    stop_notified_level = row["stop_notified_level"] or 0
    stop_triggered = row["stop_triggered"] or 0

    conn.execute("UPDATE tickers SET high_water_mark = ? WHERE ticker = ?", (high_water_mark, ticker))
    conn.commit()

    if price <= stop_level:
        if not stop_triggered:
            conn.execute("UPDATE tickers SET stop_triggered = 1 WHERE ticker = ?", (ticker,))
            conn.commit()
            notify_stop_triggered(ticker, stop_level, price)
    else:
        if stop_triggered:
            conn.execute("UPDATE tickers SET stop_triggered = 0 WHERE ticker = ?", (ticker,))
            conn.commit()
        if stop_notified_level == 0 or stop_level >= stop_notified_level * 1.03:
            conn.execute(
                "UPDATE tickers SET stop_notified_level = ? WHERE ticker = ?", (stop_level, ticker)
            )
            conn.commit()
            if stop_notified_level > 0:  # non avvisare al primo calcolo in assoluto
                notify_stop_raised(ticker, stop_level, high_water_mark)

    return stop_level


# --------------------------------------------------------------------------
# Storico segnali + Alert
# --------------------------------------------------------------------------
def record_signal_if_changed(conn, ticker, result):
    """Registra il segnale tecnico nello storico se è cambiato — resta solo
    un log, non genera più un alert autonomo (vedi nota sopra
    notify_signal_change): l'unico alert per un ticker è quello del
    Decision Engine, così Telegram non può mai dire una cosa diversa da
    quella mostrata nell'app."""
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
        result["stop_loss"] = check_trailing_stop(conn, ticker, result["price"])
    finally:
        conn.close()
    return result


# --------------------------------------------------------------------------
# Watchlist con soglie ingresso/stop/target (tab "🎯 Livelli")
# --------------------------------------------------------------------------
WATCH_EMOJI = {"entry": "🟢", "stop": "🔴", "target": "🎯"}
WATCH_LABEL = {"entry": "SEGNALE INGRESSO", "stop": "ALERT STOP", "target": "PROFIT TARGET"}
WATCH_ACTION = {"entry": "COMPRA", "stop": "VENDI", "target": "VENDI (prendi profitto)"}


def _fmt_range(low, high):
    if low is not None and high is not None and low != high:
        return f"{low}–{high}"
    if high is not None:
        return str(high)
    if low is not None:
        return f">{low}"
    return "—"


def seed_watch_levels():
    """Inserisce le righe di config.WATCH_LEVELS non ancora presenti,
    risolvendo stop%/target-moltiplicatore in € concreti. Chiamata ad ogni
    giro del monitor: è idempotente, se un ticker richiede un prezzo live
    (es. NOK "a mercato") e la rete fallisce ora, riprova al giro dopo
    invece di bloccare l'avvio dell'app."""
    conn = get_db()
    try:
        existing = {r["ticker"] for r in conn.execute("SELECT ticker FROM watch_levels").fetchall()}
    finally:
        conn.close()

    for spec in config.WATCH_LEVELS:
        ticker = spec["ticker"]
        if ticker in existing:
            continue

        entry_low = spec.get("entry_low")
        entry_high = spec.get("entry_high")
        stop_price = spec.get("stop_price")
        target_low = spec.get("target_low")
        target_high = spec.get("target_high")
        entry_at_market = spec.get("entry_at_market", False)

        reference_price = entry_high if entry_high is not None else entry_low
        if entry_at_market or reference_price is None:
            data = fetch_market_data(ticker)
            if not data:
                print(f"Watchlist: prezzo non disponibile per {ticker}, riprovo al prossimo giro")
                continue
            reference_price = to_eur(data["price"], data.get("currency", "USD"))

        if stop_price is None and spec.get("stop_pct") is not None:
            stop_price = round(reference_price * (1 + spec["stop_pct"]), 2)
        if target_low is None and spec.get("target_multiple") is not None:
            target_low = target_high = round(reference_price * spec["target_multiple"], 2)

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO watch_levels
                    (ticker, name, entry_low, entry_high, stop_price, target_low,
                     target_high, reference_price, entry_notified, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    ticker, spec.get("name", ticker), entry_low, entry_high, stop_price,
                    target_low, target_high, round(reference_price, 2),
                    1 if entry_at_market else 0,  # "a mercato" = già considerato entrato
                ),
            )
            conn.commit()
        finally:
            conn.close()


def notify_watch_signal(row, signal_type, price):
    ticker = row["ticker"]
    name = row["name"] or ticker
    ref = row["reference_price"]
    move_txt = f" ({(price - ref) / ref * 100:+.1f}% dal riferimento {ref})" if ref else ""

    subject = f"{WATCH_EMOJI[signal_type]} {WATCH_LABEL[signal_type]}: {ticker} a {price:.2f}€"
    body = (
        f"{ticker} ({name})\n"
        f"Prezzo attuale: {price:.2f}€{move_txt}\n"
        f"Segnale: {WATCH_LABEL[signal_type]}\n"
        f"Azione consigliata: {WATCH_ACTION[signal_type]}\n\n"
        f"Ingresso: {_fmt_range(row['entry_low'], row['entry_high'])}\n"
        f"Stop: {row['stop_price']}\n"
        f"Target: {_fmt_range(row['target_low'], row['target_high'])}\n\n"
        f"Apri Cecchino: {config.PUBLIC_URL}"
    )
    broadcast(subject, body)

    conn = get_db()
    conn.execute(
        "INSERT INTO watch_log (ticker, signal_type, price, message) VALUES (?, ?, ?, ?)",
        (ticker, signal_type, price, body),
    )
    conn.commit()
    conn.close()


def check_watch_levels():
    """Confronta il prezzo live di ogni riga della watchlist con le sue
    soglie e manda gli alert 🟢/🔴/🎯. Aggiorna sempre WATCHLIST_CACHE per
    la dashboard, anche quando nessun segnale scatta."""
    seed_watch_levels()

    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM watch_levels WHERE active = 1 ORDER BY ticker").fetchall()
    finally:
        conn.close()

    for row in rows:
        try:
            data = fetch_market_data(row["ticker"])
            if not data:
                continue
            price = to_eur(data["price"], data.get("currency", "USD"))
            if price is None:
                continue

            entry_low, entry_high = row["entry_low"], row["entry_high"]
            in_entry_zone = False
            if entry_low is not None and entry_high is not None:
                in_entry_zone = entry_low <= price <= entry_high
            elif entry_high is not None:
                in_entry_zone = price <= entry_high

            status = "IN ATTESA"
            if row["stop_price"] is not None and price <= row["stop_price"]:
                status = "STOP"
            elif row["target_low"] is not None and price >= row["target_low"]:
                status = "TARGET"
            elif in_entry_zone:
                status = "INGRESSO"

            with CACHE_LOCK:
                WATCHLIST_CACHE[row["ticker"]] = {
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "price": round(price, 2),
                    "entry_low": entry_low,
                    "entry_high": entry_high,
                    "stop_price": row["stop_price"],
                    "target_low": row["target_low"],
                    "target_high": row["target_high"],
                    "reference_price": row["reference_price"],
                    "status": status,
                    "dist_to_entry_pct": round((entry_high - price) / price * 100, 1) if entry_high else None,
                    "dist_to_stop_pct": round((price - row["stop_price"]) / row["stop_price"] * 100, 1) if row["stop_price"] else None,
                    "dist_to_target_pct": round((row["target_low"] - price) / price * 100, 1) if row["target_low"] else None,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                }

            conn = get_db()
            try:
                if row["stop_price"] is not None and price <= row["stop_price"] and not row["stop_notified"]:
                    notify_watch_signal(row, "stop", price)
                    conn.execute("UPDATE watch_levels SET stop_notified = 1 WHERE id = ?", (row["id"],))
                elif row["target_low"] is not None and price >= row["target_low"] and not row["target_notified"]:
                    notify_watch_signal(row, "target", price)
                    conn.execute("UPDATE watch_levels SET target_notified = 1 WHERE id = ?", (row["id"],))
                elif in_entry_zone and not row["entry_notified"]:
                    notify_watch_signal(row, "entry", price)
                    conn.execute("UPDATE watch_levels SET entry_notified = 1 WHERE id = ?", (row["id"],))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"Errore watchlist per {row['ticker']}: {e}")


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


def run_decision_engine_for_portfolio():
    """Applica il Decision Engine ad ogni ticker tracciato (tabella
    tickers, active=1). Non ricalcola più spesso di
    config.DECISION_ENGINE_MIN_INTERVAL_MINUTES per ticker, anche se questa
    funzione viene chiamata ogni 10 minuti dal tick: evita di interrogare
    inutilmente i provider quando i dati non possono essere cambiati."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT ticker FROM tickers WHERE active = 1").fetchall()
    finally:
        conn.close()

    for row in rows:
        ticker = row["ticker"]
        conn = get_db()
        try:
            last = conn.execute(
                "SELECT ts FROM decisions WHERE ticker = ? ORDER BY ts DESC LIMIT 1", (ticker,)
            ).fetchone()
        finally:
            conn.close()
        if last:
            try:
                last_ts = datetime.strptime(last["ts"].split(".")[0], "%Y-%m-%d %H:%M:%S")
                age_minutes = (datetime.now() - last_ts).total_seconds() / 60
                if age_minutes < config.DECISION_ENGINE_MIN_INTERVAL_MINUTES:
                    continue
            except (ValueError, TypeError):
                pass
        try:
            evaluate_decision(ticker)
        except Exception as e:
            print(f"Errore Decision Engine per {ticker}: {e}")


def notify_opportunities(results):
    subject = f"💡 CECCHINO: {len(results)} opportunità sul mercato oggi"
    blocks = []
    for r in results:
        reasons_txt = "; ".join(r["reasons"])
        blocks.append(
            f"{r['ticker']} ({r.get('name', '')}) — {r['signal']} score {r['score']}\n"
            f"Prezzo: {r['price']} {r['currency']}\n{reasons_txt}"
        )
    body = (
        "Titoli fuori dal tuo portafoglio con segnale BUY forte oggi "
        "(universo bottleneck: monopoli/quasi-monopoli tech):\n\n"
        + "\n\n".join(blocks)
        + f"\n\nApri Cecchino: {config.PUBLIC_URL}"
    )
    broadcast(subject, body)


def run_market_screener(send_email=True):
    """Scansiona BOTTLENECK_UNIVERSE (titoli non già in portafoglio) e tiene
    i migliori segnali BUY (score >= 40). Aggiorna sempre la cache per la UI;
    manda una mail digest al massimo una volta al giorno per non spammare."""
    conn = get_db()
    try:
        active_tickers = {
            r["ticker"] for r in conn.execute("SELECT ticker FROM tickers WHERE active = 1").fetchall()
        }
    finally:
        conn.close()

    candidates = []
    for ticker in BOTTLENECK_UNIVERSE:
        if ticker in active_tickers:
            continue
        try:
            result = analyze_ticker(ticker)
        except Exception as e:
            print(f"Errore screener per {ticker}: {e}")
            continue
        if "error" not in result and result["score"] >= 40:
            candidates.append(result)

    candidates.sort(key=lambda r: r["score"], reverse=True)
    top = candidates[:5]

    with CACHE_LOCK:
        SCREENER_CACHE["results"] = top
        SCREENER_CACHE["updated"] = datetime.now().isoformat(timespec="seconds")

    if send_email and top:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = get_db()
        try:
            row = conn.execute("SELECT last_screener_sent FROM settings WHERE id = 1").fetchone()
            already_sent = row and row["last_screener_sent"] == today
            if not already_sent:
                notify_opportunities(top)
                conn.execute(
                    "INSERT INTO settings (id, last_screener_sent) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET last_screener_sent = excluded.last_screener_sent",
                    (today,),
                )
                conn.commit()
        finally:
            conn.close()

    return top


# --------------------------------------------------------------------------
# Screener settimanale a 25 titoli con regole operative (tab "📅 Settimanale")
# --------------------------------------------------------------------------
def analyze_for_screener(ticker):
    """Prezzo/RSI/medie/variazioni per il motore a regole. A differenza di
    analyze_ticker() calcola anche la variazione a 5 giorni (regola 1,
    anti-inseguimento) e non converte in €: qui i livelli sono in valuta
    nativa (USD per quasi tutti), come scritti dall'utente."""
    data = fetch_market_data(ticker)
    if not data or len(data["closes"]) < 6:
        return None
    closes = data["closes"]
    price = data["price"]
    rsi = compute_rsi(closes, 14)
    ma50 = statistics.mean(closes[-50:])
    ma200 = statistics.mean(closes[-200:]) if len(closes) >= 200 else statistics.mean(closes)
    chg_1d = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
    chg_5d = (closes[-1] - closes[-6]) / closes[-6] * 100 if closes[-6] else 0
    return {
        "ticker": ticker, "name": data["name"], "price": round(price, 2),
        "currency": data.get("currency", "USD"), "rsi": round(rsi, 1),
        "ma50": round(ma50, 2), "ma200": round(ma200, 2),
        "chg_1d": round(chg_1d, 1), "chg_5d": round(chg_5d, 1),
    }


def _screener_entry_zone_text(spec):
    lo, hi = spec.get("entry_low"), spec.get("entry_high")
    if lo is not None and hi is not None:
        return f"${lo}-{hi}"
    if hi is not None:
        return f"sotto ${hi}"
    if lo is not None:
        return f"sopra ${lo}"
    return spec.get("entry_note", "n/d")


def in_screener_entry_zone(spec, price):
    """Zona di ingresso ± 3% (richiesto dal prompt). Se il livello è solo
    una nota testuale (es. "su ritracciamento"), non c'è modo affidabile di
    verificarlo a numeri: mai BUY automatico in quel caso, resta WATCH."""
    lo, hi = spec.get("entry_low"), spec.get("entry_high")
    if lo is not None and hi is not None:
        return lo * 0.97 <= price <= hi * 1.03
    if hi is not None:
        return price <= hi * 1.03
    if lo is not None:
        return price >= lo * 0.97
    return False


# Parole chiave (italiano + inglese) che indicano un possibile evento reale
# di rottura tesi, usate quando GEMINI_API_KEY non è configurata (zero AI).
# Motore notizie deterministico (zero AI): NEWS_RULES in config.py associa
# ogni tipo di evento a una severità 0-10 fissa e a un elenco di parole
# chiave. Più preciso di un semplice sì/no: restituisce severità e
# confidenza calcolate, non un giudizio testuale generato da un modello.


def _fetch_recent_news(ticker, window_hours=48):
    """Notizie Yahoo per il ticker, filtrate alle ultime `window_hours`.
    Fonte unica gratuita: se fallisce o non trova nulla, torna lista vuota
    (mai un'eccezione) — un provider assente è "nessuna notizia disponibile",
    non un errore che blocca il resto della decisione."""
    try:
        _warm_yahoo_session()
        url = "https://query1.finance.yahoo.com/v1/finance/search"
        r = YAHOO_SESSION.get(url, params={"q": ticker, "quotesCount": 0, "newsCount": 8}, timeout=8)
        if r.status_code != 200:
            return []
        news = r.json().get("news", [])
    except Exception as e:
        print(f"Ricerca notizie fallita per {ticker}: {e}")
        return []

    cutoff = time.time() - window_hours * 3600
    recent = [n for n in news if (n.get("providerPublishTime") or 0) >= cutoff]
    return recent if recent else news  # providerPublishTime assente: valuta comunque i titoli trovati


def _has_negation_before(title_l, kw_pos):
    """Regola 6 del motore notizie: una negazione entro le 4 parole prima
    della frase chiave ("denies fraud allegations", "rules out bankruptcy")
    riduce la confidenza — non è la stessa cosa di un evento confermato."""
    prefix = title_l[:kw_pos]
    prefix_words = prefix.split()[-4:]
    return any(neg.strip() in " ".join(prefix_words) for neg in config.NEWS_NEGATION_WORDS)


def classify_news_item(title, publisher=None):
    """Confronta un titolo con config.NEWS_RULES. Ritorna il match di
    severità più alta trovato (un titolo può citare più regole), con
    confidenza calcolata deterministicamente da: affidabilità della fonte,
    numero di regole corrispondenti, presenza di negazioni. Nessuna
    chiamata esterna, nessuna AI: stesso input, stesso output sempre."""
    title_l = (title or "").lower()
    matches = []
    for event_type, rule in config.NEWS_RULES.items():
        for kw in rule["keywords"]:
            pos = title_l.find(kw)
            if pos == -1:
                continue
            negated = _has_negation_before(title_l, pos)
            matches.append({
                "event_type": event_type,
                "severity": rule["severity"],
                "keyword": kw,
                "negated": negated,
            })
            break  # una keyword per regola basta, evita doppi conteggi sulla stessa regola

    confirmed = [m for m in matches if not m["negated"]]
    if not confirmed:
        return None

    best = max(confirmed, key=lambda m: m["severity"])
    source_reliable = publisher in config.NEWS_RELIABLE_PUBLISHERS
    confidence = 0.55 if not source_reliable else 0.8
    confidence += min(0.15, 0.05 * (len(confirmed) - 1))  # più regole confermate, più confidenza
    if any(m["negated"] for m in matches) and len(confirmed) == len(matches):
        pass  # nessuna negazione tra i match confermati, nulla da penalizzare
    confidence = round(min(confidence, 1.0), 2)

    return {
        "event_type": best["event_type"],
        "severity": best["severity"],
        "direction": config.NEWS_RULES[best["event_type"]]["direction"],
        "confidence": confidence,
        "matched_rules": sorted({m["event_type"] for m in confirmed}),
    }


def _news_severity_label(severity):
    for threshold, label in config.NEWS_SEVERITY_LABELS:
        if severity >= threshold:
            return label
    return "informational"


def _normalize_headline(title):
    """Chiave di deduplica: stessa notizia ripresa da fonti diverse (o
    indicizzata due volte da Yahoo) non deve contare come due eventi."""
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def assess_news(ticker):
    """Funzione canonica del News Engine: recupera le notizie recenti,
    classifica ogni titolo con classify_news_item, deduplica per titolo
    normalizzato, e ritorna sia l'evento di severità più alta (usato dal
    Decision Engine per lo scoring) sia la lista completa degli eventi
    classificati (usata dal pannello News Intelligence) — o severity 0/
    lista vuota se nessun evento rilevante, MAI un dato mancante silenzioso.
    Logga ogni evento nuovo (per url) in news_events per lo storico."""
    items = _fetch_recent_news(ticker)
    seen_headlines = set()
    events = []
    for n in items:
        title = n.get("title") or ""
        key = _normalize_headline(title)
        if not key or key in seen_headlines:
            continue
        publisher = n.get("publisher")
        classified = classify_news_item(title, publisher)
        if not classified:
            continue
        seen_headlines.add(key)
        reliable = publisher in config.NEWS_RELIABLE_PUBLISHERS
        verification = (
            "VERIFIED" if reliable and classified["confidence"] >= 0.7
            else "UNVERIFIED" if classified["confidence"] < 0.6
            else "SECONDARY"
        )
        events.append({
            **classified,
            "source": publisher,
            "published_at": n.get("providerPublishTime"),
            "headline": title,
            "url": n.get("link"),
            "severity_label": _news_severity_label(classified["severity"]),
            "verification_status": verification,
        })

    events.sort(key=lambda e: (e["severity"], e["published_at"] or 0), reverse=True)

    # "best" — usato per lo scoring del rischio e per il trigger SELL della
    # regola 3 dello Settimanale — considera SOLO eventi negativi: un major
    # contract o una guidance raise non devono mai alzare il punteggio di
    # rischio né essere scambiati per un "evento di rottura tesi".
    negative_events = [e for e in events if e["direction"] == "negative"]
    best = negative_events[0] if negative_events else None

    if best is None:
        return {
            "event_type": None, "severity": 0, "direction": None, "confidence": 1.0,
            "source": None, "published_at": None, "matched_rules": [], "headline": None,
            "url": None, "severity_label": "informational", "events": events[:10],
        }

    if best.get("url"):
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO news_events "
                "(ticker, url, title, publisher, event_type, severity, confidence, published_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, best["url"], best["headline"], best["source"], best["event_type"],
                 best["severity"], best["confidence"], best["published_at"]),
            )
            conn.commit()
        finally:
            conn.close()
    return {**best, "events": events[:10]}


def _check_recent_event_ai(ticker, news):
    """Percorso AI (Gemini): capisce il contesto delle notizie, non solo le
    parole. Usato solo se GEMINI_API_KEY è configurata."""
    try:
        headlines = "\n".join(f"- {n.get('title', '')}" for n in news[:5])
        prompt = (
            f"Notizie più recenti trovate per il titolo {ticker}:\n{headlines}\n\n"
            'Rispondi SOLO con JSON: {"is_break": true/false, "description": "..."}. '
            "is_break=true SOLO se una di queste notizie descrive un evento concreto di "
            "rottura della tesi d'investimento: earnings mancati, guidance tagliata, "
            "downgrade multiplo di analisti, causa legale grave, cambio CEO improvviso "
            "negativo. Notizie generiche, movimenti di prezzo, opinioni o rumor NON "
            "contano: in quel caso is_break=false."
        )
        gurl = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={config.GEMINI_API_KEY}"
        )
        gr = requests.post(gurl, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        if gr.status_code != 200:
            return None
        text = gr.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        return parsed if parsed.get("is_break") else None
    except Exception as e:
        print(f"Errore check_recent_event (AI) per {ticker}: {e}")
        return None


def _check_recent_event_keywords(ticker):
    """Percorso deterministico (zero AI) per la regola 3 dello Settimanale:
    riusa assess_news e considera "evento di rottura tesi" solo la severità
    "serious" o superiore (>=7 su 10) — allineato alla stessa soglia
    prudente richiesta dalla regola 3 (mai SELL da rumore generico)."""
    news = assess_news(ticker)
    if news["severity"] >= 7:
        return {"is_break": True, "description": news["headline"], "matched_keyword": news["event_type"]}
    return None


def check_recent_event(ticker):
    """Best-effort (regola 3): cerca le notizie più recenti su Yahoo delle
    ultime 48h. Se GEMINI_API_KEY è configurata usa Gemini per capire il
    contesto; altrimenti (zero AI) usa il News Engine deterministico
    (config.NEWS_RULES) tramite assess_news. Se non si trovano notizie o
    qualcosa fallisce, ritorna None: nessun evento confermato, che blocca
    il SELL — la scelta sicura richiesta esplicitamente dalla regola 3,
    non un'approssimazione pigra."""
    if config.GEMINI_API_KEY:
        news = _fetch_recent_news(ticker)
        if not news:
            return None
        return _check_recent_event_ai(ticker, news)
    return _check_recent_event_keywords(ticker)


# --------------------------------------------------------------------------
# Decision Engine (config.DECISION_ENGINE_VERSION) — unisce motore tecnico +
# Motore A (fondamentale) + Motore B (bottleneck) + motore notizie in
# un'unica decisione BUY/HOLD/SELL/DATA_UNAVAILABLE. Sempre deterministico:
# nessuna chiamata AI, formula e soglie fisse e versionate in config.py.
# Alert Telegram/mail solo quando la decisione CAMBIA rispetto all'ultima
# registrata per quel ticker (mai un alert ripetuto a parità di stato).
# --------------------------------------------------------------------------
def _technical_reason_codes(result):
    """Ricostruisce codici motivazione strutturati dalle stesse soglie già
    usate in compute_signal, senza duplicarne la logica di scoring (che
    resta lì, intoccata, per non rischiare di rompere Scanner/Portafoglio
    che la usano da sempre)."""
    codes = []
    rsi, price = result.get("rsi"), result.get("price")
    ma50, ma200 = result.get("ma50"), result.get("ma200")
    dist_high52, day_chg = result.get("dist_high52"), result.get("day_chg")
    vol_ratio = result.get("vol_ratio")

    if rsi is not None:
        if rsi < 30:
            codes.append("T-RSI-OVERSOLD-STRONG")
        elif rsi < 40:
            codes.append("T-RSI-OVERSOLD")
        elif rsi > 75:
            codes.append("T-RSI-OVERBOUGHT-STRONG")
        elif rsi > 65:
            codes.append("T-RSI-OVERBOUGHT")
    if price is not None and ma50 is not None:
        codes.append("T-ABOVE-MA50" if price > ma50 else "T-BELOW-MA50")
    if price is not None and ma200 is not None:
        codes.append("T-ABOVE-MA200" if price > ma200 else "T-BELOW-MA200")
    if dist_high52 is not None:
        if dist_high52 < -25:
            codes.append("T-DEEP-DISCOUNT-52W")
        elif dist_high52 < -15:
            codes.append("T-DISCOUNT-52W")
        elif dist_high52 > -3:
            codes.append("T-NEAR-52W-HIGH")
    if day_chg is not None:
        if day_chg > 12:
            codes.append("T-SPIKE-UP")
        elif day_chg < -10:
            codes.append("T-DIP-DOWN")
    if vol_ratio is not None and vol_ratio > 1.8 and day_chg is not None:
        if day_chg > 2:
            codes.append("T-VOLUME-CONFIRM-UP")
        elif day_chg < -2:
            codes.append("T-VOLUME-CONFIRM-DOWN")
    return codes


def _fundamental_score_from_engine_a(engine_a):
    """0-100: quota di filtri Motore A passati sul totale valutabile (i
    "missing" non contano né a favore né contro — dato non disponibile
    non è una bocciatura, stessa regola del Bottleneck Filter)."""
    statuses = [f["status"] for f in engine_a["filters"]]
    evaluable = [s for s in statuses if s != "missing"]
    if not evaluable:
        return None, []
    score = evaluable.count("pass") / len(evaluable) * 100
    codes = [f"F-{f['key'].upper()}-FAIL" for f in engine_a["filters"] if f["status"] == "fail"]
    return round(score, 1), codes


def _bottleneck_score_from_engine_b(engine_b):
    """0-100: il totale Motore B (0-50) riscalato. None se dati incompleti."""
    if engine_b["total"] is None:
        return None, []
    return round(engine_b["total"] * 2, 1), [f"B-VERDICT-{engine_b['verdict']}"]


def _news_score_from_assessment(news):
    """0-100: severità*10. 0 = nessun evento rilevante trovato (non è un
    dato mancante: assess_news controlla sempre, torna severità 0 se non
    trova nulla di classificabile)."""
    score = news["severity"] * 10
    codes = [f"N-{news['event_type'].upper()}-SEV{news['severity']}"] if news["event_type"] else []
    return score, codes


def evaluate_decision(ticker):
    """Calcola e registra la decisione unica per un ticker. Riusa
    analyze_ticker (motore tecnico), analyze_bottleneck (Motore A+B) e
    assess_news (motore notizie) — nessuna nuova chiamata di rete qui
    dentro, solo aggregazione secondo la formula in config.DECISION_WEIGHTS."""
    data_sources = {}

    technical_result = analyze_ticker(ticker)
    data_sources["price"] = "error" not in technical_result
    if "error" in technical_result:
        layers = {
            "technical": {"score": None, "codes": [], "raw": technical_result},
            "fundamental": {"score": None, "codes": [], "raw": None},
            "bottleneck": {"score": None, "codes": [], "raw": None},
            "news": {"score": None, "codes": [], "raw": None},
        }
        return _finalize_decision(ticker, None, layers, data_sources,
                                   decision_override="DATA_UNAVAILABLE",
                                   override_codes=["DATA-UNAVAILABLE-PRICE"])

    price = technical_result["price"]
    technical_layer = {
        "score": (technical_result["score"] + 100) / 2,
        "codes": _technical_reason_codes(technical_result),
        "raw": {k: technical_result.get(k) for k in
                ("rsi", "ma50", "ma200", "dist_high52", "dist_low52", "day_chg", "vol_ratio", "score", "signal")},
    }

    bottleneck_result = analyze_bottleneck(ticker)
    data_sources["fundamentals"] = not bool(bottleneck_result.get("error"))
    if bottleneck_result.get("error"):
        fundamental_layer = {"score": None, "codes": [], "raw": None}
        bottleneck_layer = {"score": None, "codes": [], "raw": None}
    else:
        f_score, f_codes = _fundamental_score_from_engine_a(bottleneck_result["engine_a"])
        b_score, b_codes = _bottleneck_score_from_engine_b(bottleneck_result["engine_b"])
        fundamental_layer = {"score": f_score, "codes": f_codes, "raw": bottleneck_result["engine_a"]}
        bottleneck_layer = {"score": b_score, "codes": b_codes, "raw": bottleneck_result["engine_b"]}

    news = assess_news(ticker)
    data_sources["news"] = True  # assess_news non fallisce mai: severità 0 se non trova nulla
    n_score, n_codes = _news_score_from_assessment(news)
    news_layer = {"score": n_score, "codes": n_codes, "raw": news}

    layers = {"technical": technical_layer, "fundamental": fundamental_layer,
              "bottleneck": bottleneck_layer, "news": news_layer}
    result = _finalize_decision(ticker, price, layers, data_sources)
    result["news_events"] = news.get("events", [])

    # Sezione 28: lo Screener Settimanale (7 regole, indipendente) resta il
    # secondo parere. Se il ticker è nella sua lista, mostralo sempre a
    # fianco del Decision Engine — mai nasconderlo in caso di conflitto.
    spec = SCREENER_BY_TICKER.get(ticker)
    if spec:
        result["weekly_screener"] = {
            "category": spec["category"],
            "fcf_negative": bool(spec.get("fcf_negative")),
            "note": spec.get("note") or spec.get("exclusion_reason"),
            "conflict": result["decision"] in ("BUY", "BUY_BLOCKED") and (
                spec.get("fcf_negative") or spec["category"] == "excluded"
            ),
        }
    return result


def _critical_fields_missing(layers):
    """Ritorna le chiavi di config.CRITICAL_FIELDS_FOR_BUY il cui filtro di
    Motore A è "missing" (dato non disponibile) — oppure TUTTE se il
    livello fundamental è del tutto assente (fetch fallito)."""
    fundamental_raw = layers["fundamental"]["raw"]
    if not fundamental_raw or "filters" not in fundamental_raw:
        return list(config.CRITICAL_FIELDS_FOR_BUY)
    statuses = {f["key"]: f["status"] for f in fundamental_raw["filters"]}
    return [key for key in config.CRITICAL_FIELDS_FOR_BUY if statuses.get(key, "missing") == "missing"]


def _finalize_decision(ticker, price, layers, data_sources, decision_override=None, override_codes=None):
    conn = get_db()
    try:
        last = conn.execute(
            "SELECT decision FROM decisions WHERE ticker = ? ORDER BY ts DESC LIMIT 1", (ticker,)
        ).fetchone()
    finally:
        conn.close()
    previous_decision = last["decision"] if last else None

    codes = []
    for layer in layers.values():
        codes.extend(layer["codes"])

    coverage_pct = None
    if decision_override:
        decision, final_score = decision_override, None
        codes = (override_codes or []) + codes
    else:
        weights = config.DECISION_WEIGHTS
        total_weight = weighted_sum = 0.0
        for name, layer in layers.items():
            if layer["score"] is None:
                continue
            # Il layer "news" è un punteggio di RISCHIO (alto = notizia
            # grave): va invertito per contribuire nella stessa direzione
            # BUY-positiva degli altri tre layer.
            contribution = (100 - layer["score"]) if name == "news" else layer["score"]
            weighted_sum += contribution * weights[name]
            total_weight += weights[name]
        coverage_pct = round(total_weight * 100, 1)
        if total_weight <= 0:
            decision, final_score = "DATA_UNAVAILABLE", None
            codes.append("DATA-UNAVAILABLE-ALL-LAYERS")
        else:
            final_score = round(weighted_sum / total_weight, 1)
            # Con troppi livelli mancanti, ridistribuire il peso solo su
            # quelli disponibili può gonfiare artificialmente il punteggio
            # (es. mancano fundamental+bottleneck, gli unici due livelli che
            # avrebbero potuto segnalare un problema aziendale, e il
            # punteggio sale invece di restare prudente). Sotto la soglia di
            # copertura, la decisione resta HOLD qualunque sia il punteggio:
            # mai un BUY/SELL basato su dati incompleti.
            if total_weight < config.DECISION_MIN_WEIGHT_COVERAGE:
                decision = "HOLD"
                codes.append(f"DATA-COVERAGE-LOW-{round(total_weight * 100)}PCT")
            elif final_score >= config.DECISION_BUY_THRESHOLD:
                decision = "BUY"
            elif final_score <= config.DECISION_SELL_THRESHOLD:
                decision = "SELL"
            else:
                decision = "HOLD"

            # Critical-Data Gate: anche con copertura sufficiente, un BUY
            # non può passare se manca un campo critico (FCF, debito) —
            # sono proprio i dati che confermerebbero o smentirebbero la
            # tesi. "BUY_BLOCKED" è uno stato esplicito, diverso da un HOLD
            # generico, per non nasconderlo all'utente.
            if decision == "BUY":
                missing_critical = _critical_fields_missing(layers)
                if missing_critical:
                    decision = "BUY_BLOCKED"
                    codes.append("HARD-BLOCK-CRITICAL-FUNDAMENTAL-MISSING:" + ",".join(missing_critical))

    changed = previous_decision is not None and previous_decision != decision

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO decisions (ticker, price, technical_json, fundamental_json, bottleneck_json, "
            "news_json, technical_score, fundamental_score, bottleneck_score, news_score, final_score, "
            "decision, previous_decision, changed, reason_codes, filter_version, data_sources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker, price,
                json.dumps(layers["technical"]["raw"]), json.dumps(layers["fundamental"]["raw"]),
                json.dumps(layers["bottleneck"]["raw"]), json.dumps(layers["news"]["raw"]),
                layers["technical"]["score"], layers["fundamental"]["score"],
                layers["bottleneck"]["score"], layers["news"]["score"], final_score,
                decision, previous_decision, int(changed),
                json.dumps(codes), config.DECISION_ENGINE_VERSION, json.dumps(data_sources),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = {
        "ticker": ticker, "price": price, "decision": decision, "previous_decision": previous_decision,
        "changed": changed, "final_score": final_score, "coverage_pct": coverage_pct, "reason_codes": codes,
        "filter_version": config.DECISION_ENGINE_VERSION,
        "layers": {k: v["score"] for k, v in layers.items()},
        "data_sources": data_sources,
    }
    if changed:
        notify_decision_change(result)
    return result


def notify_decision_change(result):
    """Alert strutturato (MAI testo generativo), solo su cambio di
    decisione rispetto all'ultima registrata — regola di transizione del
    Decision Engine, niente spam a parità di stato. BUY_BLOCKED ha un
    avviso dedicato (regola 36): mai un finto silenzio quando un BUY viene
    fermato dai gate sui dati."""
    lines = ["CECCHINO PRO", ""]
    if result["decision"] == "BUY_BLOCKED":
        lines += ["⚠ ANALISI BLOCCATA — DATI INSUFFICIENTI", "",
                   f"{result['ticker']}: il punteggio suggerirebbe BUY ma mancano dati "
                   f"critici per verificarlo (vedi Reasons sotto). Decisione: BUY_BLOCKED.", ""]
    else:
        lines += [result["ticker"], f"Signal: {result['previous_decision']} → {result['decision']}", ""]
    if result["final_score"] is not None:
        lines += [f"Score: {result['final_score']}/100", ""]
    if result.get("coverage_pct") is not None:
        lines += [f"Data coverage: {result['coverage_pct']}%", ""]
    if result["reason_codes"]:
        lines.append("Reasons:")
        lines += [f"- {code}" for code in result["reason_codes"][:8]]
        lines.append("")
    if result["price"] is not None:
        lines.append(f"Price: {result['price']}")
    lines.append(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines += ["", f"Filter version: {result['filter_version']}"]
    body = "\n".join(lines)
    send_mail(f"CECCHINO PRO — {result['ticker']} {result['previous_decision']} → {result['decision']}", body)
    send_telegram(body)


def backfill_decision_outcomes():
    """Job periodico (cron tick): per ogni decisione con più di 3/6/12 mesi
    non ancora ricontrollata a quella scadenza, registra il prezzo attuale
    e il rendimento. Usa solo dati successivi al timestamp della decisione
    (mai il prezzo di oggi per giudicare una decisione di oggi: niente
    look-ahead)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE decision != 'DATA_UNAVAILABLE' "
            "AND (checked_3m = 0 OR checked_6m = 0 OR checked_12m = 0)"
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now()
    for row in rows:
        try:
            created = datetime.strptime(row["ts"].split(".")[0], "%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError, TypeError):
            continue
        age_days = (now - created).days
        updates = {}
        for horizon, days in (("3m", 90), ("6m", 180), ("12m", 365)):
            if age_days >= days and not row[f"checked_{horizon}"]:
                price_then = _bottleneck_price_now(row["ticker"])
                updates[f"price_{horizon}"] = price_then
                updates[f"checked_{horizon}"] = 1
                if price_then and row["price"]:
                    updates[f"return_{horizon}"] = round((price_then - row["price"]) / row["price"] * 100, 2)
        if not updates:
            continue
        conn = get_db()
        try:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE decisions SET {set_clause} WHERE id = ?", (*updates.values(), row["id"]))
            conn.commit()
        finally:
            conn.close()


def compute_screener_signal(spec, analysis, recent_event=None):
    """Applica le 7 regole operative del desk. Ritorna (signal, reasons)."""
    if spec["category"] == "excluded":
        return "ESCLUSO", [spec.get("exclusion_reason", "Titolo scartato in analisi precedente")]

    price, rsi, chg_5d = analysis["price"], analysis["rsi"], analysis["chg_5d"]
    post_jump = chg_5d is not None and chg_5d >= config.POST_JUMP_THRESHOLD_PCT  # regola 1
    fcf_negative = spec.get("fcf_negative", False)  # regola 2

    if spec["category"] == "owned":
        if spec.get("never_sell"):  # regola 5
            return "HOLD", [f"{spec.get('role', '')} — mai in vendita per policy"]
        if recent_event:  # regola 3: SELL solo su evento reale confermato
            return "SELL", [f"Evento reale rilevato: {recent_event.get('description', 'vedi notizie')} — rivedi la tesi"]
        reasons = ["In portafoglio, nessun evento di rottura tesi confermato nelle "
                   "ultime 48h: resta HOLD anche se il prezzo è sceso"]
        if post_jump:
            reasons.append(f"+{chg_5d:.1f}% negli ultimi 5gg: non aggiungere qui, lascia assestare")
        return "HOLD", reasons

    # watchlist
    reasons = []
    if spec.get("no_retrade"):
        return "WATCH", [f"{spec.get('note', '')} — richiede conferma esplicita, mai BUY automatico"]

    entry_ok = in_screener_entry_zone(spec, price)
    if not entry_ok:
        return "WATCH", [f"Fuori dalla zona di ingresso ({_screener_entry_zone_text(spec)}), prezzo attuale {price}"]

    if post_jump:
        return "WATCH", [f"+{chg_5d:.1f}% negli ultimi 5gg — balzo da lasciar assestare (regola anti-inseguimento)"]

    if fcf_negative:
        return "WATCH", ["FCF negativo — segnale limitato a WATCH finché non torna positivo"]

    if not (25 <= rsi <= 55):
        return "WATCH", [f"In zona ingresso ma RSI {rsi} fuori dal range 25-55 richiesto per BUY forte"]

    catalyst_date = spec.get("catalyst_date")
    has_catalyst = False
    if catalyst_date:
        try:
            days_to = (datetime.strptime(catalyst_date, "%Y-%m-%d").date() - datetime.now().date()).days
            has_catalyst = 0 <= days_to <= config.CATALYST_WINDOW_DAYS
        except ValueError:
            pass
    if not has_catalyst:
        return "WATCH", [
            "In zona ingresso e RSI ok, ma nessun catalizzatore datato entro 6 settimane "
            "(il target di consenso analisti non è disponibile gratis per verificare lo "
            "sconto del 20%): resta WATCH finché non c'è una data"
        ]

    return "BUY_FORTE", [
        f"In zona ingresso ({_screener_entry_zone_text(spec)}), RSI {rsi} nel range, "
        f"5gg {chg_5d:+.1f}%, catalizzatore entro 6 settimane ({catalyst_date})"
    ]


def compute_screener_concentration(owner):
    """Peso % per settore (regola 4) per un proprietario, sulle posizioni
    possedute (qty > 0) taggate con quell'owner o condivise."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tickers WHERE active = 1 AND qty > 0 AND (owner = ? OR owner = 'shared')",
            (owner,),
        ).fetchall()
    finally:
        conn.close()

    sector_value, total = {}, 0.0
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get(row["ticker"])
        if not cached or "error" in cached:
            continue
        value = cached["price"] * row["qty"]
        total += value
        sector = SCREENER_BY_TICKER.get(row["ticker"], {}).get("sector", "Altro")
        sector_value[sector] = sector_value.get(sector, 0) + value

    if not total:
        return {}
    return {s: round(v / total * 100, 1) for s, v in sector_value.items()}


def notify_weekly_report(results, concentration_alerts):
    buy = [r for r in results if r["signal"] == "BUY_FORTE"][: config.MAX_BUY_PER_WEEK]
    watch = [r for r in results if r["signal"] == "WATCH"]
    sell = [r for r in results if r["signal"] == "SELL"]

    lines = ["📅 CECCHINO — Report settimanale\n"]
    if buy:
        lines.append(f"🟢 BUY FORTE (max {config.MAX_BUY_PER_WEEK}):")
        for r in buy:
            lines.append(f"  {r['ticker']} a {r['price']} — {r['reasons'][0]}")
    else:
        lines.append("🟢 Nessun BUY FORTE questa settimana.")

    if sell:
        lines.append("\n🔴 SELL (evento confermato):")
        for r in sell:
            lines.append(f"  {r['ticker']} a {r['price']} — {r['reasons'][0]}")

    if concentration_alerts:
        lines.append("\n⚠️ ALERT CONCENTRAZIONE:")
        for a in concentration_alerts:
            lines.append(f"  {a}")

    others = len(results) - len(buy) - len(sell)
    lines.append(f"\n📋 Altri {others} titoli in WATCH/HOLD/coda — dettagli sul sito, tab Settimanale.")
    lines.append("\nMonitoraggio, non consiglio finanziario. Nessuna operazione automatica.")

    broadcast("📅 CECCHINO: report settimanale", "\n".join(lines))


def run_weekly_screener(send=True, force=False):
    """Girano tutte le 7 regole sui 25 titoli. Il report completo (con
    notifica) parte solo il lunedì, salvo force=True per un refresh manuale
    dalla UI (che aggiorna comunque la cache ma non rimanda la notifica se
    già inviata questa settimana)."""
    seed_screener_universe()

    week_key = datetime.now().strftime("%Y-W%W")
    results = []
    for spec in config.SCREENER_UNIVERSE:
        if spec["category"] == "excluded":
            results.append({
                "ticker": spec["ticker"], "name": spec["name"], "category": "excluded",
                "signal": "ESCLUSO", "reasons": [spec.get("exclusion_reason", "")],
                "price": None, "sector": spec.get("sector"),
            })
            continue
        try:
            analysis = analyze_for_screener(spec["ticker"])
        except Exception as e:
            print(f"Errore screener settimanale per {spec['ticker']}: {e}")
            analysis = None
        if not analysis:
            results.append({
                "ticker": spec["ticker"], "name": spec.get("name", spec["ticker"]),
                "category": spec["category"], "signal": "N/D",
                "reasons": ["Dati non disponibili"], "price": None, "sector": spec.get("sector"),
            })
            continue

        recent_event = check_recent_event(spec["ticker"]) if spec["category"] == "owned" else None
        signal, reasons = compute_screener_signal(spec, analysis, recent_event)
        results.append({
            "ticker": spec["ticker"], "name": spec.get("name", spec["ticker"]),
            "category": spec["category"], "owner": spec.get("owner"), "sector": spec.get("sector"),
            "signal": signal, "reasons": reasons, "price": analysis["price"],
            "currency": analysis["currency"], "rsi": analysis["rsi"], "chg_5d": analysis["chg_5d"],
        })

    concentration = {
        "mohamed": compute_screener_concentration("mohamed"),
        "micaela": compute_screener_concentration("micaela"),
    }
    concentration_alerts = []
    for owner, sectors in concentration.items():
        for sector, pct in sectors.items():
            if pct > config.SECTOR_CONCENTRATION_LIMIT_PCT:
                concentration_alerts.append(
                    f"{owner.capitalize()}: {sector} al {pct}% (limite {config.SECTOR_CONCENTRATION_LIMIT_PCT:.0f}%)"
                )

    payload = {"results": results, "concentration": concentration,
               "concentration_alerts": concentration_alerts,
               "updated": datetime.now().isoformat(timespec="seconds"), "week_key": week_key}

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (id, last_weekly_screener_results) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_weekly_screener_results = excluded.last_weekly_screener_results",
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()

        is_monday = datetime.now().weekday() == 0
        if send and (is_monday or force):
            row = conn.execute("SELECT last_weekly_screener_sent FROM settings WHERE id = 1").fetchone()
            already_sent = row and row["last_weekly_screener_sent"] == week_key
            if not already_sent:
                notify_weekly_report(results, concentration_alerts)
                conn.execute(
                    "UPDATE settings SET last_weekly_screener_sent = ? WHERE id = 1", (week_key,)
                )
                conn.commit()
    finally:
        conn.close()

    return payload


def maybe_run_weekly_screener():
    """Richiamata dal monitor orario/cron: calcola lo screener a 25 titoli
    al massimo una volta al giorno (non ad ogni tick), e con notifica solo
    il lunedì — il costo (fetch + chiamate Gemini) non giustifica girarlo
    più spesso. Il refresh manuale dalla UI chiama invece run_weekly_screener
    direttamente, sempre, ignorando questo limite."""
    conn = get_db()
    row = conn.execute("SELECT last_weekly_screener_results FROM settings WHERE id = 1").fetchone()
    conn.close()

    today = datetime.now().strftime("%Y-%m-%d")
    last_date = None
    if row and row["last_weekly_screener_results"]:
        try:
            last_date = json.loads(row["last_weekly_screener_results"]).get("updated", "")[:10]
        except (json.JSONDecodeError, TypeError):
            pass

    if last_date == today:
        return  # già calcolato oggi
    if datetime.now().weekday() == 0 or last_date is None:
        run_weekly_screener(send=True)


def generate_daily_verdict(send=True):
    """Verdetto giornaliero AI (Gemini) sull'intero portafoglio: per ogni
    posizione un giudizio COMPRA/AUMENTA/TIENI/RIDUCI/VENDI basato sui
    segnali tecnici già calcolati (RSI, medie, 52 settimane, volumi, stop
    loss, peso). Attenzione: Gemini qui NON fa ricerche web in tempo reale,
    quindi non conosce notizie specifiche del giorno a meno che non siano
    già riflesse nel prezzo — è un'analisi tecnica+AI, non una ricerca
    fondamentale verificata."""
    if not config.GEMINI_API_KEY:
        return {"error": "Serve GEMINI_API_KEY per il verdetto giornaliero AI"}

    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM tickers WHERE active = 1 ORDER BY ticker").fetchall()
    finally:
        conn.close()

    if not rows:
        return {"error": "Portafoglio vuoto"}

    positions = []
    total_value = 0.0
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get(row["ticker"])
        if cached is None or "error" in cached:
            continue
        value = cached["price"] * row["qty"] if row["qty"] else cached["price"]
        total_value += value
        positions.append({"row": row, "analysis": cached, "value": value})

    if not positions:
        return {"error": "Nessuna posizione con dati validi in cache: apri il tab Portafoglio prima"}

    sector_weights = {}
    lines = []
    for p in positions:
        ticker = p["row"]["ticker"]
        a = p["analysis"]
        weight = (p["value"] / total_value * 100) if total_value else 0
        sector = SECTOR_MAP.get(ticker, "Altro")
        sector_weights[sector] = sector_weights.get(sector, 0) + weight
        pnl_pct = None
        if p["row"]["qty"] and p["row"]["paid"]:
            pnl_pct = (p["value"] - p["row"]["paid"]) / p["row"]["paid"] * 100
        stop_txt = f", stop loss {a['stop_loss']}" if a.get("stop_loss") is not None else ""
        pnl_txt = f"{pnl_pct:.1f}%" if pnl_pct is not None else "non disponibile"
        lines.append(
            f"- {ticker}: segnale {a['signal']} (score {a['score']}), prezzo {a['price']} {a['currency']}, "
            f"peso {weight:.1f}% del portafoglio, RSI {a['rsi']}, "
            f"distanza da max 52W {a['dist_high52']}%, P&L {pnl_txt}{stop_txt}"
        )

    sector_lines = [f"- {s}: {w:.1f}%" for s, w in sector_weights.items()]

    prompt = (
        "Sei un analista di portafoglio diretto e senza fronzoli. Ti do la situazione attuale "
        "di un portafoglio azionario con dati tecnici già calcolati (RSI, medie mobili, distanza "
        "dai massimi/minimi a 52 settimane, punteggio del segnale, peso, P&L, stop loss). NON hai "
        "accesso a notizie in tempo reale: basati solo sui numeri forniti e sulla tua conoscenza "
        "generale delle aziende, e se non sei sicuro di qualcosa di specifico e recente dillo "
        "esplicitamente invece di inventarlo. Per ogni posizione dai un verdetto tra: COMPRA, "
        "AUMENTA, TIENI, RIDUCI, VENDI, con una riga di motivazione al massimo. Segnala se un "
        "settore supera il 25% del portafoglio o un singolo titolo supera il 15%. Chiudi con una "
        "riga finale con l'azione prioritaria della giornata (o \"nessuna operazione\" se è così). "
        "Italiano diretto, zero ripetizioni, testo semplice pronto per Telegram (non JSON, non markdown pesante).\n\n"
        "Posizioni:\n" + "\n".join(lines) + "\n\n"
        "Pesi per settore:\n" + "\n".join(sector_lines)
    )

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={config.GEMINI_API_KEY}"
        )
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        if r.status_code != 200:
            print(f"Gemini verdetto HTTP {r.status_code}: {r.text[:300]!r}")
            return {"error": "L'AI non ha risposto (errore di rete o quota esaurita)"}
        j = r.json()
        text = j["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini verdetto fallito: {e}")
        return {"error": f"Errore: {e}"}

    updated = datetime.now().isoformat(timespec="seconds")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO settings (id, last_verdict_text) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_verdict_text = excluded.last_verdict_text",
            (text,),
        )
        conn.commit()

        if send:
            today = datetime.now().strftime("%Y-%m-%d")
            row = conn.execute("SELECT last_verdict_sent FROM settings WHERE id = 1").fetchone()
            already_sent = row and row["last_verdict_sent"] == today
            if not already_sent:
                broadcast("📋 CECCHINO: verdetto giornaliero del portafoglio", text)
                conn.execute("UPDATE settings SET last_verdict_sent = ? WHERE id = 1", (today,))
                conn.commit()
    finally:
        conn.close()

    return {"text": text, "updated": updated}


def monitor_loop():
    """Thread di riserva per esecuzioni locali/24-7 reali (es. Raspberry Pi).
    Su hosting cloud gratuito che va in sleep, usa /api/cron/tick invece."""
    time.sleep(10)  # Attende l'avvio di Flask
    while True:
        for name, func in _TICK_STEPS:
            _run_tick_step(name, func)
        time.sleep(3600)


# --------------------------------------------------------------------------
# Bot Telegram interattivo (opzionale): manda una foto o un comando nella
# chat e ricevi la risposta lì, senza mai aprire il sito. Usa long polling
# su getUpdates (nessun webhook da registrare, funziona anche in locale/Pi).
# --------------------------------------------------------------------------
TELEGRAM_HELP_TEXT = (
    "🎯 Cecchino Pro\n\n"
    "Mandami una foto del tuo portafoglio (screenshot del broker) e la "
    "importo, poi ti do subito il verdetto.\n\n"
    "Comandi:\n"
    "/portafoglio — riepilogo posizioni attuali\n"
    "/verdetto — rigenera il verdetto AI su tutto il portafoglio\n"
    "/aiuto — questo messaggio"
)


def _telegram_get_file_bytes(file_id):
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getFile"
        r = requests.get(url, params={"file_id": file_id}, timeout=15)
        j = r.json()
        if not j.get("ok"):
            return None
        file_path = j["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}"
        r2 = requests.get(file_url, timeout=20)
        return r2.content if r2.status_code == 200 else None
    except Exception as e:
        print(f"Errore download foto Telegram: {e}")
        return None


def _format_portfolio_summary():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM tickers WHERE active = 1 ORDER BY ticker").fetchall()
    finally:
        conn.close()
    if not rows:
        return "Portafoglio vuoto."
    lines = []
    for row in rows:
        with CACHE_LOCK:
            cached = LAST_ANALYSIS.get(row["ticker"])
        if not cached or "error" in cached:
            lines.append(f"{row['ticker']}: dati non disponibili")
            continue
        pnl_txt = ""
        if row["qty"] and row["paid"]:
            value = cached["price"] * row["qty"]
            pnl_pct = (value - row["paid"]) / row["paid"] * 100
            pnl_txt = f", P&L {pnl_pct:+.1f}%"
        lines.append(f"{row['ticker']}: {cached['signal']} · {cached['price']} {cached['currency']}{pnl_txt}")
    return "\n".join(lines)


def _handle_telegram_message(message):
    """Un solo proprietario per bot: la prima chat che scrive si registra da
    sola (nessuno conosce lo username del bot appena creato tranne te);
    qualsiasi altra chat viene ignorata in silenzio da quel momento in poi."""
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    saved_chat_id = get_telegram_chat_id()
    if not saved_chat_id:
        set_telegram_chat_id(chat_id)
        send_telegram(
            "✅ Configurato! Da ora questo bot risponde solo a te.\n\n" + TELEGRAM_HELP_TEXT,
            chat_id=chat_id,
        )
        return
    if chat_id != saved_chat_id:
        return  # bot personale: ignora chat diverse dal proprietario

    text = (message.get("text") or "").strip()
    photos = message.get("photo")

    if photos:
        send_telegram("📷 Foto ricevuta, la leggo (qualche secondo)…", chat_id=chat_id)
        file_id = photos[-1]["file_id"]  # l'ultima è la risoluzione più alta
        image_bytes = _telegram_get_file_bytes(file_id)
        if not image_bytes:
            send_telegram("Non sono riuscito a scaricare la foto, riprova.", chat_id=chat_id)
            return
        result = import_photo_positions(image_bytes, "image/jpeg")
        if "error" in result:
            send_telegram(f"❌ {result['error']}", chat_id=chat_id)
            return
        imported = result.get("imported", [])
        skipped = result.get("skipped", [])
        summary = f"✅ Importate {len(imported)} posizioni"
        if skipped:
            summary += f" ({len(skipped)} non riconosciute, controllale sul sito)"
        send_telegram(summary, chat_id=chat_id)

        verdict = generate_daily_verdict(send=False)
        if "text" in verdict:
            send_telegram("📋 Verdetto aggiornato:\n\n" + verdict["text"], chat_id=chat_id)
        elif "error" in verdict:
            send_telegram(f"(Verdetto non disponibile: {verdict['error']})", chat_id=chat_id)
        return

    if text.startswith("/verdetto"):
        send_telegram("🤖 Genero il verdetto…", chat_id=chat_id)
        verdict = generate_daily_verdict(send=False)
        send_telegram(verdict.get("text") or f"Errore: {verdict.get('error')}", chat_id=chat_id)
        return

    if text.startswith("/portafoglio"):
        send_telegram(_format_portfolio_summary(), chat_id=chat_id)
        return

    if text.startswith(("/start", "/aiuto", "/help")):
        send_telegram(TELEGRAM_HELP_TEXT, chat_id=chat_id)
        return

    send_telegram("Non ho capito questo comando.\n\n" + TELEGRAM_HELP_TEXT, chat_id=chat_id)


_telegram_update_offset = 0
_telegram_started = False


def telegram_poll_loop():
    """Long polling: gira finché TELEGRAM_BOT_TOKEN è configurato. Nessun
    webhook da registrare, funziona ovunque (anche dietro NAT/in locale)."""
    global _telegram_update_offset
    time.sleep(5)
    while True:
        try:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 25, "offset": _telegram_update_offset + 1}
            r = requests.get(url, params=params, timeout=35)
            j = r.json()
            if not j.get("ok"):
                time.sleep(10)
                continue
            for update in j.get("result", []):
                _telegram_update_offset = max(_telegram_update_offset, update["update_id"])
                message = update.get("message") or update.get("channel_post")
                if message:
                    try:
                        _handle_telegram_message(message)
                    except Exception as e:
                        print(f"Errore gestione messaggio Telegram: {e}")
        except Exception as e:
            print(f"Errore polling Telegram (riprova tra 15s): {e}")
            time.sleep(15)


_monitor_started = False


def start_background_monitor():
    global _monitor_started, _telegram_started
    with CACHE_LOCK:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=monitor_loop, daemon=True).start()

    if config.TELEGRAM_BOT_TOKEN:
        with CACHE_LOCK:
            if not _telegram_started:
                _telegram_started = True
                threading.Thread(target=telegram_poll_loop, daemon=True).start()


# --------------------------------------------------------------------------
# API - Cron esterno (GitHub Actions o altro, gratuito, per il tick orario)
# --------------------------------------------------------------------------
def _run_tick_step(name, func):
    """Isola ogni fase del tick: se una fase fallisce (provider offline,
    bug non ancora scoperto, rate limit), le altre girano comunque e il
    tick torna sempre 200 — altrimenti un solo errore blocca tutto il
    risveglio automatico del sito (esattamente il sintomo che ha causato
    l'HTTP 500 su Render: un errore in un'unica fase abbatteva l'intero
    /api/cron/tick)."""
    try:
        func()
    except Exception as e:
        print(f"Errore nello step '{name}' del tick (continuo con gli altri): {e}")


_TICK_STEPS = [
    ("refresh_all_portfolio", refresh_all_portfolio),
    ("check_watch_levels", check_watch_levels),
    ("run_market_screener", run_market_screener),
    ("generate_daily_verdict", generate_daily_verdict),
    ("maybe_run_weekly_screener", maybe_run_weekly_screener),
    ("recheck_bottleneck_decisions", recheck_bottleneck_decisions),
    ("run_decision_engine_for_portfolio", run_decision_engine_for_portfolio),
    ("backfill_decision_outcomes", backfill_decision_outcomes),
]

_TICK_STATE = {"running": False, "started_at": None, "finished_at": None}
_TICK_LOCK = threading.Lock()


def _run_tick_steps_background():
    """Il lavoro vero del tick, sempre in un thread separato: con Yahoo che
    risponde 429 su quasi tutti i titoli, ogni fase può metterci diversi
    secondi (retry inclusi) e la somma di 8 fasi può superare il timeout
    che Render impone a una singola richiesta HTTP — un timeout a livello
    di infrastruttura restituisce 500 PRIMA che _run_tick_step riesca a
    isolare l'errore, perché non è un'eccezione Python. La soluzione è non
    far mai aspettare la richiesta HTTP: /api/cron/tick torna 200 subito,
    il lavoro prosegue qui indipendentemente da quanto ci mette."""
    with _TICK_LOCK:
        if _TICK_STATE["running"]:
            return
        _TICK_STATE["running"] = True
        _TICK_STATE["started_at"] = datetime.now().isoformat()
    try:
        for name, func in _TICK_STEPS:
            _run_tick_step(name, func)
    finally:
        with _TICK_LOCK:
            _TICK_STATE["running"] = False
            _TICK_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/cron/tick", methods=["POST"])
def api_cron_tick():
    secret = request.headers.get("X-Cron-Secret", "")
    if not config.CRON_SECRET or secret != config.CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    if _TICK_STATE["running"]:
        return jsonify({"ok": True, "already_running": True})
    threading.Thread(target=_run_tick_steps_background, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/cron/tick/status", methods=["GET"])
def api_cron_tick_status():
    return jsonify(_TICK_STATE)


# --------------------------------------------------------------------------
# API - Bottleneck Filter (screener a due motori, dati live)
# --------------------------------------------------------------------------
_BOTTLENECK_SCAN_STATE = {"running": False, "done": 0, "total": 0, "started_at": None, "finished_at": None}
_BOTTLENECK_SCAN_LOCK = threading.Lock()


def save_bottleneck_decision_daily(result, thresholds):
    """Come save_bottleneck_decision, ma al massimo una voce per ticker al
    giorno: evita che una scansione dell'intero universo, ripetuta più
    volte, gonfi il registro con doppioni inutili all'accuratezza."""
    if result.get("error"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id FROM bottleneck_decisions WHERE ticker = ? AND date(created) = ?",
            (result["ticker"], today),
        ).fetchone()
    finally:
        conn.close()
    if not existing:
        save_bottleneck_decision(result, thresholds)


def _run_bottleneck_scan(thresholds):
    universe = config.BOTTLENECK_UNIVERSE
    with _BOTTLENECK_SCAN_LOCK:
        _BOTTLENECK_SCAN_STATE.update({"running": True, "done": 0, "total": len(universe),
                                        "started_at": datetime.now().isoformat(), "finished_at": None})
    for ticker in universe:
        try:
            result = analyze_bottleneck(ticker, thresholds=thresholds)
            if not result.get("error"):
                conn = get_db()
                try:
                    conn.execute(
                        "INSERT INTO bottleneck_scan (ticker, result_json, scanned_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(ticker) DO UPDATE SET result_json = excluded.result_json, "
                        "scanned_at = excluded.scanned_at",
                        (ticker, json.dumps(result), time.time()),
                    )
                    conn.commit()
                finally:
                    conn.close()
                save_bottleneck_decision_daily(result, thresholds)
        except Exception as e:
            print(f"Scan Bottleneck fallita per {ticker}: {e}")
        with _BOTTLENECK_SCAN_LOCK:
            _BOTTLENECK_SCAN_STATE["done"] += 1
    with _BOTTLENECK_SCAN_LOCK:
        _BOTTLENECK_SCAN_STATE["running"] = False
        _BOTTLENECK_SCAN_STATE["finished_at"] = datetime.now().isoformat()


@app.route("/api/bottleneck/thresholds", methods=["GET"])
def api_bottleneck_thresholds():
    return jsonify(config.BOTTLENECK_DEFAULTS)


@app.route("/api/bottleneck/analyze/<ticker>", methods=["POST"])
def api_bottleneck_analyze(ticker):
    """Analisi live di un singolo ticker (ricerca manuale). Salva sempre nel
    registro delle decisioni con le soglie correnti passate dal client."""
    body = request.get_json(silent=True) or {}
    thresholds = body.get("thresholds") or config.BOTTLENECK_DEFAULTS
    owner = body.get("owner")
    save = body.get("save", True)
    result = analyze_bottleneck(ticker.upper().strip(), thresholds=thresholds, owner=owner)
    if save and not result.get("error"):
        save_bottleneck_decision(result, thresholds)
    return jsonify(result)


@app.route("/api/bottleneck/scan", methods=["POST"])
def api_bottleneck_scan_start():
    if _BOTTLENECK_SCAN_STATE["running"]:
        return jsonify({"error": "Scansione già in corso", "state": _BOTTLENECK_SCAN_STATE}), 409
    body = request.get_json(silent=True) or {}
    thresholds = body.get("thresholds") or config.BOTTLENECK_DEFAULTS
    threading.Thread(target=_run_bottleneck_scan, args=(thresholds,), daemon=True).start()
    return jsonify({"started": True, "total": len(config.BOTTLENECK_UNIVERSE)})


@app.route("/api/bottleneck/scan/status", methods=["GET"])
def api_bottleneck_scan_status():
    return jsonify(_BOTTLENECK_SCAN_STATE)


@app.route("/api/bottleneck/scan/results", methods=["GET"])
def api_bottleneck_scan_results():
    only_idonei = request.args.get("only_idonei", "1") != "0"
    conn = get_db()
    try:
        rows = conn.execute("SELECT ticker, result_json, scanned_at FROM bottleneck_scan").fetchall()
    finally:
        conn.close()
    results = []
    for row in rows:
        try:
            r = json.loads(row["result_json"])
        except (TypeError, ValueError):
            continue
        r["scanned_at"] = row["scanned_at"]
        results.append(r)
    if only_idonei:
        results = [
            r for r in results
            if r.get("engine_a", {}).get("status") in ("IDONEO", "A_UN_FILTRO")
            and r.get("engine_b", {}).get("verdict") == "COMPRA"
        ]
    results.sort(key=lambda r: (r.get("engine_a", {}).get("dislocation_value") or 0), reverse=True)
    return jsonify({"results": results, "total_scanned": len(rows)})


@app.route("/api/accuratezza", methods=["GET"])
def api_accuratezza():
    return jsonify(compute_bottleneck_accuracy())


# --------------------------------------------------------------------------
# API - Decision Engine (motore tecnico + fondamentale + bottleneck + news
# uniti in un'unica decisione, sempre deterministico)
# --------------------------------------------------------------------------
@app.route("/api/decisions/<ticker>", methods=["GET"])
def api_decision_get(ticker):
    """Calcola (e registra) la decisione corrente per un ticker. Usato dal
    pulsante "Analizza" dello Scanner per mostrare il PERCHÉ strutturato."""
    return jsonify(evaluate_decision(ticker.upper().strip()))


@app.route("/api/decisions", methods=["GET"])
def api_decisions_log():
    """Ultima decisione registrata per ogni ticker tracciato (tab Log)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT d.* FROM decisions d "
            "INNER JOIN (SELECT ticker, MAX(ts) AS max_ts FROM decisions GROUP BY ticker) latest "
            "ON d.ticker = latest.ticker AND d.ts = latest.max_ts "
            "ORDER BY d.ts DESC"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append({
            "ticker": row["ticker"], "ts": row["ts"], "price": row["price"],
            "decision": row["decision"], "previous_decision": row["previous_decision"],
            "changed": bool(row["changed"]), "final_score": row["final_score"],
            "reason_codes": json.loads(row["reason_codes"]) if row["reason_codes"] else [],
            "filter_version": row["filter_version"],
        })
    return jsonify(out)


@app.route("/api/decisions/history", methods=["GET"])
def api_decisions_history():
    """Storico dei cambi di decisione del Decision Engine (tab Storico) — a
    differenza di /api/decisions (solo l'ultima per ticker), qui ogni riga è
    un cambio di decisione realmente avvenuto (changed=1), la stessa fonte
    usata per gli alert Telegram/mail: cosa mostra Storico è sempre coerente
    con cosa ha inviato il bot, perché entrambi leggono da qui."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE changed = 1 ORDER BY ts DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        out.append({
            "ticker": row["ticker"], "ts": row["ts"], "price": row["price"],
            "decision": row["decision"], "previous_decision": row["previous_decision"],
            "final_score": row["final_score"],
            "reason_codes": json.loads(row["reason_codes"]) if row["reason_codes"] else [],
            "filter_version": row["filter_version"],
        })
    return jsonify(out)


@app.route("/api/accuracy/decisions", methods=["GET"])
def api_decision_accuracy():
    return jsonify(compute_decision_accuracy())


@app.route("/api/news/<ticker>", methods=["GET"])
def api_news_events(ticker):
    """Storico degli eventi notizia già classificati per un ticker (tab
    News/Eventi)."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM news_events WHERE ticker = ? ORDER BY logged_at DESC LIMIT 30",
            (ticker.upper().strip(),),
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(row) for row in rows])


# --------------------------------------------------------------------------
# API - Screener settimanale a 25 titoli
# --------------------------------------------------------------------------
@app.route("/api/screener25", methods=["GET"])
def api_screener25_get():
    conn = get_db()
    row = conn.execute("SELECT last_weekly_screener_results FROM settings WHERE id = 1").fetchone()
    conn.close()
    if not row or not row["last_weekly_screener_results"]:
        return jsonify({"results": [], "concentration": {}, "concentration_alerts": [], "updated": None})
    return jsonify(json.loads(row["last_weekly_screener_results"]))


@app.route("/api/screener25/refresh", methods=["POST"])
def api_screener25_refresh():
    payload = run_weekly_screener(send=False)
    return jsonify(payload)


# --------------------------------------------------------------------------
# API - Watchlist ingresso/stop/target
# --------------------------------------------------------------------------
@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    with CACHE_LOCK:
        cached = dict(WATCHLIST_CACHE)
    conn = get_db()
    rows = conn.execute("SELECT * FROM watch_levels WHERE active = 1 ORDER BY ticker").fetchall()
    conn.close()
    out = []
    for row in rows:
        item = cached.get(row["ticker"], {
            "ticker": row["ticker"], "name": row["name"], "price": None,
            "entry_low": row["entry_low"], "entry_high": row["entry_high"],
            "stop_price": row["stop_price"], "target_low": row["target_low"],
            "target_high": row["target_high"], "status": "IN ATTESA", "updated": None,
        })
        out.append(item)
    return jsonify(out)


@app.route("/api/watchlist/refresh", methods=["POST"])
def api_watchlist_refresh():
    check_watch_levels()
    with CACHE_LOCK:
        return jsonify(list(WATCHLIST_CACHE.values()))


@app.route("/api/watchlist/log", methods=["GET"])
def api_watchlist_log():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM watch_log ORDER BY timestamp DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------
# API - Verdetto giornaliero AI (Gemini)
# --------------------------------------------------------------------------
@app.route("/api/verdict", methods=["GET"])
def api_verdict_get():
    conn = get_db()
    row = conn.execute("SELECT last_verdict_text FROM settings WHERE id = 1").fetchone()
    conn.close()
    return jsonify({"text": row["last_verdict_text"] if row and row["last_verdict_text"] else None})


@app.route("/api/verdict/refresh", methods=["POST"])
def api_verdict_refresh():
    result = generate_daily_verdict(send=False)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


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


def import_photo_positions(image_bytes, mime_type):
    """Nucleo condiviso dell'import da foto: usato sia dall'endpoint HTTP
    /api/portfolio/import-photo sia dal bot Telegram (manda una foto in
    chat = stesso risultato, senza aprire il sito). La app non conosce il
    numero esatto di azioni dalla foto: lo stima dividendo il valore di
    posizione (in €) per il prezzo di mercato attuale convertito in €."""
    extracted = extract_portfolio_from_image(image_bytes, mime_type)
    if "error" in extracted:
        return extracted

    imported, skipped = [], []
    conn = get_db()
    try:
        for pos in extracted["positions"]:
            ticker = (pos.get("ticker") or "").strip().upper()
            value_eur = pos.get("value_eur")
            pnl_eur = pos.get("pnl_eur")
            pnl_pct = pos.get("pnl_pct")

            if not ticker or value_eur is None:
                skipped.append({"raw": pos, "motivo": "ticker o valore non leggibile dalla foto"})
                continue

            data = fetch_market_data(ticker)
            if not data:
                skipped.append({"ticker": ticker, "motivo": "prezzo non trovato, aggiungilo a mano"})
                continue

            price_eur = to_eur(data["price"], data.get("currency", "USD"))
            if not price_eur:
                skipped.append({"ticker": ticker, "motivo": "impossibile convertire il prezzo in euro"})
                continue

            qty = round(value_eur / price_eur, 4)
            if pnl_eur is not None:
                paid = round(value_eur - pnl_eur, 2)
            elif pnl_pct is not None:
                paid = round(value_eur / (1 + pnl_pct / 100), 2)
            else:
                paid = round(value_eur, 2)  # nessun P&L leggibile: assume carico = valore attuale

            conn.execute(
                """
                INSERT INTO tickers (ticker, qty, paid, active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(ticker) DO UPDATE SET
                    qty = excluded.qty,
                    paid = excluded.paid,
                    active = 1
                """,
                (ticker, qty, paid),
            )
            imported.append({"ticker": ticker, "qty": qty, "paid": paid, "value_eur": value_eur})
        conn.commit()
    finally:
        conn.close()

    for item in imported:
        try:
            analyze_and_store(item["ticker"], None, None)
        except Exception as e:
            print(f"Errore analisi post-import {item['ticker']}: {e}")

    return {"imported": imported, "skipped": skipped}


@app.route("/api/portfolio/import-photo", methods=["POST"])
def api_portfolio_import_photo():
    """Legge una foto del portafoglio (es. screenshot Trade Republic) con
    Gemini Vision e aggiunge/aggiorna le posizioni trovate."""
    if "image" not in request.files:
        return jsonify({"error": "Nessuna immagine ricevuta"}), 400
    file = request.files["image"]
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"error": "Immagine vuota"}), 400
    mime_type = file.mimetype or "image/jpeg"

    result = import_photo_positions(image_bytes, mime_type)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


# --------------------------------------------------------------------------
# API - Opportunità (screener automatico sull'universo bottleneck)
# --------------------------------------------------------------------------
@app.route("/api/opportunities", methods=["GET"])
def api_opportunities_list():
    with CACHE_LOCK:
        return jsonify({"results": SCREENER_CACHE["results"], "updated": SCREENER_CACHE["updated"]})


@app.route("/api/opportunities/refresh", methods=["POST"])
def api_opportunities_refresh():
    top = run_market_screener(send_email=False)
    return jsonify({"results": top, "updated": SCREENER_CACHE["updated"]})


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
    return render_template_string(
        INDEX_HTML,
        alert_email=get_alert_email() or "",
        telegram_chat_id=get_telegram_chat_id() or "",
    )


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
button:focus-visible, input:focus-visible, a:focus-visible, summary:focus-visible {
  outline: 2px solid var(--blue);
  outline-offset: 2px;
}
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
}
.card.stripe { padding-left: 18px; overflow: hidden; }
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

.ai-note {
  margin-top: 10px;
  padding: 10px;
  background: rgba(59,130,246,0.10);
  border: 1px solid rgba(59,130,246,0.3);
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.5;
}

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
  min-width: 0;
  min-height: 44px;
  background: transparent;
  border-radius: 0;
  color: var(--dim);
  padding: 8px 2px;
  font-size: 12px;
  font-weight: 600;
  line-height: 1.3;
}
nav.bottom button.active { color: var(--blue); }

#nav-more-sheet {
  position: fixed;
  left: 0; right: 0; bottom: 58px;
  background: var(--card);
  border-top: 1px solid var(--border);
  border-radius: 14px 14px 0 0;
  box-shadow: 0 -4px 16px rgba(0,0,0,0.35);
  display: none;
  z-index: 50;
  padding: 8px;
}
#nav-more-sheet.show { display: block; }
#nav-more-sheet button {
  width: 100%;
  min-height: 44px;
  background: transparent;
  border-radius: 8px;
  color: var(--text);
  text-align: left;
  font-size: 15px;
  font-weight: 600;
  padding: 10px 12px;
}
#nav-more-sheet button.active { color: var(--blue); background: var(--card2); }
#nav-more-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.4);
  display: none;
  z-index: 49;
}
#nav-more-backdrop.show { display: block; }

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
  align-items: flex-start;
  justify-content: center;
  z-index: 999;
  padding: 20px;
  overflow-y: auto;
}
#email-gate .box { max-width: 420px; width: 100%; margin: 24px 0; }

.gate-header { text-align: center; margin-bottom: 20px; }
.gate-header .gate-icon { font-size: 34px; line-height: 1; }
.gate-header h1 { font-size: 22px; margin: 8px 0 4px 0; }
.gate-header .gate-sub { color: var(--dim); font-size: 14px; line-height: 1.5; margin: 0; }

.bot-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  margin-bottom: 18px;
}
.bot-avatar {
  width: 42px; height: 42px; border-radius: 50%;
  background: rgba(59,130,246,0.18);
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; flex-shrink: 0;
}
.bot-info { flex: 1; min-width: 0; }
.bot-info .bot-name { font-weight: 700; font-size: 14px; }
.bot-info .bot-username { color: var(--dim); font-size: 12px; }
.bot-open-btn {
  display: inline-block; white-space: nowrap;
  background: var(--blue); color: white; font-weight: 600;
  font-size: 13px; padding: 8px 12px; border-radius: 8px;
  text-decoration: none; flex-shrink: 0;
}

.steps { margin-bottom: 18px; }
.step { display: flex; gap: 12px; padding: 8px 0; }
.step-num {
  width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0;
  background: var(--card2); border: 1px solid var(--border);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 13px; color: var(--dim);
}
.step-body { padding-top: 2px; }
.step-title { font-weight: 600; font-size: 14px; }
.step-desc { color: var(--dim); font-size: 13px; margin-top: 2px; line-height: 1.4; }
.step-desc code {
  background: var(--card2); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 5px; font-size: 12px;
}

.gate-status {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; color: var(--dim);
  padding: 10px 12px; border-radius: 8px;
  background: var(--card2); margin-bottom: 12px;
}
.gate-status.checking { color: var(--blue); }
.gate-status.connected { color: var(--buy); background: rgba(34,197,94,0.12); }
.gate-status .spin {
  width: 13px; height: 13px; border-radius: 50%;
  border: 2px solid currentColor; border-top-color: transparent;
  animation: gate-spin 0.7s linear infinite; flex-shrink: 0;
}
@keyframes gate-spin { to { transform: rotate(360deg); } }

#gate-connect-btn { width: 100%; padding: 13px; font-size: 15px; }
#gate-connect-btn:disabled { opacity: 0.6; cursor: default; }
.gate-hint { color: var(--dim); font-size: 12px; margin-top: 8px; line-height: 1.4; text-align: center; }
#email-gate .err {
  color: var(--sell); font-size: 13px; margin-top: 10px; line-height: 1.5;
  background: rgba(239,68,68,0.10); border: 1px solid rgba(239,68,68,0.3);
  border-radius: 8px; padding: 10px; display: none;
}
#email-gate .err.show { display: block; }

.gate-fallback { margin-top: 18px; }
.gate-fallback summary {
  cursor: pointer; font-size: 13px; color: var(--dim);
  padding: 8px 0; list-style: none;
}
.gate-fallback summary::-webkit-details-marker { display: none; }
.gate-fallback summary::before { content: "▸ "; }
.gate-fallback[open] summary::before { content: "▾ "; }
.gate-fallback input { width: 100%; margin-top: 8px; }
.gate-fallback button { width: 100%; margin-top: 8px; }

#settings-modal {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 998;
  padding: 20px;
}
#settings-modal .box {
  max-width: 400px;
  width: 100%;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}
#settings-modal h2 { font-size: 18px; margin: 0 0 4px 0; }
#settings-modal label { display: block; font-size: 12px; color: var(--dim); margin: 14px 0 6px 0; }
#settings-modal input { width: 100%; }
#settings-modal .hint { font-size: 12px; color: var(--dim); margin-top: 6px; line-height: 1.4; }
#settings-modal .row-btns { display: flex; gap: 8px; margin-top: 8px; }
#settings-modal .err { color: var(--sell); font-size: 13px; margin-top: 8px; min-height: 16px; }
#settings-modal .ok { color: var(--buy); font-size: 13px; margin-top: 8px; min-height: 16px; }
#settings-modal .close-x { float: right; cursor: pointer; color: var(--dim); font-size: 20px; line-height: 1; }

.verdict-box {
  white-space: pre-wrap;
  font-size: 13px;
  line-height: 1.5;
}
</style>
</head>
<body>

<div id="email-gate" style="display:none">
  <div class="box">
    <div class="gate-header">
      <div class="gate-icon">🎯</div>
      <h1>Cecchino Pro</h1>
      <p class="gate-sub">Per ricevere gli avvisi di Cecchino Pro devi prima aprire il bot su Telegram e inviargli un messaggio.</p>
    </div>

    <div class="bot-card">
      <div class="bot-avatar">✈️</div>
      <div class="bot-info">
        <div class="bot-name" id="gate-bot-name">Bot Telegram</div>
        <div class="bot-username" id="gate-bot-username">Caricamento…</div>
      </div>
      <a class="bot-open-btn" id="gate-bot-link" href="https://telegram.org" target="_blank" rel="noopener">Apri Telegram</a>
    </div>

    <div class="steps">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-body">
          <div class="step-title">Apri Telegram</div>
          <div class="step-desc">Cerca il bot mostrato sopra (o tocca "Apri Telegram") e premi <b>Avvia</b></div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-body">
          <div class="step-title">Invia un messaggio</div>
          <div class="step-desc">Scrivi <code>/start</code> oppure un messaggio qualsiasi, es. "ciao"</div>
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-body">
          <div class="step-title">Torna qui e collega</div>
          <div class="step-desc">Premi il pulsante qui sotto: verifichiamo il tuo ultimo messaggio e colleghiamo l'account</div>
        </div>
      </div>
    </div>

    <div class="gate-status" id="gate-status">
      <span>⚠️ Telegram non è ancora collegato</span>
    </div>

    <button id="gate-connect-btn" onclick="detectAndStartTelegram()">📡 Collega Telegram</button>
    <div class="gate-hint">Premendo "Collega", Cecchino Pro verifica il tuo ultimo messaggio al bot e associa il tuo account Telegram all'app.</div>

    <div class="err" id="gate-err"></div>

    <details class="gate-fallback">
      <summary>⚙️ Hai problemi a collegare Telegram?</summary>
      <p class="dim" style="font-size:12px;margin:6px 0">In alternativa puoi inserire manualmente il Chat ID (scrivi a <b>@userinfobot</b> su Telegram: risponde subito con il tuo ID).</p>
      <input id="gate-telegram" type="text" placeholder="es. 123456789" autocomplete="off" inputmode="numeric">
      <button class="secondary" onclick="saveGateTelegram()">Collega con Chat ID</button>
    </details>
  </div>
</div>

<div id="settings-modal" style="display:none">
  <div class="box">
    <span class="close-x" onclick="closeSettings()">✕</span>
    <h2>⚙️ Impostazioni</h2>
    <div class="dim" style="font-size:12px">Canale principale per gli alert BUY/SELL</div>

    <div id="settings-telegram-connected" class="gate-status connected" style="display:none;margin-top:14px">
      <span id="settings-telegram-status-text">✓ Telegram collegato</span>
    </div>
    <div id="settings-telegram-form">
      <label>Telegram — chat ID</label>
      <input id="set-telegram" type="text" placeholder="es. 123456789">
      <div class="row-btns">
        <button class="secondary" style="flex:1" onclick="detectTelegramChatId()">📡 Rileva automaticamente</button>
      </div>
      <div class="hint">Scrivi prima un messaggio qualsiasi al bot su Telegram (es. "ciao"), poi premi "Rileva automaticamente" — trova da solo il tuo chat ID.</div>
    </div>
    <div class="row-btns" id="settings-telegram-actions" style="display:none">
      <button class="secondary" style="flex:1" onclick="showTelegramForm()">🔄 Cambia</button>
      <button class="secondary" style="flex:1" onclick="testTelegram()">📨 Testa Telegram</button>
    </div>

    <label>Email (opzionale, in più a Telegram)</label>
    <input id="set-email" type="email" placeholder="lascia vuoto per non usarla">

    <div class="row-btns">
      <button style="flex:1" onclick="saveSettings()">Salva</button>
    </div>
    <div class="err" id="settings-err"></div>
    <div class="ok" id="settings-ok"></div>
  </div>
</div>

<div id="app">
  <div class="row">
    <h1>🎯 Cecchino Pro</h1>
    <div class="row" style="gap:10px">
      <span class="dim" id="email-display" onclick="openSettings()" style="cursor:pointer"></span>
      <button class="secondary" onclick="openSettings()" style="padding:6px 10px">⚙️</button>
    </div>
  </div>

  <!-- SCANNER -->
  <div class="tab-view active" id="tab-scanner">
    <div class="card">
      <div class="row">
        <div class="ticker-field" style="flex:1">
          <input id="scan-input" placeholder="Ticker, nome o crypto (es. MU, Micron, BTC-USD)" style="width:100%" autocapitalize="characters" autocomplete="off">
          <div class="suggest-box" id="scan-suggest"></div>
        </div>
        <button onclick="scanTicker()">Analizza</button>
      </div>
    </div>
    <div id="scan-result"></div>
  </div>

  <!-- PORTAFOGLIO -->
  <div class="tab-view" id="tab-portfolio">
    <div class="row" style="margin-bottom:10px; flex-wrap:wrap; gap:8px">
      <button class="secondary" onclick="refreshPortfolio()">🔄 Aggiorna prezzi</button>
      <button class="secondary" onclick="triggerPhotoImport()">📷 Importa da foto</button>
      <button onclick="toggleAddForm()">+ Aggiungi</button>
      <input type="file" id="photo-input" accept="image/*" capture="environment" style="display:none" onchange="importPhoto(this.files[0])">
    </div>
    <div class="card" id="add-form" style="display:none">
      <div class="form-grid">
        <div class="ticker-field full">
          <input id="pf-ticker" placeholder="Ticker o nome (anche crypto: BTC-USD)" style="width:100%" autocomplete="off">
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
    <div id="photo-import-result"></div>

    <div class="card">
      <div class="row">
        <b style="font-size:14px">📋 Verdetto giornaliero AI</b>
        <button class="secondary" onclick="refreshVerdict()" style="padding:6px 10px">🔄</button>
      </div>
      <div class="dim" style="font-size:11px;margin-top:4px">Basato sui segnali tecnici (RSI/medie/52 settimane/volumi), non su notizie in tempo reale — non sostituisce una verifica manuale prima di operare.</div>
      <div class="verdict-box dim" id="verdict-text" style="margin-top:10px">Premi 🔄 per generarlo (richiede GEMINI_API_KEY configurata).</div>
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
    <div class="dim" style="font-size:12px;margin-bottom:8px">🧭 Storico delle decisioni del Decision Engine — la stessa fonte usata per gli alert Telegram/mail. È l'unica decisione autorevole dell'app.</div>
    <div id="decisions-history-list"></div>
    <div class="card" style="margin-top:16px">
      <b style="font-size:14px">📊 Segnale tecnico (solo un livello del Decision Engine)</b>
      <div class="dim" style="font-size:12px;margin-top:4px">Log del solo indicatore tecnico, mostrato per trasparenza — NON è la decisione finale (vedi sopra).</div>
    </div>
    <div class="card" id="history-stats"></div>
    <div id="history-list"></div>
  </div>

  <!-- OPPORTUNITÀ -->
  <div class="tab-view" id="tab-opportunities">
    <div class="card">
      <div class="row">
        <div class="dim" style="font-size:12px">🤖 Scansione automatica di ~30 titoli "bottleneck" (monopoli tech: NVDA, ORCL, ASML, ...) non ancora nel tuo portafoglio, aggiornata ogni ora. Mostra solo segnali BUY forti (score ≥ 40).</div>
        <button class="secondary" onclick="refreshOpportunities()" style="white-space:nowrap">🔄 Aggiorna</button>
      </div>
    </div>
    <div id="opportunities-updated" class="dim" style="font-size:12px;margin-bottom:8px"></div>
    <div id="opportunities-list"></div>
  </div>

  <!-- LIVELLI (watchlist ingresso/stop/target) -->
  <div class="tab-view" id="tab-watchlist">
    <div class="card">
      <div class="row">
        <div class="dim" style="font-size:12px">🎯 Titoli con soglie di ingresso/stop/target definite a mano. Alert 🟢 quando entra in zona ingresso, 🔴 se rompe lo stop, 🎯 se raggiunge il target — su mail e/o Telegram.</div>
        <button class="secondary" onclick="refreshWatchlist()" style="white-space:nowrap">🔄 Aggiorna</button>
      </div>
    </div>
    <div id="watchlist-updated" class="dim" style="font-size:12px;margin-bottom:8px"></div>
    <div id="watchlist-list"></div>
    <div class="card">
      <b style="font-size:14px">📜 Ultimi segnali</b>
      <div id="watchlist-log" style="margin-top:8px"></div>
    </div>
  </div>

  <!-- SETTIMANALE (screener a 25 titoli con le 7 regole operative) -->
  <div class="tab-view" id="tab-screener25">
    <div class="card">
      <div class="row">
        <div class="dim" style="font-size:12px">📅 25 titoli — 7 in portafoglio, 15 in watchlist, 7 da evitare — con le regole anti-inseguimento, filtro FCF, concentrazione settoriale e massimo 2 BUY a settimana. Report completo automatico il lunedì.</div>
        <button class="secondary" onclick="refreshScreener25()" style="white-space:nowrap">🔄 Aggiorna</button>
      </div>
    </div>
    <div id="screener25-updated" class="dim" style="font-size:12px;margin-bottom:8px"></div>
    <div id="screener25-concentration"></div>
    <div id="screener25-buy"></div>
    <div id="screener25-list"></div>
  </div>

  <!-- BOTTLENECK FILTER (screener a due motori, dati live, universo multi-borsa) -->
  <div class="tab-view" id="tab-bottleneck">
    <div class="card">
      <div class="dim" style="font-size:12px">
        🎯 Due motori separati: <b>Motore A</b> (8 filtri quantitativi che giudicano l'azienda) e
        <b>Motore B</b> (Bottleneck Filter personale, 0-10 per criterio). I vincoli di portafoglio
        sono un terzo livello, applicato dopo e mostrato a parte. Screener informativo, criteri
        uniformi su dati pubblici, nessuna raccomandazione personalizzata.
      </div>
    </div>
    <div class="card">
      <div class="row">
        <div class="ticker-field" style="flex:1">
          <input id="bn-input" placeholder="Ticker o nome (es. ASML, Micron)" style="width:100%" autocapitalize="characters" autocomplete="off">
          <div class="suggest-box" id="bn-suggest"></div>
        </div>
        <button onclick="analyzeBottleneckTicker()">Analizza</button>
      </div>
      <div class="row" style="margin-top:8px">
        <button class="secondary" onclick="toggleBottleneckSliders()" style="flex:1">⚙️ Soglie</button>
        <button class="secondary" onclick="startBottleneckScan()" style="flex:1">🔍 Scansiona universo</button>
        <button class="secondary" onclick="toggleBottleneckAccuracy()" style="flex:1">📊 Accuratezza</button>
      </div>
    </div>
    <div id="bn-sliders" style="display:none"></div>
    <div id="bn-scan-status" class="dim" style="font-size:12px;margin-bottom:8px"></div>
    <div id="bn-accuracy" style="display:none"></div>
    <div id="bn-result"></div>
    <div id="bn-scan-results"></div>
  </div>
</div>

<div id="nav-more-backdrop" onclick="closeMoreSheet()"></div>
<div id="nav-more-sheet">
  <button id="more-watchlist" onclick="showTab('watchlist')">🎯 Livelli</button>
  <button id="more-screener25" onclick="showTab('screener25')">📅 Settimanale</button>
  <button id="more-opportunities" onclick="showTab('opportunities')">💡 Opportunità</button>
  <button id="more-history" onclick="showTab('history')">📜 Storico</button>
</div>
<nav class="bottom">
  <button id="nav-scanner" class="active" onclick="showTab('scanner')">🔍 Scanner</button>
  <button id="nav-portfolio" onclick="showTab('portfolio')">💼 Portafoglio</button>
  <button id="nav-bottleneck" onclick="showTab('bottleneck')">🎯 Bottleneck</button>
  <button id="nav-alerts" onclick="showTab('alerts')">🔔 Alert</button>
  <button id="nav-more" onclick="toggleMoreSheet()">☰ Altro</button>
</nav>

<script>
let CURRENT_EMAIL = {{ alert_email|tojson }};
let CURRENT_TELEGRAM = {{ telegram_chat_id|tojson }};

const TELEGRAM_ERROR_MESSAGES = {
  not_found: 'Non riesco ancora a trovare un messaggio del bot. Apri il bot su Telegram, premi Avvia e inviagli /start. Poi torna qui e riprova.',
  not_configured: 'Il bot Telegram non è ancora configurato su questo sito. Contatta chi gestisce Cecchino Pro.',
  network: 'Non riesco a contattare Telegram in questo momento. Controlla la connessione e riprova tra qualche secondo.',
  generic: 'Qualcosa non ha funzionato. Riprova tra qualche secondo.',
};

function humanizeTelegramError(data, status) {
  const raw = (data && data.error) || '';
  console.error('Telegram error:', status, raw); // dettaglio tecnico solo in console, mai mostrato
  if (status === 404 || /nessun messaggio/i.test(raw)) return TELEGRAM_ERROR_MESSAGES.not_found;
  if (status === 400 && /non configurato/i.test(raw)) return TELEGRAM_ERROR_MESSAGES.not_configured;
  if (status >= 500 || /rete/i.test(raw)) return TELEGRAM_ERROR_MESSAGES.network;
  return TELEGRAM_ERROR_MESSAGES.generic;
}

async function loadBotInfo() {
  const nameEl = document.getElementById('gate-bot-name');
  const userEl = document.getElementById('gate-bot-username');
  const linkEl = document.getElementById('gate-bot-link');
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.telegram_bot_username) {
      nameEl.textContent = data.telegram_bot_name || 'Bot Telegram';
      userEl.textContent = '@' + data.telegram_bot_username;
      linkEl.href = `https://t.me/${data.telegram_bot_username}`;
    } else if (!data.telegram_bot_configured) {
      nameEl.textContent = 'Bot non ancora configurato';
      userEl.textContent = 'Contatta chi gestisce il sito';
    } else {
      userEl.textContent = 'Apri Telegram e cerca il tuo bot';
    }
  } catch (e) { /* la card resta col placeholder, non blocca il resto */ }
}

function setGateStatus(state, text) {
  const el = document.getElementById('gate-status');
  el.className = 'gate-status' + (state ? ' ' + state : '');
  el.innerHTML = state === 'checking' ? `<span class="spin"></span><span>${text}</span>` : `<span>${text}</span>`;
}

function setGateError(msg) {
  const el = document.getElementById('gate-err');
  el.textContent = msg || '';
  el.classList.toggle('show', !!msg);
}

function setGateBusy(busy) {
  const btn = document.getElementById('gate-connect-btn');
  btn.disabled = busy;
  btn.textContent = busy ? 'Controllo Telegram…' : '📡 Collega Telegram';
}

function refreshEmailUI() {
  const gate = document.getElementById('email-gate');
  const display = document.getElementById('email-display');
  if (!CURRENT_TELEGRAM) {
    gate.style.display = 'flex';
    setGateStatus('', '⚠️ Telegram non è ancora collegato');
    setGateError('');
    display.textContent = '';
    loadBotInfo();
  } else {
    gate.style.display = 'none';
    display.textContent = '🔔 Telegram collegato' + (CURRENT_EMAIL ? ' + ✉️' : '');
  }
}

async function saveTelegramChatId(chatId) {
  setGateStatus('checking', 'Controllo Telegram…');
  setGateBusy(true);
  setGateError('');
  try {
    const res = await fetch('/api/settings/telegram', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chat_id: chatId}),
    });
    const data = await res.json();
    if (!res.ok) {
      setGateStatus('', '⚠️ Telegram non è ancora collegato');
      setGateError(humanizeTelegramError(data, res.status));
      return;
    }
    CURRENT_TELEGRAM = data.telegram_chat_id;
    setGateStatus('connected', '✓ Telegram collegato! Riceverai qui gli avvisi di Cecchino Pro.');
    setTimeout(refreshEmailUI, 1100);
  } catch (e) {
    setGateStatus('', '⚠️ Telegram non è ancora collegato');
    setGateError(TELEGRAM_ERROR_MESSAGES.network);
  } finally {
    setGateBusy(false);
  }
}

async function detectAndStartTelegram() {
  setGateError('');
  setGateBusy(true);
  setGateStatus('checking', 'Controllo Telegram…');
  try {
    const res = await fetch('/api/settings/telegram/detect', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      setGateStatus('', '⚠️ Telegram non è ancora collegato');
      setGateError(humanizeTelegramError(data, res.status));
      setGateBusy(false);
      return;
    }
    await saveTelegramChatId(data.telegram_chat_id);
  } catch (e) {
    setGateStatus('', '⚠️ Telegram non è ancora collegato');
    setGateError(TELEGRAM_ERROR_MESSAGES.network);
    setGateBusy(false);
  }
}

async function saveGateTelegram() {
  const chatId = document.getElementById('gate-telegram').value.trim();
  if (!chatId) {
    setGateError('Inserisci un Chat ID, oppure usa "Collega Telegram" sopra dopo aver scritto al bot.');
    return;
  }
  await saveTelegramChatId(chatId);
}

function showTelegramForm() {
  document.getElementById('settings-telegram-connected').style.display = 'none';
  document.getElementById('settings-telegram-form').style.display = 'block';
  document.getElementById('settings-telegram-actions').style.display = 'none';
}

function showTelegramConnected(chatId) {
  const masked = chatId.length > 3 ? '•••••' + chatId.slice(-3) : chatId;
  document.getElementById('settings-telegram-status-text').textContent = `✓ Telegram collegato — Chat ID: ${masked}`;
  document.getElementById('settings-telegram-connected').style.display = 'flex';
  document.getElementById('settings-telegram-form').style.display = 'none';
  document.getElementById('settings-telegram-actions').style.display = 'flex';
}

async function openSettings() {
  document.getElementById('settings-err').textContent = '';
  document.getElementById('settings-ok').textContent = '';
  document.getElementById('set-email').value = CURRENT_EMAIL || '';
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    document.getElementById('set-telegram').value = data.telegram_chat_id || '';
    if (data.telegram_chat_id) showTelegramConnected(data.telegram_chat_id);
    else showTelegramForm();
  } catch (e) { showTelegramForm(); }
  document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettings() {
  document.getElementById('settings-modal').style.display = 'none';
}

async function saveSettings() {
  const errEl = document.getElementById('settings-err');
  const okEl = document.getElementById('settings-ok');
  errEl.textContent = '';
  okEl.textContent = '';
  const email = document.getElementById('set-email').value.trim();
  const telegram = document.getElementById('set-telegram').value.trim();

  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email}),
  });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = data.error || 'Email non valida.'; return; }
  CURRENT_EMAIL = data.email;

  if (telegram) {
    await fetch('/api/settings/telegram', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chat_id: telegram}),
    });
    CURRENT_TELEGRAM = telegram;
    showTelegramConnected(telegram);
  }

  refreshEmailUI();
  okEl.textContent = 'Salvato ✓';
}

async function detectTelegramChatId() {
  const errEl = document.getElementById('settings-err');
  const okEl = document.getElementById('settings-ok');
  errEl.textContent = '';
  okEl.textContent = '';
  const res = await fetch('/api/settings/telegram/detect', { method: 'POST' });
  const data = await res.json();
  if (!res.ok) { errEl.textContent = humanizeTelegramError(data, res.status); return; }
  document.getElementById('set-telegram').value = data.telegram_chat_id;
  CURRENT_TELEGRAM = data.telegram_chat_id;
  showTelegramConnected(data.telegram_chat_id);
  okEl.textContent = `Trovato: ${data.name || data.telegram_chat_id} ✓`;
  refreshEmailUI();
}

async function testTelegram() {
  const errEl = document.getElementById('settings-err');
  const okEl = document.getElementById('settings-ok');
  errEl.textContent = '';
  okEl.textContent = 'Invio in corso…';
  try {
    const res = await fetch('/api/settings/telegram/test', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) {
      okEl.textContent = '';
      errEl.textContent = 'Non sono riuscito a mandare il messaggio di prova. Controlla di aver scritto al bot di recente.';
      return;
    }
    okEl.textContent = 'Messaggio di prova inviato ✓ — controlla Telegram';
  } catch (e) {
    okEl.textContent = '';
    errEl.textContent = 'Errore di rete durante l\'invio di prova.';
  }
}

function triggerPhotoImport() {
  document.getElementById('photo-input').click();
}

async function importPhoto(file) {
  if (!file) return;
  const resultEl = document.getElementById('photo-import-result');
  resultEl.innerHTML = '<div class="card"><div class="spinner">📷 Lettura della foto in corso (può richiedere qualche secondo)…</div></div>';
  const formData = new FormData();
  formData.append('image', file);
  try {
    const res = await fetch('/api/portfolio/import-photo', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) {
      resultEl.innerHTML = `<div class="card"><span class="dim">${data.error || 'Errore durante la lettura della foto'}</span></div>`;
      return;
    }
    const imported = data.imported || [];
    const skipped = data.skipped || [];
    let html = '';
    if (imported.length) {
      html += `<div class="card"><b>✅ Importate ${imported.length} posizioni:</b><div class="dim" style="margin-top:6px">` +
        imported.map(i => `${i.ticker}: ${i.qty} unità, carico stimato €${i.paid}`).join('<br>') + '</div></div>';
    }
    if (skipped.length) {
      html += `<div class="card"><b>⚠️ Non importate (${skipped.length}):</b><div class="dim" style="margin-top:6px">` +
        skipped.map(s => `${s.ticker || '?'}: ${s.motivo}`).join('<br>') + '</div></div>';
    }
    resultEl.innerHTML = html || '<div class="card"><span class="dim">Nessuna posizione riconosciuta nella foto.</span></div>';
    loadPortfolio();
  } catch (e) {
    resultEl.innerHTML = '<div class="card"><span class="dim">Errore di rete durante l\'invio della foto.</span></div>';
  }
  document.getElementById('photo-input').value = '';
}

async function loadVerdict() {
  try {
    const res = await fetch('/api/verdict');
    const data = await res.json();
    document.getElementById('verdict-text').textContent = data.text || 'Premi 🔄 per generarlo (richiede GEMINI_API_KEY configurata).';
  } catch (e) { /* silenzioso, resta il testo di default */ }
}

async function refreshVerdict() {
  document.getElementById('verdict-text').textContent = '🤖 Generazione in corso…';
  const res = await fetch('/api/verdict/refresh', { method: 'POST' });
  const data = await res.json();
  document.getElementById('verdict-text').textContent = data.text || data.error || 'Errore';
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
attachTickerSuggest('bn-input', 'bn-suggest', (it) => analyzeBottleneckTicker());

const NAV_OVERFLOW_TABS = ['watchlist', 'screener25', 'opportunities', 'history'];

function showTab(name) {
  document.querySelectorAll('.tab-view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.bottom button, #nav-more-sheet button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (NAV_OVERFLOW_TABS.includes(name)) {
    document.getElementById('nav-more').classList.add('active');
    document.getElementById('more-' + name).classList.add('active');
  } else {
    document.getElementById('nav-' + name).classList.add('active');
  }
  closeMoreSheet();
  if (name === 'portfolio') { loadPortfolio(); loadVerdict(); }
  if (name === 'alerts') loadAlerts();
  if (name === 'history') loadHistory();
  if (name === 'opportunities') loadOpportunities();
  if (name === 'watchlist') { loadWatchlist(); loadWatchlistLog(); }
  if (name === 'screener25') loadScreener25();
  if (name === 'bottleneck') initBottleneck();
}

function toggleMoreSheet() {
  document.getElementById('nav-more-sheet').classList.toggle('show');
  document.getElementById('nav-more-backdrop').classList.toggle('show');
}

function closeMoreSheet() {
  document.getElementById('nav-more-sheet').classList.remove('show');
  document.getElementById('nav-more-backdrop').classList.remove('show');
}

function renderAnalysisCard(a, extraButtons) {
  if (a.error) {
    return `<div class="card"><div class="row"><b>${a.ticker}</b><span class="dim">${a.error}</span></div></div>`;
  }
  const stopTile = a.stop_loss != null
    ? `<div class="metric"><div class="val" style="color:var(--sell)">${a.stop_loss}</div><div class="lbl">Stop loss 🛡️</div></div>`
    : '';
  const metrics = `
    <div class="metrics">
      <div class="metric"><div class="val">${a.rsi}</div><div class="lbl">RSI 14</div></div>
      <div class="metric"><div class="val">${a.ma50}</div><div class="lbl">MA50</div></div>
      <div class="metric"><div class="val">${a.score}</div><div class="lbl">Score tecnico</div></div>
      <div class="metric"><div class="val">${a.dist_high52}%</div><div class="lbl">da Max 52W</div></div>
      <div class="metric"><div class="val">${a.dist_low52}%</div><div class="lbl">da Min 52W</div></div>
      <div class="metric"><div class="val">${a.vol_ratio != null ? a.vol_ratio + 'x' : '—'}</div><div class="lbl">Volume/media</div></div>
      <div class="metric"><div class="val">${a.suggested_buy}</div><div class="lbl">Zona acquisto 🤖</div></div>
      <div class="metric"><div class="val">${a.suggested_sell}</div><div class="lbl">Zona vendita 🤖</div></div>
      ${stopTile}
      <div class="metric"><div class="val">${a.updated.slice(11,16)}</div><div class="lbl">Aggiornato</div></div>
    </div>`;
  const reasons = `<div class="reasons">${a.reasons.map(r => `<div>• ${r}</div>`).join('')}</div>`;
  const aiNote = a.ai_commentary ? `<div class="ai-note">🤖 <b>AI:</b> ${a.ai_commentary}</div>` : '';
  return `
    <div class="card stripe ${a.signal}">
      <div class="row">
        <div>
          <div class="ticker-name">${a.ticker} <span class="dim">${a.name || ''}</span></div>
          <div class="dim">${a.price} ${a.currency} (${a.day_chg >= 0 ? '+' : ''}${a.day_chg}%)</div>
        </div>
        <div style="text-align:right">
          <div class="dim" style="font-size:11px">Segnale tecnico</div>
          <span class="pill ${a.signal}">${a.signal}</span>
        </div>
      </div>
      ${metrics}
      ${reasons}
      ${aiNote}
      ${extraButtons || ''}
    </div>`;
}

// --------------------------------------------------------------------------
// Scanner: card unificata. Il Decision Engine è l'UNICA fonte della
// decisione finale mostrata — il segnale tecnico resta visibile ma solo
// come uno dei 4 livelli che lo alimentano, mai come una seconda
// "decisione" indipendente (era esattamente il bug: card tecnica BUY,
// Decision Engine HOLD, due voci diverse per lo stesso ticker).
// --------------------------------------------------------------------------
const DECISION_LABELS = {
  BUY: 'BUY', SELL: 'SELL', HOLD: 'HOLD',
  BUY_BLOCKED: 'BUY BLOCCATO', DATA_UNAVAILABLE: 'DATI NON DISPONIBILI',
};

function timeAgo(iso) {
  if (!iso) return null;
  const diffMin = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (diffMin < 1) return 'adesso';
  if (diffMin < 60) return `${diffMin} min fa`;
  const h = Math.floor(diffMin / 60), m = diffMin % 60;
  return `${h}h${m ? ' ' + m + 'min' : ''} fa`;
}

function layerCoverageRow(label, score) {
  return score != null
    ? `<div class="row" style="font-size:13px;padding:5px 0;border-bottom:1px solid var(--border)"><span>✓ ${label}</span><span class="dim">${Math.round(score)}/100</span></div>`
    : `<div class="row" style="font-size:13px;padding:5px 0;border-bottom:1px solid var(--border)"><span style="color:var(--sell)">⚠ ${label}</span><span class="dim">NON DISPONIBILE</span></div>`;
}

function newsEventRow(e) {
  const dot = e.direction === 'negative' ? '🔴' : e.direction === 'positive' ? '🟢' : '⚪';
  const age = timeAgo(e.published_at ? new Date(e.published_at * 1000).toISOString() : null) || '';
  const verifBadge = { VERIFIED: '✓ Verificata', SECONDARY: 'Secondaria', UNVERIFIED: '⚠ Non verificata' }[e.verification_status] || '';
  const link = e.url ? `<a href="${e.url}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:underline">${e.headline}</a>` : e.headline;
  return `
    <div style="padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="font-size:13px">${dot} <b>${e.source || 'fonte sconosciuta'}</b> · ${age}</div>
      <div style="font-size:13px;margin-top:2px">${link}</div>
      <div class="dim" style="font-size:11px;margin-top:2px">${e.severity_label.toUpperCase()} — ${verifBadge}</div>
    </div>`;
}

function renderScannerResult(a, d) {
  if (a.error) {
    return `<div class="card"><div class="row"><b>${a.ticker}</b><span class="dim">${a.error}</span></div></div>`;
  }
  const decision = (d && !d.error) ? d.decision : 'DATA_UNAVAILABLE';
  const label = DECISION_LABELS[decision] || decision;
  const freshness = timeAgo(a.updated);
  const isStale = a.updated && (Date.now() - new Date(a.updated).getTime()) / 60000 > 60;

  const coverage = d && d.coverage_pct != null ? d.coverage_pct : null;
  const coverageBadge = coverage != null
    ? `<span style="color:${coverage >= 65 ? 'var(--buy)' : 'var(--sell)'};font-weight:700">${coverage}% ${coverage >= 65 ? '✓' : '⚠'}</span>`
    : '<span class="dim">n/d</span>';

  const layers = (d && d.layers) || {};
  const layerRows = layerCoverageRow('Technical', layers.technical)
    + layerCoverageRow('Fundamental', layers.fundamental)
    + layerCoverageRow('Bottleneck', layers.bottleneck)
    + layerCoverageRow('News', layers.news);

  const newsEvents = (d && d.news_events) || [];
  const newsPanel = `
    <div class="card">
      <b style="font-size:14px">📰 News Intelligence</b>
      ${newsEvents.length
        ? newsEvents.slice(0, 8).map(newsEventRow).join('')
        : '<div class="dim" style="font-size:13px;margin-top:8px">Nessun evento rilevante trovato nelle notizie recenti.</div>'}
    </div>`;

  const ws = d && d.weekly_screener;
  const wsPanel = ws ? `
    <div class="card">
      <b style="font-size:14px">📅 Weekly Screener</b>
      <div class="row" style="margin-top:8px"><span class="dim">Stato</span><b>${ws.category.toUpperCase()}</b></div>
      ${ws.fcf_negative ? '<div class="row" style="margin-top:4px"><span class="dim">FCF</span><span style="color:var(--sell)">⚠ NEGATIVO</span></div>' : ''}
      ${ws.note ? `<div class="dim" style="font-size:12px;margin-top:8px">Reason: ${ws.note}</div>` : ''}
      ${ws.conflict ? '<div style="margin-top:8px;padding:8px;border-radius:8px;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);font-size:12px">⚠️ In conflitto con il Decision Engine — vince la prudenza.</div>' : ''}
    </div>` : '';

  const codes = (d && d.reason_codes || []).map(c => `<div>• ${c}</div>`).join('') || '<div class="dim">Nessun codice motivazione</div>';

  return `
    <div class="card stripe ${decision === 'BUY' ? 'BUY' : decision === 'SELL' ? 'SELL' : 'HOLD'}">
      <div class="row">
        <div>
          <div class="ticker-name">${a.ticker} <span class="dim">${a.name || ''}</span></div>
          <div class="dim">${a.price} ${a.currency} (${a.day_chg >= 0 ? '+' : ''}${a.day_chg}%)</div>
        </div>
      </div>
      <div style="margin-top:12px;text-align:center;padding:14px;border-radius:10px;background:var(--card2)">
        <div class="dim" style="font-size:12px;letter-spacing:0.5px">FINAL DECISION</div>
        <div style="font-size:26px;font-weight:800;color:${decisionColor(decision)};margin-top:4px">${label}</div>
      </div>
      <div class="row" style="margin-top:10px;font-size:12px">
        <span class="dim">Dati aggiornati: ${freshness || '—'}</span>
        ${isStale ? '<span style="color:var(--sell)">⚠ DATI NON RECENTI</span>' : ''}
      </div>
      <div class="row" style="margin-top:12px">
        <span style="font-size:13px;font-weight:600">Data Coverage</span>
        ${coverageBadge}
      </div>
      <div style="margin-top:6px">${layerRows}</div>
      <div class="reasons" style="margin-top:10px"><b style="font-size:13px">PERCHÉ</b>${codes}</div>
      <button style="margin-top:12px;width:100%" onclick="quickAdd('${a.ticker}', ${a.price})">＋ Aggiungi al portafoglio</button>
    </div>
    ${newsPanel}
    ${wsPanel}`;
}

async function scanTicker() {
  const ticker = document.getElementById('scan-input').value.trim().toUpperCase();
  if (!ticker) return;
  document.getElementById('scan-result').innerHTML = '<div class="spinner">Analisi in corso… (prezzo → tecnico → fondamentali → bottleneck → news → verifica → decisione)</div>';
  const [scanRes, decisionRes] = await Promise.all([
    fetch(`/api/scan/${ticker}`).then(r => r.json()).catch(() => ({ ticker, error: 'Errore di rete' })),
    fetch(`/api/decisions/${ticker}`).then(r => r.json()).catch(() => null),
  ]);
  document.getElementById('scan-result').innerHTML = renderScannerResult(scanRes, decisionRes);
}

function decisionColor(d) {
  return { BUY: 'var(--buy)', SELL: 'var(--sell)', HOLD: '#eab308', BUY_BLOCKED: 'var(--sell)', DATA_UNAVAILABLE: 'var(--dim)' }[d] || 'var(--dim)';
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

async function loadDecisionsHistory() {
  const el = document.getElementById('decisions-history-list');
  el.innerHTML = '<div class="spinner">Caricamento…</div>';
  try {
    const res = await fetch('/api/decisions/history');
    const rows = await res.json();
    if (!rows.length) {
      el.innerHTML = '<div class="card"><div class="dim">Nessun cambio di decisione ancora registrato.</div></div>';
      return;
    }
    el.innerHTML = rows.map(d => {
      const transition = d.previous_decision ? `${d.previous_decision} → ${d.decision}` : d.decision;
      const codes = (d.reason_codes || []).map(c => `<div>• ${c}</div>`).join('');
      return `
      <div class="card stripe ${d.decision === 'BUY' ? 'BUY' : d.decision === 'SELL' ? 'SELL' : 'HOLD'}">
        <div class="row">
          <div>
            <b>${d.ticker}</b>
            <span style="color:${decisionColor(d.decision)};font-weight:700;margin-left:6px">${transition}</span>
            <div class="dim">${d.price != null ? d.price : ''} ${d.final_score != null ? '· score ' + d.final_score : ''}</div>
          </div>
          <span class="dim">${(d.ts || '').replace('T', ' ').slice(0, 16)}</span>
        </div>
        ${codes ? `<div class="reasons">${codes}</div>` : ''}
        <div class="dim" style="font-size:11px;margin-top:6px">Engine v${d.filter_version}</div>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="card"><div class="dim">Errore nel caricamento dello storico decisioni.</div></div>';
  }
}

async function loadHistory() {
  loadDecisionsHistory();
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

async function loadOpportunities() {
  document.getElementById('opportunities-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/opportunities');
  const data = await res.json();
  renderOpportunities(data);
}

async function refreshOpportunities() {
  document.getElementById('opportunities-list').innerHTML = '<div class="spinner">Scansione del mercato in corso, può richiedere qualche minuto…</div>';
  const res = await fetch('/api/opportunities/refresh', { method: 'POST' });
  const data = await res.json();
  renderOpportunities(data);
}

function renderOpportunities(data) {
  document.getElementById('opportunities-updated').textContent = data.updated
    ? `Ultima scansione: ${data.updated.replace('T', ' ').slice(0, 16)}`
    : 'Nessuna scansione ancora eseguita: premi "Aggiorna" oppure attendi il prossimo giro automatico (ogni ora).';
  const container = document.getElementById('opportunities-list');
  if (!data.results || data.results.length === 0) {
    container.innerHTML = '<div class="dim">Nessuna opportunità BUY forte al momento fuori dal tuo portafoglio.</div>';
    return;
  }
  container.innerHTML = data.results.map(a => {
    const btn = `<button style="margin-top:10px" onclick="quickAdd('${a.ticker}', ${a.price})">+ Aggiungi al portafoglio</button>`;
    return renderAnalysisCard(a, btn);
  }).join('');
}

const WATCH_STATUS_CLASS = {STOP: 'SELL', TARGET: 'BUY', INGRESSO: 'BUY', 'IN ATTESA': 'HOLD'};

async function loadWatchlist() {
  document.getElementById('watchlist-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/watchlist');
  renderWatchlist(await res.json());
}

async function refreshWatchlist() {
  document.getElementById('watchlist-list').innerHTML = '<div class="spinner">Controllo prezzi in corso…</div>';
  const res = await fetch('/api/watchlist/refresh', { method: 'POST' });
  renderWatchlist(await res.json());
  loadWatchlistLog();
}

function renderWatchlist(items) {
  const container = document.getElementById('watchlist-list');
  const updEl = document.getElementById('watchlist-updated');
  if (!items || items.length === 0) {
    container.innerHTML = '<div class="dim">Nessun titolo in watchlist.</div>';
    updEl.textContent = '';
    return;
  }
  const latest = items.map(i => i.updated).filter(Boolean).sort().pop();
  updEl.textContent = latest ? `Ultimo controllo: ${latest.replace('T', ' ').slice(0, 16)}` : 'Non ancora controllato: premi "Aggiorna".';

  container.innerHTML = items.map(a => {
    const cls = WATCH_STATUS_CLASS[a.status] || 'HOLD';
    const entryTxt = (a.entry_low != null && a.entry_high != null) ? `${a.entry_low}–${a.entry_high}`
      : (a.entry_high != null ? `sotto ${a.entry_high}` : '—');
    const targetTxt = (a.target_low != null && a.target_high != null && a.target_low !== a.target_high)
      ? `${a.target_low}–${a.target_high}` : (a.target_low != null ? a.target_low : '—');
    return `
      <div class="card stripe ${cls}">
        <div class="row">
          <div>
            <div class="ticker-name">${a.ticker} <span class="dim">${a.name || ''}</span></div>
            <div class="dim">${a.price != null ? a.price + ' €' : 'prezzo non ancora controllato'}</div>
          </div>
          <span class="pill ${cls}">${a.status}</span>
        </div>
        <div class="metrics">
          <div class="metric"><div class="val">${entryTxt}</div><div class="lbl">Ingresso €</div></div>
          <div class="metric"><div class="val">${a.stop_price ?? '—'}</div><div class="lbl">Stop €</div></div>
          <div class="metric"><div class="val">${targetTxt}</div><div class="lbl">Target €</div></div>
          <div class="metric"><div class="val">${a.dist_to_entry_pct != null ? a.dist_to_entry_pct + '%' : '—'}</div><div class="lbl">a ingresso</div></div>
          <div class="metric"><div class="val">${a.dist_to_stop_pct != null ? a.dist_to_stop_pct + '%' : '—'}</div><div class="lbl">sopra stop</div></div>
          <div class="metric"><div class="val">${a.dist_to_target_pct != null ? a.dist_to_target_pct + '%' : '—'}</div><div class="lbl">a target</div></div>
        </div>
      </div>`;
  }).join('');
}

async function loadWatchlistLog() {
  const el = document.getElementById('watchlist-log');
  el.innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/watchlist/log');
  const log = await res.json();
  if (!log.length) {
    el.innerHTML = '<div class="dim">Nessun segnale ancora registrato.</div>';
    return;
  }
  const emoji = {entry: '🟢', stop: '🔴', target: '🎯'};
  el.innerHTML = log.map(l => `
    <div class="row" style="padding:6px 0;border-bottom:1px solid var(--border)">
      <span>${emoji[l.signal_type] || ''} <b>${l.ticker}</b> a ${l.price}€</span>
      <span class="dim" style="font-size:11px">${l.timestamp}</span>
    </div>`).join('');
}

const SCREENER25_CLASS = {BUY_FORTE: 'BUY', WATCH: 'HOLD', HOLD: 'HOLD', SELL: 'SELL', ESCLUSO: 'SELL', 'N/D': 'HOLD'};
const SCREENER25_MAX_BUY = 2;

async function loadScreener25() {
  document.getElementById('screener25-list').innerHTML = '<div class="spinner">Caricamento…</div>';
  const res = await fetch('/api/screener25');
  renderScreener25(await res.json());
}

async function refreshScreener25() {
  document.getElementById('screener25-list').innerHTML = '<div class="spinner">Scansione dei 25 titoli in corso, può richiedere qualche minuto…</div>';
  const res = await fetch('/api/screener25/refresh', { method: 'POST' });
  renderScreener25(await res.json());
}

function screener25Card(r, extraBadge) {
  const cls = SCREENER25_CLASS[r.signal] || 'HOLD';
  const priceTxt = r.price != null ? `${r.price} ${r.currency || ''}` : 'dati non disponibili';
  return `
    <div class="card stripe ${cls}">
      <div class="row">
        <div>
          <div class="ticker-name">${r.ticker} <span class="dim">${r.name || ''}</span></div>
          <div class="dim">${priceTxt}${r.chg_5d != null ? ' · 5gg ' + (r.chg_5d >= 0 ? '+' : '') + r.chg_5d + '%' : ''}</div>
        </div>
        <span class="pill ${cls}">${extraBadge || r.signal.replace('_', ' ')}</span>
      </div>
      <div class="reasons">${(r.reasons || []).map(x => `<div>• ${x}</div>`).join('')}</div>
    </div>`;
}

function renderScreener25(data) {
  document.getElementById('screener25-updated').textContent = data.updated
    ? `Ultimo calcolo: ${data.updated.replace('T', ' ').slice(0, 16)} (settimana ${data.week_key || ''})`
    : 'Non ancora calcolato: premi "Aggiorna" oppure attendi il report automatico di lunedì.';

  const concEl = document.getElementById('screener25-concentration');
  if (data.concentration_alerts && data.concentration_alerts.length) {
    concEl.innerHTML = `<div class="card" style="border-color:var(--sell)">
      <b style="color:var(--sell)">⚠️ Concentrazione oltre soglia</b>
      <div class="dim" style="margin-top:6px">${data.concentration_alerts.join('<br>')}</div>
    </div>`;
  } else {
    concEl.innerHTML = '';
  }

  const results = data.results || [];
  const buyAll = results.filter(r => r.signal === 'BUY_FORTE');
  const buyTop = buyAll.slice(0, SCREENER25_MAX_BUY);
  const buyRest = buyAll.slice(SCREENER25_MAX_BUY);

  const buyEl = document.getElementById('screener25-buy');
  if (buyTop.length) {
    buyEl.innerHTML = `<div class="dim" style="font-size:12px;margin:10px 0 6px">🟢 BUY FORTE (max ${SCREENER25_MAX_BUY} a settimana)</div>`
      + buyTop.map(r => screener25Card(r)).join('')
      + (buyRest.length ? `<div class="dim" style="font-size:12px;margin:6px 0">${buyRest.length} altri BUY validi ma in coda questa settimana: ${buyRest.map(r => r.ticker).join(', ')}</div>` : '');
  } else {
    buyEl.innerHTML = '';
  }

  const owned = results.filter(r => r.category === 'owned');
  const watchlist = results.filter(r => r.category === 'watchlist' && r.signal !== 'BUY_FORTE');
  const excluded = results.filter(r => r.category === 'excluded');

  let html = '';
  if (owned.length) {
    html += '<div class="dim" style="font-size:12px;margin:10px 0 6px">🏠 Portafoglio</div>' + owned.map(r => screener25Card(r)).join('');
  }
  if (watchlist.length) {
    html += '<div class="dim" style="font-size:12px;margin:10px 0 6px">👀 Watchlist</div>' + watchlist.map(r => screener25Card(r)).join('');
  }
  if (excluded.length) {
    html += '<div class="dim" style="font-size:12px;margin:10px 0 6px">🚫 Da evitare (non suggeriti anche se lo screener li troverebbe validi)</div>'
      + excluded.map(r => `
        <div class="card" style="padding:10px 14px">
          <div class="row"><b>${r.ticker}</b><span class="dim">${r.name || ''}</span></div>
          <div class="dim" style="font-size:12px;margin-top:4px">${r.reasons[0] || ''}</div>
        </div>`).join('');
  }
  document.getElementById('screener25-list').innerHTML = html || '<div class="dim">Nessun dato ancora calcolato.</div>';
}

// --------------------------------------------------------------------------
// Bottleneck Filter — screener a due motori
// --------------------------------------------------------------------------
let BN_THRESHOLDS = null;
let BN_LAST_TICKER = null;
let BN_SCAN_TIMER = null;

async function initBottleneck() {
  if (!BN_THRESHOLDS) {
    const res = await fetch('/api/bottleneck/thresholds');
    BN_THRESHOLDS = await res.json();
    renderBottleneckSliders();
  }
  pollBottleneckScanStatus();
}

const BN_LABELS = {
  drawdown_min_pct: 'Drawdown minimo da max 52w (%)',
  return_3y_max_pct: 'Tetto rendimento 3 anni (%)',
  pe_max: 'P/E massimo',
  dislocation_min: 'Dislocazione minima (x)',
  net_debt_ebitda_max: 'Debito netto/EBITDA massimo',
  min_analyst_coverage: 'Copertura analisti minima',
  catalyst_window_days: 'Finestra catalizzatore (giorni)',
  buy_score_min: 'Punteggio minimo COMPRA (Motore B)',
  hype_max: 'Hype massimo (Motore B)',
  growth_min_pct: 'Crescita ricavi minima (%)',
  watch_score_min: 'Punteggio minimo ATTENDI (Motore B)',
  max_pct_per_stock: 'Max % per titolo',
  max_pct_per_sector: 'Max % per settore',
  min_pct_defensive: 'Min % in difensivi',
};
const BN_RANGES = {
  drawdown_min_pct: [0, 60, 1], return_3y_max_pct: [0, 500, 10], pe_max: [5, 80, 1],
  dislocation_min: [0.5, 5, 0.1], net_debt_ebitda_max: [0, 8, 0.1], min_analyst_coverage: [0, 15, 1],
  catalyst_window_days: [7, 180, 1], buy_score_min: [15, 50, 1], hype_max: [0, 10, 0.5],
  growth_min_pct: [0, 100, 1], watch_score_min: [10, 40, 1], max_pct_per_stock: [2, 40, 1],
  max_pct_per_sector: [10, 80, 1], min_pct_defensive: [0, 40, 1],
};

function toggleBottleneckSliders() {
  const el = document.getElementById('bn-sliders');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function renderBottleneckSliders() {
  const groups = [['engine_a', 'Motore A — filtri quantitativi'], ['engine_b', 'Motore B — Bottleneck'], ['portfolio', 'Vincoli di portafoglio']];
  let html = '';
  for (const [group, label] of groups) {
    html += `<div class="card"><b style="font-size:13px">${label}</b>`;
    for (const key of Object.keys(BN_THRESHOLDS[group])) {
      const val = BN_THRESHOLDS[group][key];
      const [min, max, step] = BN_RANGES[key] || [0, 100, 1];
      html += `
        <div style="margin-top:10px">
          <div class="row" style="font-size:12px"><span>${BN_LABELS[key] || key}</span><span class="dim" id="bn-val-${group}-${key}">${val}</span></div>
          <input type="range" min="${min}" max="${max}" step="${step}" value="${val}" style="width:100%"
                 oninput="bnSliderChange('${group}','${key}', this.value)">
        </div>`;
    }
    html += '</div>';
  }
  document.getElementById('bn-sliders').innerHTML = html;
}

function bnSliderChange(group, key, value) {
  const v = parseFloat(value);
  BN_THRESHOLDS[group][key] = v;
  document.getElementById(`bn-val-${group}-${key}`).textContent = v;
  if (BN_LAST_TICKER) analyzeBottleneckTicker(BN_LAST_TICKER, false);
}

function bnStatusColor(status) {
  return { IDONEO: 'var(--buy)', A_UN_FILTRO: '#e6a817', DATI_INCOMPLETI: 'var(--dim)', ESCLUSO: 'var(--sell)',
           COMPRA: 'var(--buy)', ATTENDI: '#e6a817', PASSA: 'var(--sell)' }[status] || 'var(--dim)';
}
function bnIcon(status) {
  return { pass: '✅', fail: '❌', missing: '➖' }[status] || '➖';
}
function bnFmt(v) {
  return v == null ? '—' : (typeof v === 'number' ? Math.round(v * 100) / 100 : v);
}

function bottleneckFilterRow(f) {
  return `<div class="row" style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
    <span>${bnIcon(f.status)} ${f.label}</span>
    <span class="dim">${bnFmt(f.value)}${f.unit || ''} ${f.threshold != null ? '(soglia ' + f.threshold + (f.unit || '') + ')' : ''}</span>
  </div>`;
}

function bottleneckCard(r) {
  if (r.error) {
    return `<div class="card"><div class="row"><b>${r.ticker}</b><span class="dim">${r.error}</span></div></div>`;
  }
  const a = r.engine_a, b = r.engine_b, p = r.portfolio;
  const levelLabel = { motore_a: 'Motore A (fondamentali)', motore_b: 'Motore B (Bottleneck)', portafoglio: 'Vincoli di portafoglio' };
  const blockedText = r.blocked_by_level
    ? `<div style="color:var(--sell);font-size:12px;margin-top:8px">⛔ Bloccato a livello: ${levelLabel[r.blocked_by_level]}</div>`
    : `<div style="color:var(--buy);font-size:12px;margin-top:8px">✅ Nessun livello blocca questo titolo</div>`;
  return `
    <div class="card">
      <div class="row"><b>${r.ticker}</b><span class="dim">${r.name || ''} — ${bnFmt(r.price)}</span></div>
      <div style="margin-top:8px">
        <div class="row"><b style="font-size:13px">Motore A — fondamentali</b><span style="color:${bnStatusColor(a.status)};font-weight:700;font-size:12px">${a.status}</span></div>
        ${a.filters.map(bottleneckFilterRow).join('')}
      </div>
      <div style="margin-top:10px">
        <div class="row"><b style="font-size:13px">Motore B — Bottleneck (${bnFmt(b.total)}/50)</b><span style="color:${bnStatusColor(b.verdict)};font-weight:700;font-size:12px">${b.verdict}</span></div>
        ${Object.entries(b.scores).map(([k, v]) => `
          <div class="row" style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
            <span>${k}</span><span class="dim">${bnFmt(v)}/10</span>
          </div>`).join('')}
      </div>
      <div style="margin-top:10px">
        <div class="row"><b style="font-size:13px">Vincoli di portafoglio</b><span class="dim" style="font-size:12px">${p.blocked ? '⛔' : '✅'}</span></div>
        ${p.checks.map(c => `
          <div class="row" style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
            <span>${bnIcon(c.status)} ${c.label}</span><span class="dim">${bnFmt(c.value)}${c.threshold != null ? ' (soglia ' + c.threshold + ')' : ''}</span>
          </div>`).join('')}
      </div>
      ${blockedText}
    </div>`;
}

async function analyzeBottleneckTicker(tickerArg, save) {
  const ticker = (tickerArg && typeof tickerArg === 'string') ? tickerArg : document.getElementById('bn-input').value.trim();
  if (!ticker) return;
  BN_LAST_TICKER = ticker;
  document.getElementById('bn-result').innerHTML = '<div class="spinner">Analisi in corso…</div>';
  const res = await fetch(`/api/bottleneck/analyze/${encodeURIComponent(ticker.toUpperCase())}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thresholds: BN_THRESHOLDS, save: save !== false }),
  });
  const data = await res.json();
  document.getElementById('bn-result').innerHTML = bottleneckCard(data);
}

async function startBottleneckScan() {
  const res = await fetch('/api/bottleneck/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thresholds: BN_THRESHOLDS }),
  });
  if (res.status === 409) {
    document.getElementById('bn-scan-status').textContent = 'Scansione già in corso…';
  }
  pollBottleneckScanStatus();
}

async function pollBottleneckScanStatus() {
  clearTimeout(BN_SCAN_TIMER);
  const res = await fetch('/api/bottleneck/scan/status');
  const s = await res.json();
  const statusEl = document.getElementById('bn-scan-status');
  if (s.running) {
    statusEl.textContent = `🔍 Scansione in corso: ${s.done}/${s.total} titoli…`;
    BN_SCAN_TIMER = setTimeout(pollBottleneckScanStatus, 4000);
  } else if (s.finished_at) {
    statusEl.textContent = `Ultima scansione completata: ${s.finished_at.slice(0, 16).replace('T', ' ')} (${s.total} titoli)`;
    loadBottleneckScanResults();
  } else {
    statusEl.textContent = '';
  }
}

async function loadBottleneckScanResults() {
  const res = await fetch('/api/bottleneck/scan/results');
  const data = await res.json();
  const el = document.getElementById('bn-scan-results');
  if (!data.results.length) {
    el.innerHTML = `<div class="dim" style="font-size:12px;margin:10px 0">Nessun titolo IDONEO su entrambi i motori nell'ultima scansione (${data.total_scanned} analizzati). Ordinati per dislocazione quando presenti.</div>`;
    return;
  }
  el.innerHTML = '<div class="dim" style="font-size:12px;margin:10px 0 6px">✅ IDONEI su entrambi i motori, ordinati per dislocazione</div>'
    + data.results.map(bottleneckCard).join('');
}

function toggleBottleneckAccuracy() {
  const el = document.getElementById('bn-accuracy');
  if (el.style.display === 'none') {
    el.style.display = 'block';
    loadBottleneckAccuracy();
  } else {
    el.style.display = 'none';
  }
}

async function loadBottleneckAccuracy() {
  const el = document.getElementById('bn-accuracy');
  el.innerHTML = '<div class="spinner">Carico…</div>';
  const res = await fetch('/api/accuratezza');
  const data = await res.json();
  if (!data.per_filter.length) {
    el.innerHTML = "<div class=\"card dim\" style=\"font-size:12px\">Ancora nessuna decisione ricontrollata a 3/6/12 mesi: torna qui più avanti, il registro si popola con l'uso.</div>";
    return;
  }
  let html = '<div class="card"><b style="font-size:13px">📊 Accuratezza per filtro</b>'
    + '<div class="dim" style="font-size:11px;margin:4px 0 8px">Tra i titoli esclusi da ciascun filtro, quanti sono poi saliti e quanti scesi.</div>';
  html += data.per_filter.map(f => `
    <div class="row" style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
      <span>${f.filter}</span>
      <span class="dim">🟢 ${f.escluso_salito} / 🔴 ${f.escluso_sceso} (${f.pct_salito_dopo_esclusione ?? '—'}% saliti, n=${f.n})</span>
    </div>`).join('');
  html += '</div>';
  if (data.suggestions.length) {
    html += '<div class="card"><b style="font-size:13px">💡 Suggerimenti di ricalibrazione</b>'
      + data.suggestions.map(s => `<div class="dim" style="font-size:12px;margin-top:6px">${s}</div>`).join('')
      + '<div class="dim" style="font-size:11px;margin-top:8px">Solo suggerimenti: le soglie non vengono cambiate automaticamente.</div></div>';
  }
  el.innerHTML = html;
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
