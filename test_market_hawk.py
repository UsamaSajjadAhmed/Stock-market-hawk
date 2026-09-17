import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("market_hawk", str(Path(__file__).with_name("main.py")))
mh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mh)


class MarketHawkTests(unittest.TestCase):
    def setUp(self):
        self.watchlist = mh.validate_watchlist(
            {
                "NVDA": {
                    "name": "NVIDIA",
                    "market": "US",
                    "drop_alert": -1,
                    "rise_alert": 1,
                    "alert_step": 1,
                }
            }
        )

    def test_watchlist_defaults_and_risk_override(self):
        rules = mh.validate_watchlist({"AAPL": {}})["AAPL"]
        self.assertEqual(rules["drop_alert"], -1.0)
        self.assertEqual(rules["rise_alert"], 1.0)
        self.assertEqual(rules["alert_step"], 1.0)
        self.assertTrue(rules["news_enabled"])

        manual = mh.validate_watchlist({"MSFT": {"risk": 44}})["MSFT"]
        self.assertEqual(manual["risk_override"]["score"], 44.0)
        self.assertEqual(manual["risk_override"]["level"], "medium")

    def test_fresh_quote_alerts_when_state_is_missing_and_dedupes(self):
        state = mh.create_default_state()
        sent = []

        with (
            patch.object(mh, "get_market_status", return_value={"is_open": True, "source": "test"}),
            patch.object(mh, "get_market_local_date", return_value="2026-07-14"),
            patch.object(mh, "get_stock_quote", return_value=(102.4, 100.0, 2.4)),
            patch.object(mh, "safe_send_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            quotes = mh.check_stocks(self.watchlist, state)
            self.assertIn("NVDA", quotes)
            self.assertEqual(len(sent), 1)
            self.assertEqual(state["symbols"]["NVDA"]["daily_status"], "rise")
            self.assertEqual(state["symbols"]["NVDA"]["last_alerted_step"], 2.0)

            mh.check_stocks(self.watchlist, state)
            self.assertEqual(len(sent), 1, "same threshold bucket must not repeat")

        with (
            patch.object(mh, "get_market_status", return_value={"is_open": True, "source": "test"}),
            patch.object(mh, "get_market_local_date", return_value="2026-07-14"),
            patch.object(mh, "get_stock_quote", return_value=(103.2, 100.0, 3.2)),
            patch.object(mh, "safe_send_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_stocks(self.watchlist, state)
            self.assertEqual(len(sent), 2)
            self.assertIn("+3%", sent[-1])

    def test_closed_market_does_not_send_price_alert(self):
        state = mh.create_default_state()
        sent = []
        with (
            patch.object(mh, "get_market_status", return_value={"is_open": False, "source": "test"}),
            patch.object(mh, "get_market_local_date", return_value="2026-07-14"),
            patch.object(mh, "get_stock_quote", return_value=(103.0, 100.0, 3.0)),
            patch.object(mh, "safe_send_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_stocks(self.watchlist, state)
        self.assertEqual(sent, [])
        self.assertEqual(state["symbols"]["NVDA"]["daily_status"], "normal")

    def test_news_first_run_baseline_then_only_new_article(self):
        state = mh.create_default_state()
        first = {
            "id": 1,
            "headline": "NVIDIA launches product",
            "summary": "New product",
            "datetime": 1_700_000_000,
            "source": "Example",
            "url": "https://example.com/1",
        }
        second = {
            "id": 2,
            "headline": "NVIDIA announces partnership",
            "summary": "Partnership announcement",
            "datetime": 1_700_000_100,
            "source": "Example",
            "url": "https://example.com/2",
        }
        sent = []

        with (
            patch.object(mh, "get_company_news", return_value=[first]),
            patch.object(mh, "safe_send_long_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_news(self.watchlist, state)
        self.assertEqual(sent, [])
        self.assertIn("id:1", state["symbols"]["NVDA"]["seen_news_ids"])

        with (
            patch.object(mh, "get_company_news", return_value=[first, second]),
            patch.object(mh, "safe_send_long_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_news(self.watchlist, state)
        self.assertEqual(len(sent), 1)
        self.assertIn("partnership", sent[0].lower())
        self.assertIn("id:2", state["symbols"]["NVDA"]["seen_news_ids"])

        with (
            patch.object(mh, "get_company_news", return_value=[first, second]),
            patch.object(mh, "safe_send_long_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_news(self.watchlist, state)
        self.assertEqual(len(sent), 1, "already seen news must not repeat")

    def test_news_is_not_marked_seen_when_all_telegram_delivery_fails(self):
        state = mh.create_default_state()
        state["symbols"]["NVDA"] = mh.create_default_symbol_state()
        state["symbols"]["NVDA"]["news_initialized"] = True
        article = {
            "id": 99,
            "headline": "NVIDIA important update",
            "datetime": 1_700_000_000,
            "source": "Example",
            "url": "https://example.com/99",
        }
        with (
            patch.object(mh, "get_company_news", return_value=[article]),
            patch.object(mh, "safe_send_long_telegram_message", return_value=False),
        ):
            mh.check_news(self.watchlist, state)
        self.assertNotIn("id:99", state["symbols"]["NVDA"]["seen_news_ids"])

    def test_calculated_risk_is_deterministic_and_bounded(self):
        candles = []
        price = 100.0
        for day in range(80):
            price *= 1 + (0.01 if day % 3 else -0.008)
            candles.append(
                {
                    "timestamp": float(day),
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                    "volume": 1_000.0,
                }
            )
        risk = mh.calculate_risk_from_candles(candles)
        self.assertGreaterEqual(risk["score"], 0)
        self.assertLessEqual(risk["score"], 100)
        self.assertIn(risk["level"], mh.ALLOWED_RISK_LEVELS)
        self.assertEqual(risk["source"], "calculated")

    def test_state_round_trip(self):
        state = mh.create_default_state()
        state["symbols"]["NVDA"] = mh.create_default_symbol_state()
        state["symbols"]["NVDA"]["last_price"] = 123.45

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "alert_state.json"
            with patch.object(mh, "ALERT_STATE_PATH", state_path):
                mh.save_alert_state(state)
                loaded = mh.load_alert_state()
        self.assertEqual(loaded["symbols"]["NVDA"]["last_price"], 123.45)

    def test_earnings_reminder_deduplication(self):
        state = mh.create_default_state()
        today = datetime.now(tz=mh.UK_TIMEZONE).date()
        event = {
            "date": (today + mh.timedelta(days=1)).isoformat(),
            "hour": "amc",
            "epsEstimate": 1.2,
        }
        sent = []
        with (
            patch.object(mh, "get_earnings_calendar", return_value=[event]),
            patch.object(mh, "safe_send_telegram_message", side_effect=lambda message, chat_id=None: sent.append(message) or True),
        ):
            mh.check_earnings(self.watchlist, state)
            mh.check_earnings(self.watchlist, state)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
