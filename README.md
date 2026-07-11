# Cecchino Pro

Sistema di trading signal monitoring per Raspberry Pi 5, con accesso web da
qualsiasi browser mobile sulla rete locale. Analizza i titoli in portafoglio
(MU, ASML, MSFT, SNDK, TSMC di default), calcola segnali BUY/HOLD/SELL basati
su RSI, medie mobili e distanza dai massimi/minimi a 52 settimane, e invia
alert via mail quando un segnale cambia o una soglia di prezzo viene superata.

## Requisiti

- Raspberry Pi 5 con Raspberry Pi OS / Ubuntu (Python 3.11+)
- Connessione internet (per interrogare Yahoo Finance)
- Un account Gmail con una **password per le app** (non la password normale)

## Installazione

```bash
# 1. Copia il progetto sul Raspberry Pi (es. via git clone o scp), poi:
cd ~/cecchino

# 2. Crea un ambiente virtuale
python3 -m venv venv
source venv/bin/activate

# 3. Installa le dipendenze
pip install -r requirements.txt

# 4. Configura le credenziali email
cp .env.example .env
nano .env
```

Nel file `.env` inserisci:

```
CECCHINO_EMAIL_FROM=tuonome@gmail.com
CECCHINO_EMAIL_TO=tuonome@gmail.com
CECCHINO_EMAIL_PASSWORD=xxxx xxxx xxxx xxxx
```

La password per le app si genera su
`https://myaccount.google.com/apppasswords` (richiede la verifica in due
passaggi attiva sull'account Google). **Non committare mai il file `.env`**:
è già escluso da `.gitignore`.

## Avvio manuale (test)

```bash
source venv/bin/activate
python app.py
```

Trova l'IP locale del Raspberry Pi con `hostname -I`, poi apri da un
telefono/PC sulla stessa rete:

```
http://<IP-DEL-RASPBERRY>:5000
```

Al primo avvio il database `signals.db` viene creato automaticamente con i
5 titoli di default e le relative soglie.

## Avvio automatico all'accensione (systemd)

Crea il file `/etc/systemd/system/cecchino.service`:

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

Poi abilita e avvia il servizio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cecchino
sudo systemctl start cecchino

# Controlla i log in tempo reale
sudo journalctl -u cecchino -f
```

Il servizio riparte automaticamente ad ogni riavvio del Raspberry Pi e in
caso di crash.

## Funzionalità

- **Scanner**: analisi on-demand di qualsiasi ticker (prezzo, RSI 14, MA50,
  MA200, distanza da massimo/minimo 52 settimane, segnale e motivazioni in
  italiano).
- **Portafoglio**: titoli tracciati con quantità, prezzo pagato, P&L in € e
  %, segnale attuale, soglie personali di acquisto/vendita. Aggiornamento
  automatico ogni ora (thread in background) o manuale dal pulsante
  "Aggiorna prezzi".
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
- Il monitor in background gira ogni ora; in caso di errore riprova dopo 5
  minuti invece di bloccarsi.
- Tutti i dati restano in locale in `signals.db` (SQLite), nessun servizio
  cloud coinvolto oltre a Yahoo Finance (lettura) e Gmail SMTP (invio mail).
