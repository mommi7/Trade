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
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # colonna già esistente

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
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify({
        "email": get_alert_email(),
        "telegram_chat_id": get_telegram_chat_id(),
        "telegram_bot_configured": bool(config.TELEGRAM_BOT_TOKEN),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    if not EMAIL_RE.match(email):
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
    broadcast(subject, body)


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
        try:
            refresh_all_portfolio()
            check_watch_levels()
            run_market_screener()
            generate_daily_verdict()
        except Exception as e:
            print(f"Errore monitor (riprova in 5 min): {e}")
            time.sleep(300)
            continue
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
@app.route("/api/cron/tick", methods=["POST"])
def api_cron_tick():
    secret = request.headers.get("X-Cron-Secret", "")
    if not config.CRON_SECRET or secret != config.CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    refresh_all_portfolio()
    check_watch_levels()
    run_market_screener()
    generate_daily_verdict()
    return jsonify({"ok": True})


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
    <h1>🎯 Cecchino Pro</h1>
    <p>Inserisci la tua email: è l'indirizzo a cui arriveranno gli alert BUY/SELL.</p>
    <input id="gate-email" type="email" placeholder="tuamail@esempio.com">
    <button onclick="saveGateEmail()">Inizia</button>
    <div class="err" id="gate-err"></div>
  </div>
</div>

<div id="settings-modal" style="display:none">
  <div class="box">
    <span class="close-x" onclick="closeSettings()">✕</span>
    <h2>⚙️ Impostazioni</h2>
    <div class="dim" style="font-size:12px">Canali per ricevere gli alert BUY/SELL</div>

    <label>Email</label>
    <input id="set-email" type="email" placeholder="tuamail@esempio.com">

    <label>Telegram — chat ID</label>
    <input id="set-telegram" type="text" placeholder="es. 123456789">
    <div class="row-btns">
      <button class="secondary" style="flex:1" onclick="detectTelegramChatId()">📡 Rileva automaticamente</button>
    </div>
    <div class="hint">Prima scrivi un messaggio qualsiasi al tuo bot su Telegram (es. "ciao"), poi premi "Rileva automaticamente" — trova da solo il tuo chat ID. Se il bot non è ancora configurato lato server, vedi il README per crearlo con @BotFather.</div>

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
</div>

<nav class="bottom">
  <button id="nav-scanner" class="active" onclick="showTab('scanner')">🔍 Scanner</button>
  <button id="nav-portfolio" onclick="showTab('portfolio')">💼 Portafoglio</button>
  <button id="nav-watchlist" onclick="showTab('watchlist')">🎯 Livelli</button>
  <button id="nav-opportunities" onclick="showTab('opportunities')">💡 Opportunità</button>
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

async function openSettings() {
  document.getElementById('settings-err').textContent = '';
  document.getElementById('settings-ok').textContent = '';
  document.getElementById('set-email').value = CURRENT_EMAIL || '';
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    document.getElementById('set-telegram').value = data.telegram_chat_id || '';
  } catch (e) { /* ignora, campo resta vuoto */ }
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
  if (!res.ok) { errEl.textContent = data.error || 'Errore'; return; }
  CURRENT_EMAIL = data.email;

  await fetch('/api/settings/telegram', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({chat_id: telegram}),
  });

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
  if (!res.ok) { errEl.textContent = data.error || 'Errore'; return; }
  document.getElementById('set-telegram').value = data.telegram_chat_id;
  okEl.textContent = `Trovato: ${data.name || data.telegram_chat_id} ✓ (premi Salva)`;
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

function showTab(name) {
  document.querySelectorAll('.tab-view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.bottom button').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('nav-' + name).classList.add('active');
  if (name === 'portfolio') { loadPortfolio(); loadVerdict(); }
  if (name === 'alerts') loadAlerts();
  if (name === 'history') loadHistory();
  if (name === 'opportunities') loadOpportunities();
  if (name === 'watchlist') { loadWatchlist(); loadWatchlistLog(); }
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
      <div class="metric"><div class="val">${a.score}</div><div class="lbl">Score</div></div>
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
        <span class="pill ${a.signal}">${a.signal}</span>
      </div>
      ${metrics}
      ${reasons}
      ${aiNote}
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
