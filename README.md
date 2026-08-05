# Cecchino Pro

Sistema di trading signal monitoring. Alla prima apertura chiede solo
**l'email a cui mandare gli alert** (niente login, niente account Google da
configurare) — poi Scanner, Portafoglio, Alert e Storico. Calcola segnali
basati su RSI, medie mobili e distanza dai massimi/minimi a 52 settimane.

Puoi farlo girare in due modi:

1. **Sul tuo Raspberry Pi** — davvero 24/7, gratis, senza limiti (vedi sotto).
2. **Su un hosting cloud gratuito (Render) + GitHub Actions** — comodo per
   testarlo velocemente da un link pubblico senza avere hardware acceso.
   Ha un limite importante: leggi la sezione "Limiti del free tier" prima
   di fidartene per soldi veri.

## 1. Configura il mittente delle mail (obbligatorio in entrambi i casi)

Genera una **password per le app** Gmail su
`https://myaccount.google.com/apppasswords` (richiede la verifica in due
passaggi attiva). Questo è l'account che **invia** le mail — il
**destinatario** è l'email che inserisci direttamente nell'app al primo
avvio (si può cambiare in qualsiasi momento dalle Impostazioni ⚙️ in alto
a destra).

## 1b. Bot Telegram — notifiche push E comandi interattivi

Il sito resta il "cervello" che gira in background (calcola i segnali,
tiene lo stop loss, aggiorna il portafoglio ogni ora): il bot Telegram è
un secondo modo di parlargli, più comodo della mail e senza dover aprire
il sito ogni volta.

**Cosa puoi fare dalla chat Telegram, senza mai toccare il sito:**
- **Mandare una foto** del portafoglio (screenshot del broker) → il bot la
  legge, importa le posizioni e ti risponde subito con il verdetto AI.
- `/portafoglio` → riepilogo posizioni attuali con segnale e P&L.
- `/verdetto` → rigenera al volo il verdetto AI su tutto il portafoglio.
- `/aiuto` → elenco comandi.
- E ricevi comunque, in automatico, gli alert push (cambio segnale, stop
  loss, opportunità, verdetto giornaliero) — quelli arrivano da soli,
  senza che tu scriva nulla.

**Configurazione, tutta dentro Telegram tranne un passaggio:**

1. **Crea il bot** (2 minuti): apri Telegram, cerca **@BotFather**,
   mandagli `/newbot`, dagli un nome e uno username che finisca in `bot`
   (es. `CecchinoProBot`). Ti risponde con un **token** tipo
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — copialo.
2. **Unico passaggio fuori da Telegram**: metti quel token nella variabile
   d'ambiente `TELEGRAM_BOT_TOKEN` (Render → Environment, oppure `.env` in
   locale — nome esatto, tutto maiuscolo) e aspetta il redeploy.
3. **Da qui in poi, solo Telegram**: cerca il tuo bot per lo username che
   gli hai dato e mandagli un messaggio qualsiasi (es. "ciao"). Il primo
   messaggio che riceve si registra da solo come proprietario — nessun
   altro può usarlo dopo, il bot ignora silenziosamente chiunque non sia
   te. Ti risponde "✅ Configurato!" e da lì puoi mandare foto o comandi.

Puoi usare mail e Telegram insieme: ogni alert automatico va su entrambi
i canali configurati.

**Un limite onesto sulla reattività**: sull'hosting cloud gratuito
(Render) il bot risponde ai tuoi messaggi solo mentre il processo è
sveglio — il workflow GitHub Actions incluso lo risveglia ogni 10 minuti
(vedi sotto), quindi nella pratica è quasi sempre reattivo ma non è un
always-on garantito al 100%. Se vuoi zero compromessi, il Raspberry Pi
(Opzione A) tiene il bot sempre sveglio per davvero.

## 2. Opzione A — Raspberry Pi (always-on reale, gratis)

```bash
cd ~/cecchino
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # inserisci CECCHINO_EMAIL_FROM, CECCHINO_EMAIL_PASSWORD, CECCHINO_PUBLIC_URL
```

```bash
python app.py
```

Apri da telefono/PC sulla stessa rete: `http://<IP-DEL-RASPBERRY>:5000`,
inserisci la tua email nella schermata iniziale e sei pronto.

### Avvio automatico all'accensione (systemd)

