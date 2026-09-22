"""
Regression test obbligatori per il Decision Engine (vedi master prompt
sezione 52). Copre i 5 scenari critici originali (ORCL, dati completi,
copertura sopra/sotto soglia, Critical-Data Gate) più i regression test
aggiunti per bloccare i bug corretti in questa sessione: doppio canale di
alert Telegram, notizie positive scambiate per rischio, deduplica headline,
e lo storico delle decisioni (Storico) che deve leggere dalla stessa fonte
degli alert.

Eseguibile senza rete e senza dipendenze extra:
    python3 -m unittest tests.test_decision_engine -v
(dalla cartella del repository, con signals.db non condiviso con l'app in
esecuzione: ogni test usa un DB temporaneo dedicato).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CECCHINO_CRON_SECRET", "test-secret")

import app  # noqa: E402
import config  # noqa: E402


FAKE_MARKET = {"closes": [100.0] * 260, "volumes": [1000.0] * 260, "price": 150.0,
               "currency": "USD", "name": "Mock Inc"}

FAKE_FUND_FULL = {
    "pe": 20.0, "market_cap": 1e11, "fifty_two_week_high": 160.0,
    "fcf_ttm": 1e9, "ebitda_ttm": 5e9, "total_debt": 1e9, "total_cash": 2e9,
    "analyst_coverage": 8, "recommendation_key": "buy",
    "gross_margin_pct": 60.0, "operating_margin_pct": 25.0, "revenue_growth_yoy_pct": 35.0,
    "quarters": [{"revenue": 5e9, "ebit": 1.1e9, "rnd": 0.8e9}] * 4,
    "capex_ttm": -2e9, "next_earnings_date": None,
}


class NoNewsResponse:
    status_code = 200
    def json(self):
        return {"news": []}


class DecisionEngineRegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp_db.close()
        self._orig_db_path = app.DB_PATH
        app.DB_PATH = self._tmp_db.name
        app.init_db()
        self._orig_gemini_key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""  # zero AI per i test, come da default consigliato

    def tearDown(self):
        app.DB_PATH = self._orig_db_path
        config.GEMINI_API_KEY = self._orig_gemini_key
        try:
            os.unlink(self._tmp_db.name)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # TEST 1 — ORCL: bottleneck+fundamental mancanti, technical+news
    # disponibili. Il vecchio motore avrebbe dato BUY (75.5). Deve restare
    # HOLD (coverage 50% < soglia 65%), mai BUY.
    # ------------------------------------------------------------------
    def test_01_orcl_missing_layers_buy_must_be_blocked(self):
        # analyze_bottleneck fallito del tutto (fetch bloccato/rate-limited,
        # come accaduto su Render): fundamental E bottleneck mancano
        # entrambi, esattamente come nello screenshot storico.
        with patch("app.fetch_market_data", return_value=FAKE_MARKET), \
             patch("app.analyze_bottleneck", return_value={"ticker": "ORCL", "error": "dati non disponibili"}), \
             patch.object(app.YAHOO_SESSION, "get", return_value=NoNewsResponse()):
            result = app.evaluate_decision("ORCL")

        self.assertNotEqual(result["decision"], "BUY",
                             "TEST 1 FALLITO: ORCL non deve poter tornare BUY con fundamental/bottleneck mancanti")
        self.assertEqual(result["decision"], "HOLD")
        self.assertTrue(any(c.startswith("DATA-COVERAGE-LOW") for c in result["reason_codes"]))
        self.assertLess(result["coverage_pct"], config.DECISION_MIN_WEIGHT_COVERAGE * 100)

    # ------------------------------------------------------------------
    # TEST 2 — dati completi: il comportamento deve restare quello atteso
    # (BUY quando il punteggio è alto e la copertura è piena).
    # ------------------------------------------------------------------
    def test_02_full_data_unchanged(self):
        with patch("app.fetch_market_data", return_value={**FAKE_MARKET, "price": 200.0}), \
             patch("app.fetch_yahoo_history", return_value=[90.0 + i * 0.5 for i in range(156)]), \
             patch("app.fetch_yahoo_fundamentals", return_value=FAKE_FUND_FULL), \
             patch.object(app.YAHOO_SESSION, "get", return_value=NoNewsResponse()):
            result = app.evaluate_decision("GOODCO")

        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertIn(result["decision"], ("BUY", "HOLD", "SELL"))
        self.assertNotEqual(result["decision"], "DATA_UNAVAILABLE")
        self.assertFalse(any(c.startswith("DATA-COVERAGE-LOW") for c in result["reason_codes"]))

    # ------------------------------------------------------------------
    # TEST 3 — un solo livello mancante (news), copertura 85% >= soglia:
    # il motore deve poter comunque decidere normalmente (niente blocco
    # da copertura).
    # ------------------------------------------------------------------
    def test_03_one_level_missing_coverage_above_threshold(self):
        layers = {
            "technical": {"score": 80, "codes": [], "raw": {}},
            "fundamental": {"score": 85, "codes": [], "raw": {"filters": [
                {"key": "fcf", "status": "pass", "value": 1, "threshold": 0, "unit": "€"},
                {"key": "net_debt_ebitda", "status": "pass", "value": 1, "threshold": 3, "unit": "x"},
            ]}},
            "bottleneck": {"score": 80, "codes": [], "raw": {}},
            "news": {"score": None, "codes": [], "raw": None},
        }
        result = app._finalize_decision("PARTIAL1", 100.0, layers, {"price": True, "fundamentals": True, "news": False})
        coverage = config.DECISION_WEIGHTS["technical"] + config.DECISION_WEIGHTS["fundamental"] + config.DECISION_WEIGHTS["bottleneck"]
        self.assertAlmostEqual(result["coverage_pct"], round(coverage * 100, 1))
        self.assertGreaterEqual(result["coverage_pct"], config.DECISION_MIN_WEIGHT_COVERAGE * 100)
        self.assertNotEqual(result["decision"], "DATA_UNAVAILABLE")

    # ------------------------------------------------------------------
    # TEST 4 — due livelli mancanti (fundamental+bottleneck), copertura
    # 50% < soglia 65%: deve restare HOLD qualunque sia il punteggio.
    # ------------------------------------------------------------------
    def test_04_two_levels_missing_coverage_below_threshold(self):
        layers = {
            "technical": {"score": 95, "codes": [], "raw": {}},
            "fundamental": {"score": None, "codes": [], "raw": None},
            "bottleneck": {"score": None, "codes": [], "raw": None},
            "news": {"score": 0, "codes": [], "raw": {}},
        }
        result = app._finalize_decision("PARTIAL2", 100.0, layers, {"price": True, "fundamentals": False, "news": True})
        self.assertEqual(result["decision"], "HOLD")
        self.assertTrue(any(c.startswith("DATA-COVERAGE-LOW") for c in result["reason_codes"]))

    # ------------------------------------------------------------------
    # TEST 5 — Critical-Data Gate: copertura sufficiente (>=65%) ma FCF
    # specificamente mancante tra i filtri di Motore A -> BUY_BLOCKED,
    # anche con punteggio alto.
    # ------------------------------------------------------------------
    def test_05_critical_fundamental_missing_buy_blocked(self):
        # Copertura piena (tutti e 4 i livelli disponibili, punteggi alti
        # da attraversare la soglia BUY) ma il filtro "fcf" di Motore A è
        # esplicitamente "missing" — deve bloccare comunque il BUY.
        layers = {
            "technical": {"score": 90, "codes": [], "raw": {}},
            "fundamental": {"score": 90, "codes": [], "raw": {"filters": [
                {"key": "fcf", "status": "missing", "value": None, "threshold": 0, "unit": "€"},
                {"key": "net_debt_ebitda", "status": "pass", "value": 1, "threshold": 3, "unit": "x"},
                {"key": "pe", "status": "pass", "value": 15, "threshold": 35, "unit": "x"},
            ]}},
            "bottleneck": {"score": 90, "codes": [], "raw": {}},
            "news": {"score": 0, "codes": [], "raw": {}},
        }
        result = app._finalize_decision("NOFCF", 200.0, layers, {"price": True, "fundamentals": True, "news": True})

        self.assertEqual(result["coverage_pct"], 100.0,
                          "il test non è valido se anche la coverage totale è insufficiente")
        self.assertEqual(result["decision"], "BUY_BLOCKED",
                          "TEST 5 FALLITO: FCF mancante deve bloccare il BUY anche con copertura sufficiente")
        self.assertTrue(any(c.startswith("HARD-BLOCK-CRITICAL-FUNDAMENTAL-MISSING") for c in result["reason_codes"]))

    # ------------------------------------------------------------------
    # TEST 6 — un solo canale di alert: notify_decision_change deve
    # mandare esattamente UN messaggio Telegram e nessun secondo alert
    # dal segnale tecnico (record_signal_if_changed non deve più chiamare
    # nessuna funzione di notifica propria).
    # ------------------------------------------------------------------
    def test_06_single_alert_channel_no_duplicate_telegram(self):
        self.assertFalse(hasattr(app, "notify_signal_change"),
                          "TEST 6 FALLITO: notify_signal_change non deve più esistere, "
                          "altrimenti può rimandare un secondo alert indipendente")
        conn = app.get_db()
        try:
            with patch("app.send_telegram") as mock_telegram, patch("app.send_mail"):
                app.record_signal_if_changed(conn, "MU", {
                    "signal": "BUY", "price": 100.0, "score": 60, "rsi": 55, "reasons": ["test"],
                })
            mock_telegram.assert_not_called()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # TEST 7 — BUY_BLOCKED ha un messaggio Telegram dedicato (regola 36):
    # niente falso silenzio quando un BUY viene fermato dai gate sui dati.
    # ------------------------------------------------------------------
    def test_07_buy_blocked_has_dedicated_telegram_message(self):
        result = {
            "ticker": "NOFCF", "decision": "BUY_BLOCKED", "previous_decision": "HOLD",
            "final_score": 82.0, "coverage_pct": 100.0,
            "reason_codes": ["HARD-BLOCK-CRITICAL-FUNDAMENTAL-MISSING:fcf"],
            "price": 200.0, "filter_version": config.DECISION_ENGINE_VERSION,
        }
        with patch("app.send_telegram") as mock_telegram, patch("app.send_mail"):
            app.notify_decision_change(result)
        mock_telegram.assert_called_once()
        sent_text = mock_telegram.call_args[0][0]
        self.assertIn("BLOCCATA", sent_text)
        self.assertIn("BUY_BLOCKED", sent_text)

    # ------------------------------------------------------------------
    # TEST 8 — solo eventi negativi possono diventare il "best" evento
    # usato per lo scoring/rischio: un major contract (positivo, severità
    # alta) non deve mai essere scambiato per un evento di rischio.
    # ------------------------------------------------------------------
    def test_08_positive_news_never_becomes_risk_event(self):
        fake_items = [{
            "title": "Acme Corp wins major $5B multi-year contract",
            "publisher": "Reuters", "providerPublishTime": 1700000000,
            "link": "https://example.com/1",
        }]
        with patch("app._fetch_recent_news", return_value=fake_items):
            news = app.assess_news("ACME")
        self.assertIsNone(news["direction"],
                           "TEST 8 FALLITO: nessun evento negativo presente, 'best' deve restare vuoto")
        self.assertEqual(news["severity"], 0)
        self.assertEqual(len(news["events"]), 1)
        self.assertEqual(news["events"][0]["direction"], "positive")

    # ------------------------------------------------------------------
    # TEST 9 — deduplica: la stessa notizia ripresa da fonti/indicizzazioni
    # diverse (stesso titolo normalizzato) conta come un solo evento.
    # ------------------------------------------------------------------
    def test_09_duplicate_headlines_deduplicated(self):
        fake_items = [
            {"title": "Company X guidance cut for next quarter", "publisher": "Reuters",
             "providerPublishTime": 1700000000, "link": "https://example.com/a"},
            {"title": "Company X guidance cut for next quarter!", "publisher": "Bloomberg",
             "providerPublishTime": 1700000100, "link": "https://example.com/b"},
        ]
        with patch("app._fetch_recent_news", return_value=fake_items):
            news = app.assess_news("DUPX")
        self.assertEqual(len(news["events"]), 1,
                          "TEST 9 FALLITO: due titoli quasi identici devono contare come un solo evento")

    # ------------------------------------------------------------------
    # TEST 10 — /api/decisions/history legge solo le righe changed=1 (la
    # stessa fonte usata dagli alert), non ogni valutazione periodica.
    # ------------------------------------------------------------------
    def test_10_decisions_history_endpoint_matches_alert_source(self):
        conn = app.get_db()
        try:
            conn.execute(
                "INSERT INTO decisions (ticker, ts, price, decision, previous_decision, changed, "
                "final_score, reason_codes, filter_version) VALUES "
                "('MU', '2026-09-20T10:00:00', 105.0, 'HOLD', 'BUY', 1, 55.2, ?, '1.1')",
                (json.dumps(["DATA-COVERAGE-LOW-35PCT"]),),
            )
            conn.execute(
                "INSERT INTO decisions (ticker, ts, price, decision, previous_decision, changed, "
                "final_score, reason_codes, filter_version) VALUES "
                "('MU', '2026-09-20T11:00:00', 106.0, 'HOLD', 'HOLD', 0, 55.0, '[]', '1.1')"
            )
            conn.commit()
        finally:
            conn.close()

        client = app.app.test_client()
        resp = client.get("/api/decisions/history")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual(len(rows), 1, "TEST 10 FALLITO: deve tornare solo la riga con changed=1")
        self.assertEqual(rows[0]["decision"], "HOLD")
        self.assertEqual(rows[0]["previous_decision"], "BUY")
        self.assertEqual(rows[0]["filter_version"], "1.1")

    # ------------------------------------------------------------------
    # TEST 11 — nessun falso blocco: se tutti i filtri critici sono
    # presenti (nessuno "missing"), un BUY con copertura piena deve
    # restare BUY, mai BUY_BLOCKED.
    # ------------------------------------------------------------------
    def test_11_no_false_positive_buy_blocked_when_critical_fields_present(self):
        layers = {
            "technical": {"score": 90, "codes": [], "raw": {}},
            "fundamental": {"score": 90, "codes": [], "raw": {"filters": [
                {"key": "fcf", "status": "pass", "value": 1, "threshold": 0, "unit": "€"},
                {"key": "net_debt_ebitda", "status": "pass", "value": 1, "threshold": 3, "unit": "x"},
            ]}},
            "bottleneck": {"score": 90, "codes": [], "raw": {}},
            "news": {"score": 0, "codes": [], "raw": {}},
        }
        result = app._finalize_decision("GOODFCF", 200.0, layers, {"price": True, "fundamentals": True, "news": True})
        self.assertEqual(result["decision"], "BUY",
                          "TEST 11 FALLITO: con tutti i filtri critici presenti non deve mai scattare BUY_BLOCKED")

    # ------------------------------------------------------------------
    # TEST 12 — SELL deterministico: punteggio basso con copertura piena
    # deve dare SELL, non un HOLD "prudente" che nasconderebbe il segnale.
    # ------------------------------------------------------------------
    def test_12_low_score_full_coverage_gives_sell(self):
        layers = {
            "technical": {"score": 10, "codes": [], "raw": {}},
            "fundamental": {"score": 10, "codes": [], "raw": {"filters": [
                {"key": "fcf", "status": "pass", "value": 1, "threshold": 0, "unit": "€"},
                {"key": "net_debt_ebitda", "status": "pass", "value": 1, "threshold": 3, "unit": "x"},
            ]}},
            "bottleneck": {"score": 10, "codes": [], "raw": {}},
            "news": {"score": 100, "codes": [], "raw": {}},
        }
        result = app._finalize_decision("BADCO", 50.0, layers, {"price": True, "fundamentals": True, "news": True})
        self.assertEqual(result["decision"], "SELL")

    # ------------------------------------------------------------------
    # TEST 13 — bug reale trovato in produzione: un fetch fondamentali
    # completamente fallito (Yahoo bloccato in quel momento) veniva
    # salvato in cache per 24 ORE come "nessun dato", bloccando
    # Fundamental/Bottleneck su "non disponibile" tutto il giorno anche
    # se Yahoo tornava disponibile pochi minuti dopo. Un fallimento deve
    # usare una cache molto più corta (20 minuti) di un successo (24h).
    # ------------------------------------------------------------------
    def test_13_failed_fundamentals_fetch_is_not_cached_for_24h(self):
        with patch("app.fetch_yahoo_fundamentals", return_value=None), \
             patch("app.fetch_market_data", return_value=None), \
             patch("app.fetch_yahoo_history", return_value=None):
            first = app.get_fundamentals_cached("FAILCO")
        self.assertIsNone(first["fundamentals"])

        # 21 minuti dopo (oltre la cache-fallimento di 20 minuti, ben dentro
        # le 24h di una cache normale): deve ritentare, non servire la
        # cache vecchia.
        app._BOTTLENECK_MEM_CACHE.pop("FAILCO", None)
        conn = app.get_db()
        try:
            conn.execute("UPDATE bottleneck_cache SET fetched_at = ? WHERE ticker = 'FAILCO'",
                         (time.time() - 21 * 60,))
            conn.commit()
        finally:
            conn.close()

        with patch("app.fetch_yahoo_fundamentals", return_value={"pe": 20.0}), \
             patch("app.fetch_market_data", return_value={"price": 50, "closes": [1, 2],
                                                            "currency": "USD", "name": "x"}), \
             patch("app.fetch_yahoo_history", return_value=[10, 20]):
            second = app.get_fundamentals_cached("FAILCO")
        self.assertIsNotNone(second["fundamentals"],
                              "TEST 13 FALLITO: la cache di un fallimento non deve durare come quella di un successo")

    # ------------------------------------------------------------------
    # TEST 14 — bug reale trovato in produzione: il Verdetto giornaliero
    # AI restava bloccato per sempre su "Generazione in corso…" perché
    # una posizione con una cache di analisi incompleta (campo mancante,
    # es. da una versione precedente dell'app) faceva esplodere l'intero
    # endpoint con un 500 HTML, che il frontend non sapeva interpretare.
    # Una posizione con dati incompleti deve essere saltata, mai far
    # fallire l'intera risposta.
    # ------------------------------------------------------------------
    def test_14_verdict_endpoint_never_crashes_on_malformed_cached_analysis(self):
        orig_key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = "fake-key-for-test"
        conn = app.get_db()
        try:
            conn.execute("INSERT INTO tickers (ticker, qty, paid, active) VALUES ('BADCO', 10, 100, 1)")
            conn.commit()
        finally:
            conn.close()

        with app.CACHE_LOCK:
            app.LAST_ANALYSIS["BADCO"] = {"price": 50.0, "currency": "USD"}  # manca signal/score/rsi/dist_high52

        try:
            client = app.app.test_client()
            resp = client.post("/api/verdict/refresh")
            # Qualunque sia l'esito, DEVE essere JSON valido (mai una pagina
            # di errore HTML che lascia il frontend bloccato per sempre).
            body = resp.get_json()
            self.assertIsNotNone(body, "TEST 14 FALLITO: la risposta deve essere sempre JSON, mai HTML")
            self.assertIn(resp.status_code, (200, 400, 500))
        finally:
            config.GEMINI_API_KEY = orig_key
            with app.CACHE_LOCK:
                app.LAST_ANALYSIS.pop("BADCO", None)

    # ------------------------------------------------------------------
    # TEST 15 — bug reale trovato in produzione: la quota giornaliera
    # gratuita di Twelve Data (800/giorno) veniva superata (807/800 visto
    # in dashboard) perché l'app continuava a mandare richieste che
    # tornavano comunque 429 fino a fine giornata. Il budget manager deve
    # fermare le richieste PRIMA di superare la soglia di sicurezza, senza
    # fare alcuna chiamata di rete quando il budget è esaurito.
    # ------------------------------------------------------------------
    def test_15_twelvedata_stops_before_daily_budget_exceeded(self):
        self.assertTrue(app.twelvedata_budget_ok())
        for _ in range(config.TWELVEDATA_DAILY_BUDGET):
            app._record_provider_usage("twelvedata")
        self.assertFalse(app.twelvedata_budget_ok(),
                          "TEST 15 FALLITO: il budget deve considerarsi esaurito alla soglia configurata")

        orig_key = config.TWELVEDATA_API_KEY
        config.TWELVEDATA_API_KEY = "fake-key-for-test"
        try:
            with patch("app.requests.get") as mock_get:
                errors = []
                result = app.fetch_twelvedata("MU", errors)
            mock_get.assert_not_called()
            self.assertIsNone(result)
            self.assertTrue(any("budget" in e.lower() for e in errors))
        finally:
            config.TWELVEDATA_API_KEY = orig_key

    # ------------------------------------------------------------------
    # TEST 16 — le scansioni bulk su tutto l'universo (Opportunità,
    # Scansiona universo: ~30 titoli non in portafoglio) non devono
    # consumare la quota Twelve Data, riservata alle ricerche dirette
    # dell'utente e al portafoglio. use_twelvedata=False deve impedire
    # qualunque chiamata di rete a Twelve Data, anche se Yahoo e Stooq
    # falliscono entrambi.
    # ------------------------------------------------------------------
    def test_16_bulk_scans_skip_twelvedata_even_when_needed(self):
        orig_key = config.TWELVEDATA_API_KEY
        config.TWELVEDATA_API_KEY = "fake-key-for-test"
        try:
            with patch("app.fetch_yahoo", return_value=None), \
                 patch("app.fetch_stooq", return_value=None), \
                 patch("app.requests.get") as mock_get, \
                 patch("app.time.sleep", return_value=None):
                errors = []
                result = app.fetch_market_data("MU", errors, use_twelvedata=False)
            self.assertIsNone(result)
            mock_get.assert_not_called()
        finally:
            config.TWELVEDATA_API_KEY = orig_key

    # ------------------------------------------------------------------
    # TEST 17 — circuit breaker: un 429 REALE ricevuto da Twelve Data deve
    # bloccare nuove richieste per il periodo di raffreddamento, senza
    # nemmeno provare la rete — anche se il nostro conteggio locale del
    # budget pensa (a torto, es. dopo un redeploy) che ci sia ancora
    # margine. Copre il caso reale visto in produzione: un redeploy azzera
    # il contatore locale, ma la quota lato Twelve Data resta esaurita.
    # ------------------------------------------------------------------
    def test_17_twelvedata_circuit_breaker_trips_on_429(self):
        class Resp429:
            status_code = 429
            text = "Too Many Requests"
            headers = {}

        orig_key = config.TWELVEDATA_API_KEY
        config.TWELVEDATA_API_KEY = "fake-key-for-test"
        try:
            self.assertTrue(app.twelvedata_circuit_ok())
            with patch("app.requests.get", return_value=Resp429()):
                errors = []
                result = app.fetch_twelvedata("MU", errors)
            self.assertIsNone(result)
            self.assertFalse(app.twelvedata_circuit_ok(),
                              "TEST 17 FALLITO: un 429 reale deve attivare il circuit breaker")

            with patch("app.requests.get") as mock_get:
                errors2 = []
                result2 = app.fetch_twelvedata("ORCL", errors2)
            mock_get.assert_not_called()
            self.assertIsNone(result2)
        finally:
            config.TWELVEDATA_API_KEY = orig_key

    # ------------------------------------------------------------------
    # TEST 18 — audit Twelve Data: fetch_market_data deve avere una cache
    # breve. Una seconda richiesta per lo stesso ticker entro il TTL non
    # deve fare NESSUNA chiamata di rete, nemmeno a Yahoo (audit: prima di
    # questo fix un solo tick chiedeva lo stesso ticker 3-4 volte senza
    # alcuna cache in mezzo).
    # ------------------------------------------------------------------
    def test_18_price_cache_avoids_duplicate_network_call(self):
        app._PRICE_CACHE.clear()
        app._PRICE_INFLIGHT.clear()
        fake_data = {"closes": [1, 2, 3], "volumes": [1, 1, 1], "price": 100.0,
                     "currency": "USD", "name": "Mock"}
        with patch("app.fetch_yahoo", return_value=fake_data) as mock_yahoo:
            r1 = app.fetch_market_data("MU")
            r2 = app.fetch_market_data("MU")
        self.assertEqual(mock_yahoo.call_count, 1,
                          "TEST 18 FALLITO: la seconda chiamata entro il TTL deve usare la cache")
        self.assertEqual(r1, r2)

    # ------------------------------------------------------------------
    # TEST 19 — audit Twelve Data: richieste concorrenti per lo stesso
    # ticker (es. un passo del tick e una ricerca dell'utente nello stesso
    # istante) devono deduplicarsi: una sola richiesta di rete parte, le
    # altre aspettano e riusano il risultato.
    # ------------------------------------------------------------------
    def test_19_concurrent_requests_for_same_ticker_deduplicated(self):
        app._PRICE_CACHE.clear()
        app._PRICE_INFLIGHT.clear()
        call_count = {"n": 0}
        count_lock = threading.Lock()

        def slow_fetch(ticker, errors=None):
            with count_lock:
                call_count["n"] += 1
            time.sleep(0.3)
            return {"closes": [1, 2, 3], "volumes": [1, 1, 1], "price": 100.0,
                    "currency": "USD", "name": "Mock"}

        results = []
        results_lock = threading.Lock()

        def worker():
            r = app.fetch_market_data("ORCL")
            with results_lock:
                results.append(r)

        with patch("app.fetch_yahoo", side_effect=slow_fetch):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(call_count["n"], 1,
                          "TEST 19 FALLITO: 5 richieste concorrenti per lo stesso ticker devono produrre 1 sola chiamata di rete")
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(app._PRICE_INFLIGHT), 0,
                          "TEST 19 FALLITO: la entry in-flight deve essere ripulita dopo il completamento")

    # ------------------------------------------------------------------
    # TEST 20 — audit Twelve Data: run_market_screener (scansione bulk
    # ~30 titoli) non deve girare a ogni chiamata automatica — solo la
    # prima volta o dopo config.MARKET_SCREENER_MIN_INTERVAL_SECONDS.
    # Il refresh manuale (force=True) deve invece girare sempre.
    # ------------------------------------------------------------------
    def test_20_market_screener_throttled_unless_forced(self):
        app.SCREENER_CACHE["results"] = [{"ticker": "PREV"}]
        app.SCREENER_CACHE["updated"] = datetime.now().isoformat(timespec="seconds")
        with patch("app.analyze_ticker") as mock_analyze:
            result = app.run_market_screener(send_email=False)
        mock_analyze.assert_not_called()
        self.assertEqual(result, [{"ticker": "PREV"}])

        with patch("app.analyze_ticker", return_value={"error": "n/d"}) as mock_analyze:
            app.run_market_screener(send_email=False, force=True)
        mock_analyze.assert_called()

    # ==================================================================
    # DATA PROVIDER ROUTER + FINNHUB (master prompt: audit Twelve Data
    # cost, provider indipendenza) — test 21-28
    # ==================================================================

    def _reset_finnhub_state(self):
        app._FINNHUB_CANDLES_UNAVAILABLE = False
        app._PRICE_CACHE.clear()
        app._PRICE_INFLIGHT.clear()

    # ------------------------------------------------------------------
    # TEST 21 — Finnhub success: quando risponde con dati validi, deve
    # essere usato per primo (priorità PRICE/OHLCV: Finnhub > Yahoo >
    # Stooq > Twelve Data) e il risultato deve portare source="finnhub".
    # ------------------------------------------------------------------
    def test_21_finnhub_success_used_first(self):
        self._reset_finnhub_state()
        orig_key = config.FINNHUB_API_KEY
        config.FINNHUB_API_KEY = "fake-key"
        fake = {"closes": [1, 2, 3], "volumes": [1, 1, 1], "price": 100.0,
                "currency": "USD", "name": "Mock", "source": "finnhub"}
        try:
            with patch("app.fetch_finnhub", return_value=fake), patch("app.fetch_yahoo") as mock_yahoo:
                result = app.fetch_market_data("MU")
            self.assertEqual(result["source"], "finnhub")
            mock_yahoo.assert_not_called()
        finally:
            config.FINNHUB_API_KEY = orig_key

    # ------------------------------------------------------------------
    # TEST 22 — Finnhub 429: attiva il proprio circuit breaker (rule 6),
    # non deve fare retry aggressivo, e la catena deve comunque proseguire
    # sul provider successivo (Yahoo).
    # ------------------------------------------------------------------
    def test_22_finnhub_429_trips_circuit_and_falls_through(self):
        self._reset_finnhub_state()
        orig_key = config.FINNHUB_API_KEY
        config.FINNHUB_API_KEY = "fake-key"

        class Resp429:
            status_code = 429
            text = "rate limit exceeded"
            headers = {}

        fake_yahoo = {"closes": [1, 2, 3], "volumes": [1, 1, 1], "price": 50.0,
                      "currency": "USD", "name": "Mock", "source": "yahoo"}
        try:
            with patch("app.requests.get", return_value=Resp429()), patch("app.fetch_yahoo", return_value=fake_yahoo):
                result = app.fetch_market_data("ORCL")
            self.assertEqual(result["source"], "yahoo")
            self.assertFalse(app.finnhub_circuit_ok(),
                              "TEST 22 FALLITO: un 429 reale di Finnhub deve attivare il suo circuit breaker")

            # una seconda richiesta, con Finnhub ancora in cooldown, non deve
            # nemmeno provare la rete verso Finnhub
            with patch("app.requests.get") as mock_get, patch("app.fetch_yahoo", return_value=fake_yahoo):
                app._PRICE_CACHE.clear()
                app.fetch_market_data("ORCL")
            mock_get.assert_not_called()
        finally:
            config.FINNHUB_API_KEY = orig_key

    # ------------------------------------------------------------------
    # TEST 23 — Finnhub non disponibile sul piano corrente (403): deve
    # marcare UNAVAILABLE_ON_CURRENT_PLAN e MAI PIÙ ritentare quell'
    # endpoint in questo processo (rule 5), invece di continuare a
    # sprecare chiamate su qualcosa che sappiamo già non funzionare.
    # ------------------------------------------------------------------
    def test_23_finnhub_unavailable_on_plan_never_retried(self):
        self._reset_finnhub_state()
        orig_key = config.FINNHUB_API_KEY
        config.FINNHUB_API_KEY = "fake-key"

        class Resp403:
            status_code = 403
            text = "not available on your plan"
            headers = {}

        try:
            with patch("app.requests.get", return_value=Resp403()):
                app.fetch_finnhub("MU")
            self.assertTrue(app._FINNHUB_CANDLES_UNAVAILABLE)

            with patch("app.requests.get") as mock_get:
                result = app.fetch_finnhub("ORCL")
            mock_get.assert_not_called()
            self.assertIsNone(result)
        finally:
            config.FINNHUB_API_KEY = orig_key
            app._FINNHUB_CANDLES_UNAVAILABLE = False

    # ------------------------------------------------------------------
    # TEST 24 — catena di fallback completa: Finnhub e Yahoo falliscono,
    # Stooq risponde. Il sistema deve produrre PRICE con source="stooq",
    # non "ANALYSIS FAILED" (rule 27, primo scenario simulato).
    # ------------------------------------------------------------------
    def test_24_fallback_chain_produces_correct_source(self):
        self._reset_finnhub_state()
        fake_stooq = {"closes": [1] * 300, "volumes": [1] * 300, "price": 42.0,
                      "currency": "USD", "name": "Mock", "source": "stooq"}
        with patch("app.fetch_finnhub", return_value=None), \
             patch("app.fetch_yahoo", return_value=None), \
             patch("app.fetch_stooq", return_value=fake_stooq):
            result = app.analyze_ticker("MU")
        self.assertNotIn("error", result)
        self.assertEqual(result["price_source"], "stooq")

    # ------------------------------------------------------------------
    # TEST 25 — tutti i provider falliscono: deve tornare PARTIAL
    # ANALYSIS (errore esplicito con coverage ridotta a valle nel
    # Decision Engine), mai un dato inventato (rule 17-18).
    # ------------------------------------------------------------------
    def test_25_all_providers_fail_gives_partial_analysis_not_invented_data(self):
        self._reset_finnhub_state()
        with patch("app.fetch_finnhub", return_value=None), \
             patch("app.fetch_yahoo", return_value=None), \
             patch("app.fetch_stooq", return_value=None), \
             patch("app.fetch_twelvedata", return_value=None), \
             patch("app.time.sleep", return_value=None):
            result = app.analyze_ticker("MU")
        self.assertIn("error", result)
        self.assertNotIn("price", result)  # nessun prezzo inventato

        with patch("app.fetch_market_data", return_value=None):
            decision = app.evaluate_decision("MU")
        self.assertEqual(decision["decision"], "DATA_UNAVAILABLE")

    # ------------------------------------------------------------------
    # TEST 26 — stato provider (rule 11): EXHAUSTED quando il budget
    # giornaliero Twelve Data è finito, RATE_LIMITED quando il circuit
    # breaker è attivo, UNAVAILABLE quando non è configurato.
    # ------------------------------------------------------------------
    def test_26_provider_status_classification(self):
        self.assertEqual(app.get_provider_status("twelvedata", configured=False),
                          app.PROVIDER_STATUS_UNAVAILABLE)

        for _ in range(config.TWELVEDATA_DAILY_BUDGET):
            app._record_provider_usage("twelvedata")
        self.assertEqual(app.get_provider_status("twelvedata", configured=True),
                          app.PROVIDER_STATUS_EXHAUSTED)

    # ------------------------------------------------------------------
    # TEST 27 — regressione ORCL: la nuova architettura provider non deve
    # alterare l'esito del caso storico. Fundamental/bottleneck mancanti
    # -> HOLD, mai BUY, indipendentemente da quale provider prezzo abbia
    # risposto.
    # ------------------------------------------------------------------
    def test_27_orcl_regression_unaffected_by_provider_router(self):
        self._reset_finnhub_state()
        fake_price = {"closes": [100.0] * 260, "volumes": [1000.0] * 260, "price": 150.0,
                      "currency": "USD", "name": "Oracle Corp", "source": "yahoo"}
        with patch("app.fetch_finnhub", return_value=None), \
             patch("app.fetch_yahoo", return_value=fake_price), \
             patch("app.analyze_bottleneck", return_value={"ticker": "ORCL", "error": "dati non disponibili"}), \
             patch.object(app.YAHOO_SESSION, "get", return_value=NoNewsResponse()):
            result = app.evaluate_decision("ORCL")
        self.assertNotEqual(result["decision"], "BUY",
                             "TEST 27 FALLITO: il router provider non deve alterare la regressione ORCL")
        self.assertEqual(result["decision"], "HOLD")

    # ------------------------------------------------------------------
    # TEST 28 — data failure ≠ negative data (rule 18/31): un filtro con
    # dato mancante ha status "missing", mai "fail" — non deve mai essere
    # confuso con un dato negativo reale che invece produce "fail".
    # ------------------------------------------------------------------
    def test_28_missing_data_never_confused_with_negative_value(self):
        missing = app._mk_filter("fcf", "FCF", None, 0, "gte", "€")
        self.assertEqual(missing["status"], "missing")

        negative_real = app._mk_filter("fcf", "FCF", -500_000_000, 0, "gte", "€")
        self.assertEqual(negative_real["status"], "fail")
        self.assertNotEqual(missing["status"], negative_real["status"])

    # ------------------------------------------------------------------
    # TEST 29 — su richiesta esplicita: il monitoraggio automatico in
    # background (portafoglio, alert, Opportunità, Decision Engine,
    # verdetto AI) deve restare attivo per non perdere gli alert Telegram,
    # ma molto più raro di prima (era a ogni tick, ~10 minuti). Una
    # seconda chiamata subito dopo la prima deve essere saltata.
    # ------------------------------------------------------------------
    def test_29_scheduled_monitor_throttled_to_hours_not_every_tick(self):
        self.assertTrue(app._portfolio_monitor_due())

        with patch("app.refresh_all_portfolio") as m1, \
             patch("app.check_watch_levels") as m2, \
             patch("app.run_market_screener") as m3, \
             patch("app.generate_daily_verdict") as m4, \
             patch("app.run_decision_engine_for_portfolio") as m5:
            app.run_scheduled_monitor()
        self.assertTrue(all([m1.called, m2.called, m3.called, m4.called, m5.called]))
        self.assertFalse(app._portfolio_monitor_due(),
                          "TEST 29 FALLITO: subito dopo un run, il monitor non deve essere di nuovo dovuto")

        with patch("app.refresh_all_portfolio") as m1_again:
            app.run_scheduled_monitor()
        m1_again.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
