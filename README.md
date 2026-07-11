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
avvio (si può cambiare in qualsiasi momento cliccando sulla tua email in
alto a destra).

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

- **Email per gli alert**: alla prima apertura l'app chiede solo
  l'indirizzo a cui mandare i segnali BUY/SELL. Si può cambiare in
  qualsiasi momento toccando l'email in alto a destra.
- **Scanner**: analisi on-demand di qualsiasi ticker (prezzo, RSI 14, MA50,
  MA200, distanza da massimo/minimo 52 settimane, segnale e motivazioni in
  italiano).
- **Portafoglio**: titoli tracciati (precompilato con MU, ASML, MSFT,
  SNDK, TSMC) con quantità, prezzo pagato, P&L in € e %, segnale attuale,
  soglie personali di acquisto/vendita. Aggiornamento automatico ogni ora
  (thread locale e/o tick esterno) o manuale dal pulsante "Aggiorna
  prezzi".
- **Alert**: soglie di prezzo (sopra/sotto) per qualsiasi ticker; quando
  scattano inviano una mail e vengono segnate come "scattato".
- **Storico**: ogni cambio di segnale (es. BUY → SELL) su un titolo
  tracciato viene registrato con data, prezzo e motivazione, con
  statistiche riassuntive dei cambi.

## Note tecniche

- I dati di mercato vengono presi direttamente dall'endpoint pubblico
  `chart` di Yahoo Finance via `requests` (nessuna libreria `yfinance`),
  con fallback automatico tra `query1` e `query2.finance.yahoo.com`.
- Un ticker che fallisce (rete, ticker inesistente, formato dati inatteso)
  non blocca l'analisi degli altri: ogni chiamata è avvolta in try/except.
- L'app non ha login/password: è pensata per un solo utilizzatore che
  imposta la propria email di notifica dalla UI.
- `POST /api/cron/tick` (protetto da `X-Cron-Secret`) aggiorna tutti i
  segnali: è pensato per essere chiamato da un trigger esterno gratuito
  (GitHub Actions) quando l'hosting va in sleep.
