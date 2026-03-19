"""
거래 기록 및 성과 분석 모듈
"""

import csv
import json
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class TradeLogger:
    def __init__(self, log_file: str = "trading_log.csv", performance_file: str = "performance.json"):
        self.log_file = log_file
        self.performance_file = performance_file
        self._init_csv()
        self.performance = self._load_performance()

    def _init_csv(self):
        if not os.path.exists(self.log_file):
            with open(self.log_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "action", "market", "price",
                    "amount_krw", "coin_qty", "fee",
                    "entry_price", "exit_price", "pnl_krw", "pnl_pct",
                    "reason", "signal_score", "signals"
                ])

    def _load_performance(self) -> dict:
        if os.path.exists(self.performance_file):
            with open(self.performance_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "total_pnl_krw": 0.0,
            "total_fees_krw": 0.0,
            "max_profit_pct": 0.0,
            "max_loss_pct": 0.0,
            "daily_pnl": {},
            "paper_capital": 1_000_000,  # 시작 시뮬레이션 자금 100만원
            "paper_current": 1_000_000,
        }

    def _save_performance(self):
        with open(self.performance_file, "w", encoding="utf-8") as f:
            json.dump(self.performance, f, ensure_ascii=False, indent=2)

    def log_buy(
        self,
        market: str,
        price: float,
        amount_krw: float,
        coin_qty: float,
        fee: float,
        signal_score: int,
        signals: dict,
    ):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                ts, "BUY", market, price, amount_krw, coin_qty, fee,
                price, "", "", "", "매수", signal_score, str(signals)
            ])
        logger.info(f"[LOG] 매수 기록 | {market} | 가격={price:,.0f} | 금액={amount_krw:,.0f}원 | 신호점수={signal_score}")

    def log_sell(
        self,
        market: str,
        entry_price: float,
        exit_price: float,
        coin_qty: float,
        fee: float,
        reason: str,
    ):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        buy_value = entry_price * coin_qty
        sell_value = exit_price * coin_qty - fee
        buy_fee = buy_value * 0.0005
        pnl_krw = sell_value - buy_value - buy_fee
        pnl_pct = pnl_krw / buy_value * 100

        with open(self.log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                ts, "SELL", market, exit_price, "", coin_qty, fee,
                entry_price, exit_price, pnl_krw, pnl_pct, reason, "", ""
            ])

        # 성과 업데이트
        self.performance["total_trades"] += 1
        self.performance["total_pnl_krw"] += pnl_krw
        self.performance["total_fees_krw"] += fee
        if pnl_krw >= 0:
            self.performance["winning_trades"] += 1
        else:
            self.performance["losing_trades"] += 1

        self.performance["max_profit_pct"] = max(self.performance["max_profit_pct"], pnl_pct)
        self.performance["max_loss_pct"] = min(self.performance["max_loss_pct"], pnl_pct)

        # 일별 수익 기록
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self.performance["daily_pnl"]:
            self.performance["daily_pnl"][today] = 0.0
        self.performance["daily_pnl"][today] += pnl_krw

        # 페이퍼 자금 업데이트
        self.performance["paper_current"] += pnl_krw

        self._save_performance()

        pnl_emoji = "✅ 수익" if pnl_krw >= 0 else "❌ 손실"
        logger.info(
            f"[LOG] 매도 기록 | {pnl_emoji} | PnL={pnl_krw:+,.0f}원 ({pnl_pct:+.2f}%) | 사유={reason}"
        )

    def print_summary(self):
        p = self.performance
        total = p["total_trades"]
        wins = p["winning_trades"]
        win_rate = (wins / total * 100) if total > 0 else 0
        net_pnl = p["total_pnl_krw"]
        fees = p["total_fees_krw"]
        capital = p["paper_capital"]
        current = p["paper_current"]
        total_return = (current - capital) / capital * 100

        print("\n" + "=" * 55)
        print("        📊 자동매매 봇 성과 요약")
        print("=" * 55)
        print(f"  총 거래 수     : {total}회")
        print(f"  승률           : {win_rate:.1f}% ({wins}승 {p['losing_trades']}패)")
        print(f"  순손익         : {net_pnl:+,.0f}원")
        print(f"  총 수수료      : {fees:,.0f}원")
        print(f"  최대 수익      : {p['max_profit_pct']:+.2f}%")
        print(f"  최대 손실      : {p['max_loss_pct']:+.2f}%")
        print(f"  시작 자금      : {capital:,.0f}원")
        print(f"  현재 자금      : {current:,.0f}원")
        print(f"  누적 수익률    : {total_return:+.2f}%")
        print("-" * 55)
        if p["daily_pnl"]:
            print("  일별 손익:")
            for date, pnl in sorted(p["daily_pnl"].items()):
                sign = "+" if pnl >= 0 else ""
                print(f"    {date}: {sign}{pnl:,.0f}원")
        print("=" * 55)
