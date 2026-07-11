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

# Chiave per firmare le sessioni Flask (login). In produzione impostala
# come variabile d'ambiente: una stringa persa invalida tutte le sessioni.
SECRET_KEY = os.environ.get("CECCHINO_SECRET_KEY", "dev-insecure-key-change-me")

# Credenziali OAuth "Sign in with Google" (Google Cloud Console > APIs &
# Services > Credentials > OAuth client ID > Applicazione web).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Segreto condiviso con il trigger esterno (es. GitHub Actions) che chiama
# /api/cron/tick per far girare il controllo orario anche se l'hosting
# gratuito mette l'app in sleep.
CRON_SECRET = os.environ.get("CECCHINO_CRON_SECRET", "")

# URL pubblico dell'app, usato solo nel testo delle mail di alert.
PUBLIC_URL = os.environ.get("CECCHINO_PUBLIC_URL", "http://localhost:5000")

DEFAULT_TICKERS = ["MU", "ASML", "MSFT", "SNDK", "TSMC"]

DEFAULT_LEVELS = {
    "MU":   {"buy": 840,  "sell": 1100, "qty": 0, "paid": 0},
    "ASML": {"buy": 1400, "sell": 1800, "qty": 2, "paid": 2646},
    "MSFT": {"buy": 350,  "sell": 450,  "qty": 1, "paid": 6000},
    "SNDK": {"buy": 1500, "sell": 2500, "qty": 0, "paid": 0},
    "TSMC": {"buy": 340,  "sell": 450,  "qty": 0, "paid": 0},
}