Crea `/etc/systemd/system/cecchino.service`:

```ini
[Unit]
Description=Cecchino Pro Trading Monitor
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/cecchino
EnvironmentFile=/home/pi/cecchino/.env
ExecStart=/home/pi/cecchino/venv/bin/python app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable cecchino
sudo systemctl start cecchino
sudo journalctl -u cecchino -f   # log in tempo reale
```

Su Raspberry Pi il thread interno gira ogni ora davvero in continuo: non ti
serve nient'altro.

## 3. Opzione B — Deploy gratuito su Render + GitHub Actions

Render offre un piano gratuito che si collega direttamente a un repo
GitHub e fa auto-deploy ad ogni push. Non serve carta di credito.

### 3a. Deploy su Render

1. Pusha questo repo su GitHub (se non l'hai già fatto).
2. Vai su https://render.com → crea un account (puoi accedere con GitHub).
3. **New +** → **Blueprint** → seleziona questo repository. Render legge
   automaticamente `render.yaml` e crea il servizio web `cecchino-pro`
   sul piano free.
4. Dopo il primo deploy, apri il servizio → **Environment** e compila i
   valori mancanti (`sync: false` in `render.yaml`):
   - `CECCHINO_EMAIL_FROM`, `CECCHINO_EMAIL_PASSWORD`
   - `CECCHINO_PUBLIC_URL` → l'URL che Render ti ha assegnato, es.
     `https://cecchino-pro.onrender.com`
   - `CECCHINO_CRON_SECRET` è già generato automaticamente da Render
     (`generateValue: true`); puoi lasciarlo.
5. Render fa un redeploy automatico dopo il cambio env vars. Apri l'URL:
   dovresti vedere la richiesta della tua email.

### 3b. Tenerlo sveglio (e reattivo su Telegram) gratis, con GitHub Actions

Il piano gratuito Render mette il servizio in **sleep dopo ~15 minuti di
inattività** (si risveglia alla richiesta successiva, con qualche secondo di
attesa). Per far comunque scattare il controllo dei segnali *e* mantenere
il bot Telegram reattivo è incluso un workflow GitHub Actions che chiama
un endpoint dedicato e risveglia l'app da solo:

`.github/workflows/cecchino-tick.yml` gira **ogni 10 minuti**
(`cron: "*/10 * * * *"`, gratuito e illimitato sui repo pubblici GitHub) e
chiama `POST /api/cron/tick` sull'app — tenerlo più frequente di prima
(era ogni ora) serve soprattutto a far rispondere il bot Telegram quasi
sempre, dato che il thread che lo ascolta gira solo mentre il processo è
sveglio.

Configuralo così:

1. Nel repo GitHub → **Settings** → **Secrets and variables** → **Actions**
   → **New repository secret**, aggiungi:
   - `CECCHINO_URL` → es. `https://cecchino-pro.onrender.com`
   - `CECCHINO_CRON_SECRET` → lo stesso valore che Render ha generato per
     `CECCHINO_CRON_SECRET` (Render → Environment, copialo da lì)
2. Il workflow parte da solo ogni 10 minuti. Puoi anche lanciarlo a mano da
   **Actions** → **Cecchino Pro - tick orario** → **Run workflow**.

### Limiti del free tier (leggi prima di fidarti)

- **Filesystem effimero**: Render (piano free) non garantisce la
  persistenza del disco tra un riavvio/redeploy e l'altro. Il database
  SQLite (`signals.db`) può azzerarsi, perdendo email impostata,
  portafoglio e storico. Va benissimo per **testare** che tutto funzioni;
  per un uso reale valuta un database gestito gratuito (es. Render
  Postgres free per 90 giorni, o Supabase Postgres free) al posto di
  SQLite.
- **Sleep**: senza il tick di GitHub Actions, il monitor interno gira solo
  mentre il servizio è sveglio.
- Per un always-on **vero e senza compromessi**, il Raspberry Pi (Opzione
  A) resta la soluzione più solida: è hardware tuo, gira 24/7 senza limiti
  di piattaforma.

## Funzionalità

- **Email e/o Telegram per gli alert**: alla prima apertura l'app chiede
  solo l'indirizzo a cui mandare i segnali BUY/SELL. Telegram si aggiunge
  dalle Impostazioni ⚙️ in alto a destra (vedi sezione 1b) — più semplice
  e istantaneo della mail, i due canali funzionano insieme se li
  configuri entrambi.
- **Importa portafoglio da foto 📷**: nel tab Portafoglio, "Importa da
  foto" — scatta o carica uno screenshot del tuo broker (es. Trade
  Republic) e Gemini Vision legge titolo, valore e guadagno/perdita di
  ogni posizione, aggiungendola in automatico. La quantità di azioni non è
  quasi mai leggibile dallo screenshot: viene **stimata** dividendo il
  valore della posizione per il prezzo di mercato attuale (convertito in
  €) — un'approssimazione dichiarata, non un dato letto pixel per pixel.
  Controlla sempre il risultato dopo l'import. Richiede `GEMINI_API_KEY`.
