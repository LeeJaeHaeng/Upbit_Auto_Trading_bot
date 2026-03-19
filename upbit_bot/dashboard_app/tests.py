from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import Client, SimpleTestCase

from dashboard_app import services


class DashboardViewTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_overview_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "대시보드")

    def test_trades_page_renders(self):
        response = self.client.get("/trades/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "거래 내역")

    def test_chart_page_renders(self):
        response = self.client.get("/chart/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "실시간 차트")

    @patch("dashboard_app.views.services.get_overview_data")
    def test_api_overview(self, mock_overview):
        mock_overview.return_value = {"total_trades": 3, "win_rate": 66.7}
        response = self.client.get("/api/overview/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_trades"], 3)
        self.assertEqual(payload["win_rate"], 66.7)

    @patch("dashboard_app.views.services.get_chart_data")
    def test_api_chart(self, mock_chart):
        mock_chart.return_value = [{"price": 1}]
        response = self.client.get("/api/chart/KRW-BTC/60/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"price": 1}])

    @patch("dashboard_app.views.services.get_chart_data")
    def test_api_chart_numpy_bool_serialization(self, mock_chart):
        mock_chart.return_value = {"signals": {"rsi": np.bool_(True)}, "signal_score": np.int64(3)}
        response = self.client.get("/api/chart/KRW-BTC/60/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIs(payload["signals"]["rsi"], True)
        self.assertEqual(payload["signal_score"], 3)

    @patch("dashboard_app.views.services.get_market_env")
    def test_api_market_env(self, mock_env):
        mock_env.return_value = {"score": 10, "recommendation": "보통"}
        response = self.client.get("/api/market-env/KRW-BTC/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["score"], 10)
        self.assertEqual(payload["recommendation"], "보통")

    @patch("dashboard_app.views.services.get_cumulative_pnl")
    def test_api_cumulative(self, mock_cumulative):
        mock_cumulative.return_value = [{"timestamp": "2026-01-01 00:00:00", "cumulative": 1000}]
        response = self.client.get("/api/cumulative/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["cumulative"], 1000)

    @patch("dashboard_app.services.pyupbit.get_ohlcv")
    def test_get_chart_data_contains_candle_series(self, mock_ohlcv):
        idx = pd.date_range("2026-01-01", periods=260, freq="h")
        base = np.linspace(100.0, 130.0, 260)
        mock_ohlcv.return_value = pd.DataFrame({
            "open": base,
            "high": base + 1.5,
            "low": base - 1.5,
            "close": base + np.sin(np.linspace(0, 10, 260)),
            "volume": np.linspace(10_000, 20_000, 260),
            "value": np.linspace(1_000_000, 2_000_000, 260),
        }, index=idx)

        payload = services.get_chart_data("KRW-BTC", 60)

        self.assertIn("candles", payload)
        self.assertIn("volume_bars", payload)
        self.assertGreater(len(payload["candles"]), 0)
        self.assertEqual(len(payload["candles"]), len(payload["volume_bars"]))
        self.assertIn("time", payload["candles"][0])
        self.assertIn("open", payload["candles"][0])
        self.assertIn("close", payload["candles"][0])
