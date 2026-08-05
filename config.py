"""
Configurazione di Cecchino Pro.

Le credenziali email NON vengono scritte qui in chiaro: vengono lette da
variabili d'ambiente (o da un file .env locale, non versionato in git).
Copia .env.example in .env e inserisci i tuoi valori reali sul Raspberry Pi.
"""
import os

# Carica automaticamente un file .env se presente (senza richiedere
# python-dotenv obbligatorio: se non è installato, si ignora silenziosamente
# e ci si affida alle variabili d'ambiente già esportate nella shell).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

EMAIL_FROM = os.environ.get("CECCHINO_EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("CECCHINO_EMAIL_PASSWORD", "")

# Segreto condiviso con il trigger esterno (es. GitHub Actions) che chiama
# /api/cron/tick per far girare il controllo orario anche se l'hosting
# gratuito mette l'app in sleep.
CRON_SECRET = os.environ.get("CECCHINO_CRON_SECRET", "")

# URL pubblico dell'app, usato solo nel testo delle mail di alert.
PUBLIC_URL = os.environ.get("CECCHINO_PUBLIC_URL", "http://localhost:5000")

# Terzo fallback per i dati di mercato (opzionale). Se Yahoo e Stooq sono
# entrambi irraggiungibili dall'hosting (capita su alcuni IP cloud), l'app
# usa questa API gratuita: https://twelvedata.com (free tier, no carta,
# 800 richieste/giorno). Lasciala vuota per non usarla.
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

# Commento AI opzionale sopra ogni segnale (facoltativo). Usa la API
# gratuita di Google Gemini (https://aistudio.google.com/apikey — free
# tier senza carta di credito). Lasciala vuota per non usarla: l'app
# funziona comunque, il commento AI è solo un testo in più.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Bot Telegram opzionale per ricevere i segnali lì invece (o oltre) che via
# mail: crealo con @BotFather (vedi README) e incolla qui il token. Il
# destinatario (chat_id) si imposta dalla UI dell'app, non da qui.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# NB: il ticker Yahoo/NYSE di Taiwan Semiconductor è "TSM", non "TSMC"
# ("TSMC" non è un simbolo valido su nessun mercato e fallirebbe sempre).
DEFAULT_TICKERS = ["MU", "ASML", "MSFT", "SNDK", "TSM"]

# Nessuna soglia di acquisto/vendita precompilata: il segnale BUY/HOLD/SELL è
# già calcolato in automatico dall'analisi tecnica (RSI, medie mobili,
# massimi/minimi 52 settimane, volumi). Le soglie personali restano
# disponibili nella UI solo come avviso extra facoltativo.
DEFAULT_LEVELS = {
    "MU":   {"buy": None, "sell": None, "qty": 0, "paid": 0},
    "ASML": {"buy": None, "sell": None, "qty": 2, "paid": 2646},
    "MSFT": {"buy": None, "sell": None, "qty": 1, "paid": 6000},
    "SNDK": {"buy": None, "sell": None, "qty": 0, "paid": 0},
    "TSM":  {"buy": None, "sell": None, "qty": 0, "paid": 0},
}

# --------------------------------------------------------------------------
# Watchlist con soglie ingresso/stop/target (tab "🎯 Livelli").
# --------------------------------------------------------------------------
# Tutti i valori sono in EUR (l'app converte i prezzi live in EUR con il
# tasso EURUSD=X per confrontarli, esattamente come nell'import da foto).
# entry_low/entry_high = None significa "nessun limite da quel lato"
#   (es. META ha solo entry_high=542 -> "ingresso sotto 542").
# stop_pct / target_multiple: usati SOLO se manca il valore assoluto
#   corrispondente, risolti in € concreti al primo avvio usando come
#   riferimento entry_high (o il prezzo di mercato per entry_at_market=True).
# entry_at_market=True: "ingresso al prezzo corrente" (es. NOK) — niente
#   alert di ingresso (si considera già in posizione), si monitorano solo
#   stop e target da quel momento.
WATCH_LEVELS = [
    {"ticker": "AVGO", "name": "Broadcom", "entry_low": 354, "entry_high": 363,
     "stop_price": 313, "target_low": 460, "target_high": 460},
    {"ticker": "META", "name": "Meta Platforms", "entry_low": None, "entry_high": 542,
     "stop_price": 478, "target_low": 690, "target_high": 690},
    {"ticker": "SOI.PA", "name": "Soitec", "entry_low": None, "entry_high": 147,
     "stop_pct": -0.15, "target_multiple": 2.0},
    {"ticker": "AAOI", "name": "Applied Optoelectronics", "entry_low": 87, "entry_high": 97,
     "stop_price": 74, "target_low": 184, "target_high": 184},
    {"ticker": "NOK", "name": "Nokia", "entry_at_market": True,
     "stop_pct": -0.15, "target_low": 7.30, "target_high": 9.20},
    {"ticker": "SNDK", "name": "SanDisk", "entry_low": 1020, "entry_high": 1080,
     "stop_price": 809, "target_low": 1288, "target_high": 1288},
    {"ticker": "CVX", "name": "Chevron", "entry_low": 170, "entry_high": 175,
     "stop_price": 152, "target_low": 202, "target_high": 202},
    {"ticker": "V", "name": "Visa", "entry_low": 336, "entry_high": 345,
     "stop_price": 294, "target_low": 395, "target_high": 395},
]