- **Verdetto giornaliero AI**: nel tab Portafoglio, una card genera (una
  volta al giorno in automatico, o su richiesta col pulsante 🔄) un
  giudizio COMPRA/AUMENTA/TIENI/RIDUCI/VENDI per ogni posizione, basato
  sui segnali tecnici che l'app già calcola (RSI, medie, 52 settimane,
  volumi, peso, concentrazione per settore). **Attenzione**: Gemini qui
  non fa ricerche web in tempo reale — non sa di una notizia uscita ieri
  sera a meno che non sia già riflessa nel prezzo. È un'analisi
  tecnica+AI, comoda per un check-up quotidiano veloce, non sostituisce
  una verifica manuale con dati e notizie verificati prima di operare
  cifre importanti. Richiede `GEMINI_API_KEY`.
- **Scanner**: analisi on-demand di qualsiasi ticker (prezzo, RSI 14, MA50,
  MA200, distanza da massimo/minimo 52 settimane, segnale e motivazioni in
  italiano).
- **Portafoglio**: titoli tracciati (precompilato con MU, ASML, MSFT,
  SNDK, TSM) con quantità, prezzo pagato, P&L in € e %, segnale attuale,
  soglie personali di acquisto/vendita. Aggiornamento automatico ogni ora
  (thread locale e/o tick esterno) o manuale dal pulsante "Aggiorna
  prezzi". Puoi aggiungere/rimuovere qualsiasi titolo, incluse le
  **crypto** (formato Yahoo: `BTC-USD`, `ETH-USD`, `SOL-USD`, ecc.).
- **Stop loss dinamico (trailing stop)**: per ogni titolo che possiedi
  (quantità > 0) l'app calcola in automatico uno stop-loss al 10% sotto il
  massimo storico raggiunto da quando lo possiedi, mostrato nella card
  come "Stop loss 🛡️". Quando il titolo sale e lo stop si alza di almeno
  il 3% arriva una mail per farti sapere che il guadagno è più protetto;
  se il prezzo rompe lo stop arriva una mail di avviso a vendere/valutare
  la posizione. Nessuna configurazione richiesta, gira da solo col resto
  del monitoraggio orario.
- **Opportunità (screener di mercato)**: una scansione automatica, ogni
  ora, di circa 30 titoli "bottleneck" (monopoli/quasi-monopoli
  tecnologici: NVDA, ORCL, AVGO, ASML, TSM, ...) **non ancora nel tuo
  portafoglio**. Chi ha un segnale BUY forte (score ≥ 40) compare nel tab
  "Opportunità" con lo stesso dettaglio delle altre card, pronto per
  essere aggiunto al portafoglio con un click. Una mail digest parte al
  massimo una volta al giorno per non spammarti.
- **Livelli 🎯 (watchlist ingresso/stop/target)**: soglie di trading
  definite a mano — zona di ingresso (es. 354–363), stop loss e target,
  in € (assoluti o come % / moltiplicatore risolto automaticamente al
  primo avvio, es. "-15%" o "raddoppio"). Ogni ora l'app controlla il
  prezzo live di ognuno e manda un alert quando entra in zona ingresso
  (🟢), rompe lo stop (🔴) o raggiunge il target (🎯) — su mail e/o
  Telegram, ognuno una volta sola. La lista di default è configurabile in
  `config.WATCH_LEVELS` in `config.py`. I prezzi live (spesso in USD)
  vengono convertiti in € con lo stesso tasso EURUSD live usato
  dall'import da foto, per confrontarli correttamente con le soglie.
