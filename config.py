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
