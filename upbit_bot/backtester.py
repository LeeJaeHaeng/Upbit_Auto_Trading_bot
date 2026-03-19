"""
백테스팅 모듈
실제 매매 전에 과거 데이터로 전략 성능을 검증합니다.
"""

import pandas as pd
import numpy as np
import pyupbit
import logging
from datetime import datetime, timedelta
from indicators import add_all_indicators, get_signal_score, get_sell_signal

logger = logging.getLogger(__name__)


class Backtester:
    def __init__(self, config):
        self.config = config

    def fetch_historical_data(self, days: int = 30) -> pd.DataFrame:
        """과거 N일 분봉 데이터를 가져옵니다."""
        print(f"\n📥 {days}일치 {self.config.CANDLE_UNIT}분봉 데이터 다운로드 중...")
        all_dfs = []
        # pyupbit는 한 번에 최대 200개 반환 → 여러 번 호출
        candles_per_call = 200
        minutes_per_call = candles_per_call * self.config.CANDLE_UNIT
        total_minutes = days * 24 * 60
        calls_needed = (total_minutes // minutes_per_call) + 1

        to = None
        for i in range(calls_needed):
            try:
                df = pyupbit.get_ohlcv(
                    self.config.MARKET,
                    interval=f"minute{self.config.CANDLE_UNIT}",
                    count=candles_per_call,
                    to=to,
                )
                if df is None or df.empty:
                    break
                all_dfs.append(df)
                to = df.index[0]  # 가장 오래된 시점 기준으로 다음 호출
                import time
                time.sleep(0.1)  # API 레이트 리밋 방지
            except Exception as e:
                logger.warning(f"데이터 수집 중 오류 (무시): {e}")
                break

        if not all_dfs:
            raise RuntimeError("데이터를 가져올 수 없습니다.")

        combined = pd.concat(all_dfs).sort_index()
        combined = combined[~combined.index.duplicated(keep="first")]
        combined.columns = ["open", "high", "low", "close", "volume", "value"]

        cutoff = datetime.now() - timedelta(days=days)
        combined = combined[combined.index >= cutoff]
        print(f"  ✅ {len(combined)}개 캔들 수집 완료 ({combined.index[0]} ~ {combined.index[-1]})")
        return combined

    def run(self, days: int = 30, initial_capital: float = 1_000_000) -> dict:
        """백테스팅 실행"""
        print(f"\n{'='*60}")
        print(f"  🧪 백테스팅 시작 | {days}일 | 초기자금={initial_capital:,.0f}원")
        print(f"  전략: RSI+MACD+볼린저+EMA+거래량 | 진입기준={self.config.MIN_SIGNAL_COUNT}개 이상")
        print(f"  손절={self.config.STOP_LOSS_PCT*100:.1f}% | 익절={self.config.TAKE_PROFIT_PCT*100:.1f}%")
        print(f"{'='*60}")

        df = self.fetch_historical_data(days)
        df = add_all_indicators(df, self.config)
        df = df.dropna()

        capital = initial_capital
        coin_qty = 0.0
        entry_price = 0.0
        highest_price = 0.0
        position = False

        trades = []
        daily_capital = {}

        for i in range(1, len(df)):
            row = df.iloc[i]
            date_str = row.name.strftime("%Y-%m-%d")

            # 포지션 없을 때 → 매수 신호 확인
            if not position:
                # 변동성 필터: ATR%가 최소 변동성 이상일 때만
                if row["atr_pct"] < self.config.MIN_VOLATILITY_PCT:
                    continue

                signal_result = get_signal_score(row, self.config)
                if signal_result["score"] >= self.config.MIN_SIGNAL_COUNT:
                    # 매수 실행
                    trade_amount = min(self.config.TRADE_AMOUNT_KRW, capital * 0.95)
                    if trade_amount < 5000:  # 업비트 최소 주문금액
                        continue
                    fee = trade_amount * self.config.FEE_RATE
                    coin_qty = (trade_amount - fee) / row["close"]
                    entry_price = row["close"]
                    highest_price = entry_price
                    capital -= trade_amount
                    position = True
                    trades.append({
                        "type": "BUY",
                        "datetime": row.name,
                        "price": entry_price,
                        "amount_krw": trade_amount,
                        "coin_qty": coin_qty,
                        "fee": fee,
                        "signal_score": signal_result["score"],
                    })

            # 포지션 있을 때 → 매도 신호 확인
            else:
                highest_price = max(highest_price, row["close"])
                sell_result = get_sell_signal(row, self.config, entry_price, highest_price)

                if sell_result["should_sell"]:
                    # 매도 실행
                    sell_value = coin_qty * row["close"]
                    fee = sell_value * self.config.FEE_RATE
                    net_revenue = sell_value - fee
                    buy_cost = entry_price * coin_qty + (entry_price * coin_qty * self.config.FEE_RATE)
                    pnl = net_revenue - buy_cost
                    pnl_pct = pnl / buy_cost * 100

                    capital += net_revenue
                    position = False

                    trades.append({
                        "type": "SELL",
                        "datetime": row.name,
                        "price": row["close"],
                        "entry_price": entry_price,
                        "pnl_krw": pnl,
                        "pnl_pct": pnl_pct,
                        "fee": fee,
                        "reason": sell_result["reason"],
                        "coin_qty": coin_qty,
                    })
                    coin_qty = 0.0

            # 일별 자금 기록
            current_value = capital + (coin_qty * row["close"] if position else 0)
            daily_capital[date_str] = current_value

        # 마지막에 포지션 강제 청산 (백테스트 종료)
        if position and len(df) > 0:
            last_row = df.iloc[-1]
            sell_value = coin_qty * last_row["close"]
            fee = sell_value * self.config.FEE_RATE
            net_revenue = sell_value - fee
            buy_cost = entry_price * coin_qty * (1 + self.config.FEE_RATE)
            pnl = net_revenue - buy_cost
            pnl_pct = pnl / buy_cost * 100
            capital += net_revenue
            trades.append({
                "type": "SELL",
                "datetime": last_row.name,
                "price": last_row["close"],
                "entry_price": entry_price,
                "pnl_krw": pnl,
                "pnl_pct": pnl_pct,
                "fee": fee,
                "reason": "백테스트 종료 (강제 청산)",
                "coin_qty": coin_qty,
            })

        # ── 결과 분석 ──
        sell_trades = [t for t in trades if t["type"] == "SELL"]
        total_trades = len(sell_trades)
        winning = [t for t in sell_trades if t["pnl_krw"] >= 0]
        losing = [t for t in sell_trades if t["pnl_krw"] < 0]
        win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sum(t["pnl_krw"] for t in sell_trades)
        total_fees = sum(t["fee"] for t in trades)
        total_return = (capital - initial_capital) / initial_capital * 100
        avg_win = np.mean([t["pnl_pct"] for t in winning]) if winning else 0
        avg_loss = np.mean([t["pnl_pct"] for t in losing]) if losing else 0
        max_win = max((t["pnl_pct"] for t in winning), default=0)
        max_loss = min((t["pnl_pct"] for t in losing), default=0)

        # 일별 수익률
        prev_val = initial_capital
        daily_returns = []
        for date in sorted(daily_capital):
            curr_val = daily_capital[date]
            daily_ret = (curr_val - prev_val) / prev_val * 100
            daily_returns.append(daily_ret)
            prev_val = curr_val
        avg_daily_return = np.mean(daily_returns) if daily_returns else 0

        # 최대 낙폭 (MDD)
        capitals = list(daily_capital.values())
        if capitals:
            peak = capitals[0]
            mdd = 0
            for v in capitals:
                if v > peak:
                    peak = v
                dd = (v - peak) / peak * 100
                if dd < mdd:
                    mdd = dd
        else:
            mdd = 0

        results = {
            "initial_capital": initial_capital,
            "final_capital": capital,
            "total_return_pct": total_return,
            "avg_daily_return_pct": avg_daily_return,
            "total_trades": total_trades,
            "win_rate_pct": win_rate,
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "total_pnl_krw": total_pnl,
            "total_fees_krw": total_fees,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "max_win_pct": max_win,
            "max_loss_pct": max_loss,
            "max_drawdown_pct": mdd,
            "daily_returns": daily_returns,
            "trades": trades,
        }

        self._print_results(results, days)
        return results

    def _print_results(self, r: dict, days: int):
        print(f"\n{'='*60}")
        print(f"        📈 백테스팅 결과 ({days}일)")
        print(f"{'='*60}")
        print(f"  초기 자금       : {r['initial_capital']:>12,.0f}원")
        print(f"  최종 자금       : {r['final_capital']:>12,.0f}원")
        print(f"  총 수익률       : {r['total_return_pct']:>+12.2f}%")
        print(f"  일 평균 수익률  : {r['avg_daily_return_pct']:>+12.2f}%")
        print(f"  최대 낙폭 (MDD) : {r['max_drawdown_pct']:>+12.2f}%")
        print("-" * 60)
        print(f"  총 거래         : {r['total_trades']:>12}회")
        print(f"  승률            : {r['win_rate_pct']:>12.1f}%")
        print(f"  수익 거래       : {r['winning_trades']:>12}회")
        print(f"  손실 거래       : {r['losing_trades']:>12}회")
        print(f"  평균 수익       : {r['avg_win_pct']:>+12.2f}%")
        print(f"  평균 손실       : {r['avg_loss_pct']:>+12.2f}%")
        print(f"  최대 단일 수익  : {r['max_win_pct']:>+12.2f}%")
        print(f"  최대 단일 손실  : {r['max_loss_pct']:>+12.2f}%")
        print(f"  총 수수료       : {r['total_fees_krw']:>12,.0f}원")
        print(f"  순손익          : {r['total_pnl_krw']:>+12,.0f}원")
        print("=" * 60)

        # 개별 거래 내역
        sell_trades = [t for t in r["trades"] if t["type"] == "SELL"]
        if sell_trades:
            print("\n  📋 거래 내역:")
            print(f"  {'일시':<20} {'진입가':>10} {'청산가':>10} {'손익%':>8} {'손익(원)':>10} {'사유'}")
            print("  " + "-" * 72)
            for t in sell_trades[-20:]:  # 최근 20개만 출력
                sign = "✅" if t["pnl_krw"] >= 0 else "❌"
                print(
                    f"  {sign} {str(t['datetime'])[:16]:<18} "
                    f"{t['entry_price']:>10,.0f} "
                    f"{t['price']:>10,.0f} "
                    f"{t['pnl_pct']:>+8.2f}% "
                    f"{t['pnl_krw']:>+10,.0f} "
                    f"{t['reason']}"
                )