- **Ricerca ticker con suggerimenti**: scrivendo un simbolo o un nome
  (es. "micro", "bitcoin") negli input di Scanner, Portafoglio e Alert
  compare un menu a tendina con i titoli corrispondenti, da selezionare
  con un click.
- **Alert**: soglie di prezzo (sopra/sotto) per qualsiasi ticker; quando
  scattano inviano una mail e vengono segnate come "scattato".
- **Storico**: ogni cambio di segnale (es. BUY → SELL) su un titolo
  tracciato viene registrato con data, prezzo e motivazione, con
  statistiche riassuntive dei cambi.
- **Commento AI (opzionale)**: se imposti `GEMINI_API_KEY` (gratis, vedi
  sotto), ogni analisi include un breve commento in linguaggio naturale
  generato da Google Gemini sopra ai dati tecnici.

## Se "Impossibile recuperare dati" persiste su hosting cloud

L'app prova **tre** fonti dati in sequenza: Yahoo Finance → Stooq → Twelve
Data. Le prime due non richiedono configurazione, ma alcuni hosting cloud
gratuiti (Render incluso) condividono pool di IP che sia Yahoo che Stooq
a volte bloccano o limitano. Se dopo l'ultimo deploy vedi ancora l'errore
su **tutti** i titoli, attiva la terza fonte:

1. Registrati gratis (2 minuti, nessuna carta) su https://twelvedata.com
   e copia la API key dalla dashboard.
