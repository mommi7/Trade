# Cecchino Pro

Sistema di trading signal monitoring multi-utente, con **login "Accedi con
Google"**: ogni utente ha il proprio portafoglio e riceve gli alert BUY/SELL
via mail al proprio indirizzo Google. Calcola segnali basati su RSI, medie
mobili e distanza dai massimi/minimi a 52 settimane, con Scanner, Portafoglio,
Alert e Storico.

Puoi farlo girare in due modi:

1. **Sul tuo Raspberry Pi** — davvero 24/7, gratis, senza limiti (vedi sotto).
2. **Su un hosting cloud gratuito (Render) + GitHub Actions** — comodo per
   testarlo velocemente da un link pubblico senza avere hardware acceso.
   Ha un limite importante: leggi la sezione "Limiti del free tier" prima
   di fidartene per soldi veri.

## 1. Configura il login Google e le mail (obbligatorio in entrambi i casi)

### 1a. Credenziali OAuth "Accedi con Google"

1. Vai su https://console.cloud.google.com/apis/credentials (crea un
   progetto se non ne hai già uno).
2. "Configura schermata consenso OAuth" → tipo "Esterno" → compila i campi
   obbligatori (nome app, email) → salva.
3. "Crea credenziali" → "ID client OAuth" → tipo applicazione **Web
   application**.
4. In "URI di reindirizzamento autorizzati" aggiungi:
   - `http://localhost:5000/auth/callback` (per test in locale)
   - `https://<il-tuo-dominio-render>.onrender.com/auth/callback` (per il
     deploy cloud, aggiungilo dopo il primo deploy quando conosci l'URL)
5. Copia il **Client ID** e il **Client Secret** generati.

### 1b. Password per le app Gmail (mittente delle mail)

Genera una password per le app su
`https://myaccount.google.com/apppasswords` (richiede la verifica in due
passaggi attiva). Questo è l'account che **invia** le mail — il
**destinatario** invece è automaticamente l'email Google di ogni utente che
fa login.

## 2. Opzione A — Raspberry Pi (always-on reale, gratis)

```bash
cd ~/cecchino
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # inserisci GOOGLE_CLIENT_ID/SECRET, CECCHINO_EMAIL_*, SECRET_KEY, PUBLIC_URL
```

Nel `.env`, imposta `CECCHINO_PUBLIC_URL=http://<IP-DEL-RASPBERRY>:5000` e
aggiungi lo stesso indirizzo + `/auth/callback` tra gli URI di
reindirizzamento OAuth (punto 1a). Poi:

```bash
python app.py
```

Apri da telefono/PC sulla stessa rete: `http://<IP-DEL-RASPBERRY>:5000`.

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
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
   - `CECCHINO_PUBLIC_URL` → l'URL che Render ti ha assegnato, es.
     `https://cecchino-pro.onrender.com`
   - `CECCHINO_SECRET_KEY` e `CECCHINO_CRON_SECRET` sono già generati
     automaticamente da Render (`generateValue: true`); puoi lasciarli.
5. Torna su Google Cloud Console (punto 1a) e aggiungi
   `https://cecchino-pro.onrender.com/auth/callback` tra gli URI di
   reindirizzamento autorizzati.
6. Render fa un redeploy automatico dopo il cambio env vars. Apri l'URL:
   dovresti vedere la schermata "Accedi con Google".

### 3b. Tenerlo sveglio e far girare il tick orario gratis, con GitHub Actions

Il piano gratuito Render mette il servizio in **sleep dopo ~15 minuti di
inattività** (si risveglia alla richiesta successiva, con qualche secondo di
attesa). Per far comunque scattare il controllo dei segnali ogni ora è
incluso un workflow GitHub Actions che chiama un endpoint dedicato e
risveglia l'app da solo:

`.github/workflows/cecchino-tick.yml` gira ogni ora (`cron: "0 * * * *"`,
gratuito e illimitato sui repo pubblici GitHub) e chiama
`POST /api/cron/tick` sull'app.

Configuralo così:

1. Nel repo GitHub → **Settings** → **Secrets and variables** → **Actions**
   → **New repository secret**, aggiungi:
   - `CECCHINO_URL` → es. `https://cecchino-pro.onrender.com`
   - `CECCHINO_CRON_SECRET` → lo stesso valore che Render ha generato per
     `CECCHINO_CRON_SECRET` (Render → Environment, copialo da lì)
2. Il workflow parte da solo ogni ora. Puoi anche lanciarlo a mano da
   **Actions** → **Cecchino Pro - tick orario** → **Run workflow**.

### Limiti del free tier (leggi prima di fidarti)

- **Filesystem effimero**: Render (piano free) non garantisce la
  persistenza del disco tra un riavvio/redeploy e l'altro. Il database
  SQLite (`signals.db`) può azzerarsi, perdendo utenti, portafoglio e
  storico. Va benissimo per **testare** che tutto funzioni; per un uso
  reale valuta un database gestito gratuito (es. Render Postgres free per
  90 giorni, o Supabase Postgres free) al posto di SQLite.
- **Sleep**: senza il tick di GitHub Actions, il monitor interno gira solo
  mentre il servizio è sveglio.
- Per un always-on **vero e senza compromessi**, il Raspberry Pi (Opzione
  A) resta la soluzione più solida: è hardware tuo, gira 24/7 senza limiti
  di piattaforma.

## Funzionalità

- **Login Google**: ogni utente accede con il proprio account; il
  portafoglio, gli alert e lo storico sono isolati per utente. Al primo
  accesso il portafoglio viene precompilato con i titoli di default (MU,
  ASML, MSFT, SNDK, TSMC) e le relative soglie.
- **Scanner**: analisi on-demand di qualsiasi ticker (prezzo, RSI 14, MA50,
  MA200, distanza da massimo/minimo 52 settimane, segnale e motivazioni in
  italiano).
- **Portafoglio**: titoli tracciati con quantità, prezzo pagato, P&L in € e
  %, segnale attuale, soglie personali di acquisto/vendita. Aggiornamento
  automatico ogni ora (thread locale e/o tick esterno) o manuale dal
  pulsante "Aggiorna prezzi".
- **Alert**: soglie di prezzo (sopra/sotto) per qualsiasi ticker; quando
  scattano inviano una mail all'indirizzo Google dell'utente e vengono
  segnate come "scattato".
- **Storico**: ogni cambio di segnale (es. BUY → SELL) su un titolo
  tracciato viene registrato con data, prezzo e motivazione, con
  statistiche riassuntive dei cambi.

## Note tecniche

- I dati di mercato vengono presi direttamente dall'endpoint pubblico
  `chart` di Yahoo Finance via `requests` (nessuna libreria `yfinance`),
  con fallback automatico tra `query1` e `query2.finance.yahoo.com`.
- Un ticker che fallisce (rete, ticker inesistente, formato dati inatteso)
  non blocca l'analisi degli altri: ogni chiamata è avvolta in try/except.
- L'autenticazione usa OAuth 2.0 / OpenID Connect con Google tramite
  Authlib; le sessioni sono firmate con `CECCHINO_SECRET_KEY`.
- `POST /api/cron/tick` (protetto da `X-Cron-Secret`) aggiorna i segnali di
  tutti gli utenti: è pensato per essere chiamato da un trigger esterno
  gratuito (GitHub Actions) quando l'hosting va in sleep.
