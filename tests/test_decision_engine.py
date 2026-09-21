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
import time
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