2. Su Render → il tuo servizio (o l'Environment Group collegato) →
   **Environment** → aggiungi una variabile chiamata **esattamente**
   `TWELVEDATA_API_KEY` (tutto maiuscolo, con l'underscore) col valore
   copiato → salva. **Il nome deve combaciare alla lettera**: una
   variabile chiamata ad es. `twelvedata` o `Twelvedata_Api_Key` non
   viene letta dal codice e la chiave resta di fatto disattivata, anche
   se il valore è corretto. In locale/Raspberry Pi mettila nel file
   `.env` con lo stesso nome esatto.
3. Ricarica il sito: ora, se Yahoo e Stooq falliscono, l'app usa Twelve
   Data automaticamente. Il piano free copre 800 richieste/giorno, ampio
   per un portafoglio di pochi titoli aggiornato ogni ora.

Se vuoi capire *perché* falliscono Yahoo/Stooq invece di limitarti ad
aggirarlo, guarda i log del servizio (Render → tab **Logs**): ogni
fallimento ora stampa lo status HTTP esatto restituito (es. `Yahoo
query1 HTTP 429` = blocco temporaneo per troppe richieste dall'IP
condiviso).

## Collegare un'AI gratuita (commento discorsivo sopra i segnali)

- **Un abbonamento ChatGPT Plus/Pro non serve e non si collega a
  quest'app**: è un prodotto di chat per uso personale, non un'API — non
  fornisce un modo per far girare chiamate automatiche da un server. Per
  collegare un'AI a un'app serve sempre una **API key** a parte.
- **Google Gemini ha un'API gratuita vera**, senza carta di credito:
  1. Vai su https://aistudio.google.com/apikey, accedi con un account
     Google, clicca "Create API key" e copiala.
  2. Su Render → Environment → aggiungi una variabile chiamata
     **esattamente** `GEMINI_API_KEY` (stesso discorso di sopra sul nome
     preciso) col valore copiato → salva. In locale/Raspberry Pi:
     stesso nome nel file `.env`.
  3. Ricarica il sito: ora ogni analisi (Scanner e Portafoglio) include
     in fondo alla card un riquadro "🤖 AI" con 2-3 frasi generate che
     spiegano il segnale in linguaggio naturale, oltre ai motivi tecnici
     già elencati sopra.
  4. Se non imposti questa chiave l'app funziona lo stesso, identica a
     prima: il commento AI è solo un extra facoltativo, non governa il
     segnale BUY/HOLD/SELL (quello resta calcolato da RSI/medie
     mobili/52 settimane/volumi, sempre attivo).
- Le soglie di acquisto/vendita **sono già automatiche** e non richiedono
  di inserire prezzi a mano: i campi nel form Portafoglio sono solo un
  avviso extra facoltativo a un prezzo preciso che scegli tu.

## Note tecniche

- I dati di mercato vengono presi dall'endpoint pubblico `chart` di Yahoo
  Finance via `requests` (nessuna libreria `yfinance`), con fallback
  automatico tra `query1`/`query2.finance.yahoo.com`, poi Stooq, poi
  Twelve Data (se configurata) — vedi sezione sopra.
- Il segnale usa anche i volumi: uno spike di volume (>1.8x la media a 20
  giorni) che conferma la direzione del prezzo del giorno rafforza o
  indebolisce lo score. Nella card di analisi vedi anche le "zone"
  acquisto/vendita 🤖, calcolate automaticamente dal range a 52 settimane.
- Il commento AI, l'import da foto e il verdetto giornaliero (tutti Google
  Gemini, opzionali) sono puramente aggiuntivi: se `GEMINI_API_KEY` non è
  impostata non fanno nessuna chiamata di rete e l'app si comporta
  esattamente come senza queste funzioni.
- Telegram (`TELEGRAM_BOT_TOKEN` + chat ID) è sia canale push (`broadcast()`
  manda su mail e Telegram insieme, ognuno funziona anche da solo) sia bot
  interattivo: un thread in background fa long polling su `getUpdates`
  (nessun webhook da registrare — funziona anche in locale/Raspberry Pi
  dietro NAT). Il primo chat che scrive al bot si registra da sola come
  proprietaria (`telegram_chat_id` nelle impostazioni); qualsiasi chat
  diversa da quella viene ignorata in silenzio — un bot personale non deve
  rispondere a sconosciuti che ne scoprono lo username.
- Perché Gemini e non un'altra AI: è l'unica con un piano gratuito reale
  (nessuna carta di credito). Claude/OpenAI danno risposte probabilmente
  di qualità simile o superiore ma sono a consumo fin dal primo token —
  se in futuro vuoi cambiarlo, il codice tocca solo `generate_ai_commentary`,
  `extract_portfolio_from_image` e `generate_daily_verdict` in `app.py`.
- L'import da foto stima la quantità di azioni come
  `valore_posizione_€ / prezzo_di_mercato_attuale_in_€` (con conversione
  EUR/USD live via il ticker Yahoo `EURUSD=X`, cache di un'ora): è una
  stima, non un dato letto direttamente dallo screenshot, perché i broker
  in genere non mostrano il numero di azioni nella vista elenco.
- La ricerca ticker unisce un elenco locale di ~70 titoli comuni (sempre
  disponibile, istantaneo) ai risultati live della ricerca Yahoo quando
  raggiungibile — quindi i suggerimenti funzionano anche se Yahoo è
  bloccato, solo con una copertura più limitata.
- Un ticker che fallisce (rete, ticker inesistente, formato dati inatteso)
  non blocca l'analisi degli altri: ogni chiamata è avvolta in try/except.
- Le chiamate a Twelve Data sono limitate lato app a 6/minuto (il piano
  free ne consente 8): senza questo limite, il thread di background e un
  caricamento pagina concorrenti potevano sommare le richieste e far
  scattare 429 solo su alcuni ticker (sintomo tipico: "funziona per
  alcuni titoli e non per altri" subito dopo un riavvio/redeploy).
- Lo screener "Opportunità" gira sull'universo `BOTTLENECK_UNIVERSE`
  definito in `app.py` (personalizzabile modificando quella lista): per
  ogni titolo non già in portafoglio applica la stessa `compute_signal()`
  usata ovunque nell'app, tiene solo score ≥ 40, e manda al massimo una
  mail digest al giorno (deduplicata tramite `settings.last_screener_sent`).
- Lo stop loss dinamico usa `tickers.high_water_mark` (il massimo prezzo
  visto da quando possiedi il titolo) e uno scarto fisso del 10%
  (`TRAILING_STOP_PCT` in `app.py`): trasparente e modificabile, non un
  parametro nascosto.
- L'app non ha login/password: è pensata per un solo utilizzatore che
  imposta la propria email di notifica dalla UI.
- `POST /api/cron/tick` (protetto da `X-Cron-Secret`) aggiorna tutti i
  segnali **e** fa girare lo screener di mercato: è pensato per essere
  chiamato da un trigger esterno gratuito (GitHub Actions) quando
  l'hosting va in sleep.
