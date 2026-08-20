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

# --------------------------------------------------------------------------
# Screener settimanale a 25 titoli con regole operative (tab "📅 Settimanale")
# --------------------------------------------------------------------------
# category: "owned" (già in portafoglio) / "watchlist" (da valutare) /
#           "excluded" (scartati: mai suggeriti anche se lo screener
#           generico li troverebbe altrimenti validi).
# owner (solo per "owned"): "mohamed" / "micaela" / "shared" — usato per il
# controllo di concentrazione per settore, calcolato separatamente per i
# due portafogli. entry_low/entry_high sono in USD (valuta nativa dei
# titoli), non convertiti in €.
#
# fcf_negative / catalyst_date / role / note / exclusion_reason: dati che
# l'utente ha già verificato a mano. yfinance/Yahoo non espone FCF, date
# earnings o target di consenso analisti in modo affidabile e gratuito,
# quindi qui sono flag statici da aggiornare tu quando cambiano, non dati
# "live" — sarebbe disonesto far finta di poterli scaricare gratis in modo
# solido.
SCREENER_UNIVERSE = [
    # --- Già in portafoglio ---
    {"ticker": "ASML", "name": "ASML Holding", "category": "owned", "owner": "shared",
     "sector": "Semiconduttori/Memoria", "role": "Core - monopolio EUV"},
    {"ticker": "MSFT", "name": "Microsoft", "category": "owned", "owner": "shared",
     "sector": "Cloud/Software", "role": "Core - cloud Azure"},
    {"ticker": "GOOGL", "name": "Alphabet", "category": "owned", "owner": "shared",
     "sector": "Cloud/Software", "role": "Core - search/AI"},
    {"ticker": "JNJ", "name": "Johnson & Johnson", "category": "owned", "owner": "shared",
     "sector": "Difensivo", "role": "DIFENSIVO - non vendere mai", "never_sell": True},
    {"ticker": "000660.KS", "name": "SK Hynix", "category": "owned", "owner": "mohamed",
     "sector": "Semiconduttori/Memoria", "role": "Core - memoria, PE più basso"},
    {"ticker": "INTU", "name": "Intuit", "category": "owned", "owner": "micaela",
     "sector": "Cloud/Software", "role": "Core - software fiscale, upside 76%"},
    {"ticker": "JD", "name": "JD.com", "category": "owned", "owner": "micaela",
     "sector": "E-commerce Asia", "role": "Core - e-commerce Cina, Burry top-3"},

    # --- Watchlist: non ancora comprati ---
    {"ticker": "AVGO", "name": "Broadcom", "category": "watchlist", "sector": "Semiconduttori/Memoria",
     "entry_note": "Su ritracciamento dopo earnings 2 set", "note": "AI networking +200% guidance",
     "catalyst_date": "2026-09-02"},
    {"ticker": "ORCL", "name": "Oracle", "category": "watchlist", "sector": "Cloud/Software",
     "entry_high": 136, "note": "RPO $638mld ma FCF negativo -23.7mld", "fcf_negative": True},
    {"ticker": "NOW", "name": "ServiceNow", "category": "watchlist", "sector": "Cloud/Software",
     "entry_low": 107, "entry_high": 115, "note": "Sconto -37/45%, numeri reali +24.5%"},
    {"ticker": "SOLS", "name": "Solstice Advanced Materials", "category": "watchlist", "sector": "Materiali critici",
     "entry_high": 55, "note": "Monopolio nucleare USA, rischio deal Element Solutions"},
    {"ticker": "MP", "name": "MP Materials", "category": "watchlist", "sector": "Materiali critici",
     "entry_note": "Su ritracciamento", "note": "Unico USA terre rare, FCF negativo fino 2028",
     "fcf_negative": True},
    {"ticker": "XYL", "name": "Xylem", "category": "watchlist", "sector": "Materiali critici",
     "entry_note": "Prezzo corrente", "note": "Acqua/data center cooling, compounder lento"},
    {"ticker": "FTNT", "name": "Fortinet", "category": "watchlist", "sector": "Cloud/Software",
     "entry_note": "~$95 già ragionevole", "note": "Cybersecurity, il più a sconto del settore"},
    {"ticker": "CRWD", "name": "CrowdStrike", "category": "watchlist", "sector": "Cloud/Software",
     "entry_note": "Solo su correzione forte", "note": "Già premium anche dopo calo"},
    {"ticker": "ZS", "name": "Zscaler", "category": "watchlist", "sector": "Cloud/Software",
     "entry_note": "Dopo -50% dal picco 2025", "note": "Miglior punto ingresso storico"},
    {"ticker": "ZTS", "name": "Zoetis", "category": "watchlist", "sector": "Difensivo",
     "entry_note": "Prezzo corrente", "note": "-30% a maggio, Burry ha comprato"},
    {"ticker": "VRTX", "name": "Vertex Pharmaceuticals", "category": "watchlist", "sector": "Difensivo",
     "entry_note": "Su pullback", "note": "Monopolio fibrosi cistica"},
    {"ticker": "LMT", "name": "Lockheed Martin", "category": "watchlist", "sector": "Difesa",
     "entry_note": "Prezzo corrente", "note": "Backlog $194mld, PE 17x, il più economico difesa"},
    {"ticker": "RTX", "name": "RTX Corporation", "category": "watchlist", "sector": "Difesa",
     "entry_note": "Prezzo corrente", "note": "Backlog $271mld, diversificato"},
    {"ticker": "MU", "name": "Micron", "category": "watchlist", "sector": "Semiconduttori/Memoria",
     "entry_note": "NON RITRADARE - solo se tesi confermata", "note": "Storia di 8 round-trip falliti, Burry short",
     "no_retrade": True},
    {"ticker": "BABA", "name": "Alibaba", "category": "watchlist", "sector": "E-commerce Asia",
     "entry_note": "Dopo earnings fine agosto", "note": "FCF negativo, Burry però è long",
     "fcf_negative": True, "catalyst_date": "2026-08-29"},

    # --- Da evitare: mai suggeriti, anche se lo screener li troverebbe validi ---
    {"ticker": "SPCX", "name": "SpaceX", "category": "excluded",
     "exclusion_reason": "Lock-up in corso fino dicembre 2026, pressione offerta "
                          "(NB: verifica il ticker, SpaceX non è quotata pubblicamente)"},
    {"ticker": "GPRO", "name": "GoPro", "category": "excluded",
     "exclusion_reason": "Going concern doubt, patrimonio netto negativo"},
    {"ticker": "RKLB", "name": "Rocket Lab", "category": "excluded",
     "exclusion_reason": "PE negativo, EBITDA -175mln, beta 3.30"},
    {"ticker": "SMCI", "name": "Super Micro", "category": "excluded",
     "exclusion_reason": "Diluizione $7mld annunciata, indagine DOJ"},
    {"ticker": "MRNA", "name": "Moderna", "category": "excluded",
     "exclusion_reason": "FCF negativo, non comprare su balzi verticali +80%+ in un giorno"},
    {"ticker": "PLTR", "name": "Palantir", "category": "excluded",
     "exclusion_reason": "Troppo caro (90-150x), ADX collassato"},
    {"ticker": "NVO", "name": "Novo Nordisk", "category": "excluded",
     "exclusion_reason": "Trial falliti 2 volte in 8 mesi, guidance tagliata ripetutamente"},
]

SECTOR_CONCENTRATION_LIMIT_PCT = 40.0  # regola 4
MAX_BUY_PER_WEEK = 2                   # regola 7
POST_JUMP_THRESHOLD_PCT = 15.0         # regola 1: +15% in una seduta = non inseguire
CATALYST_WINDOW_DAYS = 42              # regola 6: 6 settimane
