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
EMAIL_TO = os.environ.get("CECCHINO_EMAIL_TO", EMAIL_FROM)
EMAIL_PASSWORD = os.environ.get("CECCHINO_EMAIL_PASSWORD", "")

DEFAULT_TICKERS = ["MU", "ASML", "MSFT", "SNDK", "TSMC"]

DEFAULT_LEVELS = {
    "MU":   {"buy": 840,  "sell": 1100, "qty": 0, "paid": 0},
    "ASML": {"buy": 1400, "sell": 1800, "qty": 2, "paid": 2646},
    "MSFT": {"buy": 350,  "sell": 450,  "qty": 1, "paid": 6000},
    "SNDK": {"buy": 1500, "sell": 2500, "qty": 0, "paid": 0},
    "TSMC": {"buy": 340,  "sell": 450,  "qty": 0, "paid": 0},
}
