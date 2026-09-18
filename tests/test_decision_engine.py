"""
Regression test obbligatori per il Decision Engine (vedi master prompt
sezione 52). Copre i 5 scenari critici: il caso storico ORCL (BUY gonfiato
da dati mancanti), il comportamento invariato con dati completi, un solo
livello mancante (copertura sopra soglia), due livelli mancanti (copertura
sotto soglia) e il Critical-Data Gate (FCF mancante blocca il BUY anche con
copertura sufficiente).

Eseguibile senza rete e senza dipendenze extra:
    python3 -m unittest tests.test_decision_engine -v
(dalla cartella del repository, con signals.db non condiviso con l'app in
esecuzione: ogni test usa un DB temporaneo dedicato).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
