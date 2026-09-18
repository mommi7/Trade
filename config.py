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

# --------------------------------------------------------------------------
# Bottleneck Filter — screener a due motori (tab "🎯 Bottleneck")
# --------------------------------------------------------------------------
# Universo scansionabile. Nessuna API a pagamento espone gratis l'elenco
# completo e aggiornato di ogni titolo quotato su NYSE+Nasdaq+Borsa
# Italiana+Xetra+Euronext (sono decine di migliaia di simboli): una lista
# così va comprata da un vendor dati. Questa è una lista curata, ampia e
# multi-borsa che copre i principali nomi liquidi di ciascun mercato — va
# vista come punto di partenza estendibile a mano aggiungendo ticker qui
# sotto, non come "tutta la borsa". Formato simboli Yahoo: suffisso .MI
# (Borsa Italiana), .DE (Xetra), .PA (Euronext Parigi), .AS (Euronext
# Amsterdam), nessun suffisso per NYSE/Nasdaq.
BOTTLENECK_UNIVERSE = [
    # --- NYSE / Nasdaq: tech, semiconduttori, cloud ---
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "AVGO", "ORCL", "CRM",
    "ADBE", "AMD", "QCOM", "TXN", "INTC", "MU", "ASML", "TSM", "NOW", "INTU",
    "PANW", "FTNT", "CRWD", "ZS", "SNPS", "CDNS", "ANET", "LRCX", "KLAC",
    "AMAT", "ON", "MRVL", "WDC", "STX", "SNDK", "DELL", "HPQ", "CSCO", "IBM",
    "UBER", "ABNB", "SHOP", "NET", "DDOG", "SNOW", "MDB", "TEAM", "WDAY",
    "PLTR", "RBLX", "SPOT", "PYPL", "SQ",
    # --- NYSE / Nasdaq: e-commerce, media, consumo ---
    "JD", "BABA", "PDD", "MELI", "NFLX", "DIS", "SBUX", "MCD", "NKE", "TGT",
    "COST", "WMT", "HD", "LOW", "TJX",
    # --- NYSE / Nasdaq: healthcare / difensivo ---
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "UNH", "ZTS", "VRTX", "REGN", "GILD",
    "MRNA", "AMGN", "BMY", "PG", "KO", "PEP", "CL", "MDLZ",
    # --- NYSE / Nasdaq: industriali, difesa, materiali ---
    "LMT", "RTX", "NOC", "GD", "BA", "CAT", "DE", "HON", "GE", "MMM", "XYL",
    "MP", "SOLS", "FCX", "NEM",
    # --- NYSE / Nasdaq: energia ---
    "XOM", "CVX", "COP", "SLB", "OXY", "EOG",
    # --- NYSE / Nasdaq: finanza ---
    "V", "MA", "JPM", "BAC", "GS", "MS", "AXP", "BRK-B", "SPGI", "BLK",
    # --- Asia (ADR/quotate Nasdaq/NYSE) ---
    "000660.KS",
    # --- Borsa Italiana ---
    "ENI.MI", "ENEL.MI", "ISP.MI", "UCG.MI", "STLAM.MI", "RACE.MI",
    "STMMI.MI", "PRY.MI", "TIT.MI", "G.MI", "MONC.MI", "REC.MI", "CPR.MI",
    "AMP.MI", "LDO.MI",
    # --- Xetra (Germania) ---
    "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "AIR.DE", "BAS.DE", "BAYN.DE",
    "BMW.DE", "MBG.DE", "VOW3.DE", "MRK.DE", "MUV2.DE", "DHL.DE", "IFX.DE",
    "ADS.DE",
    # --- Euronext Parigi ---
    "MC.PA", "OR.PA", "SAN.PA", "TTE.PA", "AI.PA", "SU.PA", "SAF.PA",
    "AIR.PA", "DG.PA", "BNP.PA", "CS.PA", "ORA.PA", "STLAP.PA", "RMS.PA",
    "EL.PA",
    # --- Euronext Amsterdam ---
    "ASML.AS", "ADYEN.AS", "HEIA.AS", "PHIA.AS", "AD.AS", "WKL.AS", "INGA.AS",
    "RAND.AS", "AKZA.AS", "DSFIR.AS",
]

BOTTLENECK_DEFAULTS = {
    # Motore A — filtri quantitativi (regole 1-8 del prompt)
    "engine_a": {
        "drawdown_min_pct": 20.0,      # regola 1: almeno -20% dal massimo 52w
        "return_3y_max_pct": 200.0,    # regola 2: tetto rendimento 3y (evita titoli già troppo saliti)
        "pe_max": 35.0,                # regola 3
        "dislocation_min": 1.5,        # regola 4: calo prezzo >= 1.5x il calo peggiore ricavi/EBITDA
        "net_debt_ebitda_max": 3.0,    # regola 6
        "min_analyst_coverage": 3,     # regola 7
        "catalyst_window_days": 90,    # regola 8: 3 mesi
    },
    # Motore B — Bottleneck Filter personale (0-10 per sotto-punteggio)
    "engine_b": {
        "buy_score_min": 35,           # somma >= 35
        "hype_max": 5,                 # hype <= 5
        "growth_min_pct": 30.0,        # crescita ricavi YoY >= 30%
        "watch_score_min": 25,         # somma 25-34 = ATTENDI
    },
    # Livello 3 — vincoli di portafoglio (mai dentro i due motori)
    "portfolio": {
        "max_pct_per_stock": 15.0,
        "max_pct_per_sector": 40.0,
        "min_pct_defensive": 10.0,
    },
}

# Settori "difensivi" per il vincolo minimo di portafoglio (regola 3, livello 3).
DEFENSIVE_SECTORS = {"Difensivo", "Healthcare", "Salute", "Beni di consumo primari", "Utility"}

# Cache locale delle fondamentali (24h) per non saturare Yahoo durante una
# scansione dell'intero universo.
BOTTLENECK_CACHE_TTL_SECONDS = 24 * 3600

# --------------------------------------------------------------------------
# Verifica notizie senza AI (usata al posto di Gemini quando GEMINI_API_KEY
# non è impostata — vedi check_recent_event in app.py). Confronto letterale,
# case-insensitive, sul titolo della notizia: se una di queste frasi compare,
# il titolo posseduto passa da HOLD a SELL (regola 3 dello screener
# settimanale). È deterministico e gratuito, ma più grezzo di un'AI che
# legge il contesto — può generare falsi positivi (una frase che cita
# "lawsuit" senza riguardare l'azienda) o falsi negativi (un evento reale
# descritto con parole diverse da queste). Modifica pure questa lista.
NEWS_BREAK_KEYWORDS = [
    "guidance cut", "cuts guidance", "guidance tagliata", "lowers guidance",
    "misses estimates", "missed estimates", "earnings miss", "profit warning",
    "downgrade", "downgraded", "declassato", "declassata",
    "lawsuit", "causa legale", "class action", "investigation", "indagine",
    "sec probe", "sec inquiry", "fraud", "frode",
    "resigns", "resignation", "dimissioni", "steps down",
    "recall", "richiamo", "data breach", "cyberattack",
    "bankruptcy", "fallimento", "files for chapter 11", "delisting",
    "slashes forecast", "cuts forecast",
]
