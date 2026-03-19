"""
강화된 백테스팅 모듈

기존 backtester.py 대비 개선사항:
1. 신호 발생 캔들 다음 캔들 시가에 체결 (동일 캔들 즉시 체결 편향 제거)
2. 슬리피지 모델 (매수 +0.05%, 매도 -0.05%)
3. 리스크 지표: Sharpe, Sortino, Calmar ratio
4. Profit Factor (총수익 / 총손실)
5. 최대 연속 손실 횟수
6. 시장 국면 분류 (상승/하락/횡보) 별 성능 분리
7. 벤치마크 비교 (Buy & Hold)
"""

import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyupbit

from indicators import add_all_indicators, get_sell_signal, get_signal_score

logger = logging.getLogger(__name__)

# 슬리피지: 매수 시 불리하게, 매도 시 불리하게
SLIPPAGE_RATE = 0.0005  # 0.05%


class EnhancedBacktester:
    def __init__(self, config):
        self.config = config

    # ──────────────────────────────────────────────────────────
    # 데이터 수집
    # ──────────────────────────────────────────────────────────

    def fetch_historical_data(self, market: str, days: int = 90) -> pd.DataFrame:
        """과거 N일 분봉 데이터를 가져옵니다."""
        print(f"\n  📥 {market} {days}일치 {self.config.CANDLE_UNIT}분봉 데이터 수집 중...")
        all_dfs = []
        candles_per_call = 200
        minutes_per_call = candles_per_call * self.config.CANDLE_UNIT
        total_minutes = days * 24 * 60
        calls_needed = (total_minutes // minutes_per_call) + 2  # 여유분

        to = None
        for _ in range(calls_needed):
            try:
                df = pyupbit.get_ohlcv(
                    market,
                    interval=f"minute{self.config.CANDLE_UNIT}",
                    count=candles_per_call,
                    to=to,
                )
                if df is None or df.empty:
                    break
                all_dfs.append(df)
                to = df.index[0]
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"데이터 수집 오류 (무시): {e}")
                break

        if not all_dfs:
            raise RuntimeError("데이터를 가져올 수 없습니다.")

        combined = pd.concat(all_dfs).sort_index()
        combined = combined[~combined.index.duplicated(keep="first")]
        combined.columns = ["open", "high", "low", "close", "volume", "value"]

        cutoff = datetime.now() - timedelta(days=days)
        combined = combined[combined.index >= cutoff]
        print(f"  ✅ {len(combined)}개 캔들 수집 완료 ({combined.index[0].date()} ~ {combined.index[-1].date()})")
        return combined

    # ──────────────────────────────────────────────────────────
    # 시장 국면 분류
    # ──────────────────────────────────────────────────────────

    def _classify_market_regime(self, df: pd.DataFrame) -> pd.Series:
        """
        시장 국면을 캔들별로 분류합니다.
        - 'bull'     : close > EMA200 AND EMA50 > EMA200
        - 'bear'     : close < EMA200 AND EMA50 < EMA200
        - 'sideways' : 그 외
        """
        ema50  = df["close"].ewm(span=50,  adjust=False).mean()
        ema200 = df["close"].ewm(span=200, adjust=False).mean()

        regime = pd.Series("sideways", index=df.index)
        bull_mask = (df["close"] > ema200) & (ema50 > ema200)
        bear_mask = (df["close"] < ema200) & (ema50 < ema200)
        regime[bull_mask] = "bull"
        regime[bear_mask] = "bear"
        return regime

    # ──────────────────────────────────────────────────────────
    # 리스크 지표 계산
    # ──────────────────────────────────────────────────────────

    def _calc_sharpe(self, returns: list, risk_free_annual: float = 0.03) -> float:
        """연율화 Sharpe Ratio. 60분봉 기준 연율화 (365 * 24 periods/year)."""
        if len(returns) < 2:
            return 0.0
        arr = np.array(returns)
        periods_per_year = 365 * 24 / self.config.CANDLE_UNIT
        rf_per_period = risk_free_annual / periods_per_year
        excess = arr - rf_per_period
        std = arr.std()
        if std == 0:
            return 0.0
        return float(np.sqrt(periods_per_year) * excess.mean() / std)

    def _calc_sortino(self, returns: list, risk_free_annual: float = 0.03) -> float:
        """연율화 Sortino Ratio (하방 변동성만 페널티)."""
        if len(returns) < 2:
            return 0.0
        arr = np.array(returns)
        periods_per_year = 365 * 24 / self.config.CANDLE_UNIT
        rf_per_period = risk_free_annual / periods_per_year
        excess = arr - rf_per_period
        downside = arr[arr < 0]
        if len(downside) == 0:
            return float("inf")
        downside_std = downside.std()
        if downside_std == 0:
            return 0.0
        return float(np.sqrt(periods_per_year) * excess.mean() / downside_std)

    def _calc_calmar(self, total_return_pct: float, max_drawdown_pct: float) -> float:
        """Calmar Ratio = 총수익률 / |MDD|. 단순 비율 (연율화 미적용)."""
        if max_drawdown_pct == 0:
            return 0.0
        return total_return_pct / abs(max_drawdown_pct)

    def _calc_mdd(self, values: list) -> float:
        """최대 낙폭(MDD) 계산 — %로 반환."""
        if not values:
            return 0.0
        peak = values[0]
        mdd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = (v - peak) / peak * 100
            if dd < mdd:
                mdd = dd
        return mdd

    def _calc_max_consecutive_losses(self, sell_trades: list) -> int:
        """매도 거래 목록에서 최대 연속 손실 횟수 계산."""
        max_consec = 0
        current = 0
        for t in sell_trades:
            if t["pnl_krw"] < 0:
                current += 1
                max_consec = max(max_consec, current)
            else:
                current = 0
        return max_consec

    # ──────────────────────────────────────────────────────────
    # 메인 백테스트 실행
    # ──────────────────────────────────────────────────────────

    def run(
        self,
        market: str = None,
        days: int = 90,
        initial_capital: float = 1_000_000,
        df: pd.DataFrame = None,  # 외부 데이터 직접 전달 가능 (walk-forward 용)
    ) -> dict:
        """
        강화된 백테스팅 실행.

        개선 핵심:
        - 신호 발생(캔들 i close) → 다음 캔들(i+1) 시가에 체결
        - 매수: fill_price = next_open * (1 + SLIPPAGE_RATE)
        - 매도: fill_price = next_open * (1 - SLIPPAGE_RATE)
        """
        if market is None:
            market = self.config.MARKET

        use_trend_filter = getattr(self.config, "USE_TREND_FILTER", False)
        trend_strict     = getattr(self.config, "TREND_FILTER_STRICT", False)

        if df is None:
            df = self.fetch_historical_data(market, days)
            print(f"\n{'='*60}")
            print(f"  🔬 강화 백테스팅 | {market} | {days}일")
            print(f"  슬리피지: ±{SLIPPAGE_RATE*100:.2f}% | 수수료: {self.config.FEE_RATE*100:.3f}%")
            print(f"  진입기준: {self.config.MIN_SIGNAL_COUNT}개↑ | 손절: {self.config.STOP_LOSS_PCT*100:.1f}% | 익절: {self.config.TAKE_PROFIT_PCT*100:.1f}%")
            trend_label = "ON (strict)" if (use_trend_filter and trend_strict) else ("ON" if use_trend_filter else "OFF")
            print(f"  국면 필터: {trend_label}")
            print(f"{'='*60}")
        else:
            market = getattr(df, "_market", market)

        # 지표 계산 (pandas rolling/ewm은 미래 데이터 사용 안 함)
        df = df.copy()
        df = add_all_indicators(df, self.config)
        regime_series = self._classify_market_regime(df)
        df = df.dropna()
        regime_series = regime_series.loc[df.index]

        capital = initial_capital
        coin_qty = 0.0
        entry_price = 0.0
        highest_price = 0.0
        position = False

        trades = []
        candle_values = {}
        candle_returns = []
        prev_value = initial_capital

        # ── 핵심: i번 캔들에서 신호 감지 → i+1번 캔들 시가에 체결 ──
        for i in range(1, len(df) - 1):
            row      = df.iloc[i]
            next_row = df.iloc[i + 1]
            regime   = regime_series.iloc[i]

            if not position:
                # ── 국면 필터 (하락장 진입 차단) ──
                if use_trend_filter:
                    if trend_strict:
                        is_bear = row["ema_short"] < row["ema_trend"]   # EMA50 < EMA200
                    else:
                        is_bear = (row["close"] < row["ema_trend"]) and (row["ema_long"] < row["ema_trend"])
                    if is_bear:
                        # 포트폴리오 가치만 기록하고 진입 건너뜀
                        current_value = capital
                        candle_values[row.name] = current_value
                        ret = (current_value - prev_value) / prev_value if prev_value > 0 else 0.0
                        candle_returns.append(ret)
                        prev_value = current_value
                        continue

                # 변동성 필터
                if row["atr_pct"] < self.config.MIN_VOLATILITY_PCT:
                    pass
                else:
                    signal_result = get_signal_score(row, self.config)
                    if signal_result["score"] >= self.config.MIN_SIGNAL_COUNT:
                        trade_amount = min(self.config.TRADE_AMOUNT_KRW, capital * 0.95)
                        if trade_amount >= 5000:
                            # 매수: 다음 캔들 시가 + 슬리피지 (불리한 방향)
                            fill_price = next_row["open"] * (1 + SLIPPAGE_RATE)
                            fee = trade_amount * self.config.FEE_RATE
                            coin_qty   = (trade_amount - fee) / fill_price
                            entry_price    = fill_price
                            highest_price  = entry_price
                            capital       -= trade_amount
                            position       = True

                            trades.append({
                                "type":          "BUY",
                                "datetime":      next_row.name,
                                "signal_time":   row.name,
                                "price":         entry_price,
                                "amount_krw":    trade_amount,
                                "coin_qty":      coin_qty,
                                "fee":           fee,
                                "signal_score":  signal_result["score"],
                                "regime":        regime,
                            })
            else:
                highest_price = max(highest_price, row["close"])
                sell_result = get_sell_signal(row, self.config, entry_price, highest_price)

                if sell_result["should_sell"]:
                    # 매도: 다음 캔들 시가 - 슬리피지 (불리한 방향)
                    fill_price  = next_row["open"] * (1 - SLIPPAGE_RATE)
                    sell_value  = coin_qty * fill_price
                    fee         = sell_value * self.config.FEE_RATE
                    net_revenue = sell_value - fee
                    buy_cost    = entry_price * coin_qty * (1 + self.config.FEE_RATE)
                    pnl         = net_revenue - buy_cost
                    pnl_pct     = pnl / buy_cost * 100

                    capital  += net_revenue
                    position  = False

                    trades.append({
                        "type":       "SELL",
                        "datetime":   next_row.name,
                        "price":      fill_price,
                        "entry_price": entry_price,
                        "pnl_krw":    pnl,
                        "pnl_pct":    pnl_pct,
                        "fee":        fee,
                        "reason":     sell_result["reason"],
                        "coin_qty":   coin_qty,
                        "regime":     regime,
                    })
                    coin_qty = 0.0

            # 캔들별 포트폴리오 가치 기록
            current_value = capital + (coin_qty * row["close"] if position else 0)
            candle_values[row.name] = current_value
            ret = (current_value - prev_value) / prev_value if prev_value > 0 else 0.0
            candle_returns.append(ret)
            prev_value = current_value

        # 마지막 포지션 강제 청산
        if position and len(df) > 0:
            last = df.iloc[-1]
            fill_price  = last["close"] * (1 - SLIPPAGE_RATE)
            sell_value  = coin_qty * fill_price
            fee         = sell_value * self.config.FEE_RATE
            net_revenue = sell_value - fee
            buy_cost    = entry_price * coin_qty * (1 + self.config.FEE_RATE)
            pnl         = net_revenue - buy_cost
            pnl_pct     = pnl / buy_cost * 100
            capital    += net_revenue

            trades.append({
                "type":        "SELL",
                "datetime":    last.name,
                "price":       fill_price,
                "entry_price": entry_price,
                "pnl_krw":     pnl,
                "pnl_pct":     pnl_pct,
                "fee":         fee,
                "reason":      "백테스트 종료 (강제 청산)",
                "coin_qty":    coin_qty,
                "regime":      "unknown",
            })

        # ── 성과 집계 ──
        sell_trades = [t for t in trades if t["type"] == "SELL"]
        total_trades = len(sell_trades)
        winning = [t for t in sell_trades if t["pnl_krw"] >= 0]
        losing  = [t for t in sell_trades if t["pnl_krw"] < 0]

        win_rate     = len(winning) / total_trades * 100 if total_trades > 0 else 0.0
        total_pnl    = sum(t["pnl_krw"] for t in sell_trades)
        total_fees   = sum(t["fee"]     for t in trades)
        total_return = (capital - initial_capital) / initial_capital * 100

        avg_win  = float(np.mean([t["pnl_pct"] for t in winning])) if winning else 0.0
        avg_loss = float(np.mean([t["pnl_pct"] for t in losing]))  if losing  else 0.0
        max_win  = max((t["pnl_pct"] for t in winning), default=0.0)
        max_loss = min((t["pnl_pct"] for t in losing),  default=0.0)

        gross_profit = sum(t["pnl_krw"] for t in winning)
        gross_loss   = abs(sum(t["pnl_krw"] for t in losing))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        mdd = self._calc_mdd(list(candle_values.values()))
        sharpe  = self._calc_sharpe(candle_returns)
        sortino = self._calc_sortino(candle_returns)
        calmar  = self._calc_calmar(total_return, mdd)
        max_consec_loss = self._calc_max_consecutive_losses(sell_trades)

        # 벤치마크 (Buy & Hold)
        bnh_return = (
            (df.iloc[-1]["close"] - df.iloc[0]["close"]) / df.iloc[0]["close"] * 100
            if len(df) > 1 else 0.0
        )

        # 국면별 성능
        regime_stats: dict = {}
        for regime in ("bull", "bear", "sideways"):
            r_trades = [t for t in sell_trades if t.get("regime") == regime]
            if r_trades:
                r_win = [t for t in r_trades if t["pnl_krw"] >= 0]
                regime_stats[regime] = {
                    "trades":       len(r_trades),
                    "win_rate":     len(r_win) / len(r_trades) * 100,
                    "avg_pnl_pct":  float(np.mean([t["pnl_pct"] for t in r_trades])),
                }
            else:
                regime_stats[regime] = {"trades": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0}

        results = {
            "market":                  market,
            "days":                    days,
            "initial_capital":         initial_capital,
            "final_capital":           capital,
            "total_return_pct":        total_return,
            "benchmark_return_pct":    bnh_return,
            "alpha_pct":               total_return - bnh_return,
            "total_trades":            total_trades,
            "win_rate_pct":            win_rate,
            "winning_trades":          len(winning),
            "losing_trades":           len(losing),
            "total_pnl_krw":           total_pnl,
            "total_fees_krw":          total_fees,
            "avg_win_pct":             avg_win,
            "avg_loss_pct":            avg_loss,
            "max_win_pct":             max_win,
            "max_loss_pct":            max_loss,
            "profit_factor":           profit_factor,
            "max_drawdown_pct":        mdd,
            "sharpe_ratio":            sharpe,
            "sortino_ratio":           sortino,
            "calmar_ratio":            calmar,
            "max_consecutive_losses":  max_consec_loss,
            "regime_stats":            regime_stats,
            "trades":                  trades,
            "candle_values":           candle_values,
        }

        self._print_results(results)
        return results

    # ──────────────────────────────────────────────────────────
    # 결과 출력
    # ──────────────────────────────────────────────────────────

    def _print_results(self, r: dict):
        print(f"\n{'='*60}")
        print(f"  🔬 강화 백테스팅 결과 | {r['market']} | {r['days']}일")
        print(f"{'='*60}")
        print(f"  초기 자금           : {r['initial_capital']:>12,.0f}원")
        print(f"  최종 자금           : {r['final_capital']:>12,.0f}원")
        print(f"  전략 수익률         : {r['total_return_pct']:>+12.2f}%")
        print(f"  Buy&Hold 수익률     : {r['benchmark_return_pct']:>+12.2f}%")
        print(f"  알파 (초과수익률)   : {r['alpha_pct']:>+12.2f}%")
        print("-" * 60)
        print(f"  샤프 비율           : {r['sharpe_ratio']:>12.3f}  (1.0↑ 우수)")
        print(f"  소르티노 비율       : {r['sortino_ratio']:>12.3f}  (2.0↑ 우수)")
        print(f"  칼마 비율           : {r['calmar_ratio']:>12.3f}  (0.5↑ 우수)")
        print(f"  최대 낙폭 (MDD)     : {r['max_drawdown_pct']:>+12.2f}%")
        print("-" * 60)
        print(f"  총 거래             : {r['total_trades']:>12}회")
        print(f"  승률                : {r['win_rate_pct']:>12.1f}%")
        print(f"  수익 거래           : {r['winning_trades']:>12}회  (평균 {r['avg_win_pct']:>+.2f}%)")
        print(f"  손실 거래           : {r['losing_trades']:>12}회  (평균 {r['avg_loss_pct']:>+.2f}%)")
        print(f"  손익비 (PF)         : {r['profit_factor']:>12.2f}  (1.5↑ 우수)")
        print(f"  최대 단일 수익      : {r['max_win_pct']:>+12.2f}%")
        print(f"  최대 단일 손실      : {r['max_loss_pct']:>+12.2f}%")
        print(f"  최대 연속 손실      : {r['max_consecutive_losses']:>12}회")
        print(f"  총 수수료           : {r['total_fees_krw']:>12,.0f}원")
        print(f"  순손익              : {r['total_pnl_krw']:>+12,.0f}원")
        print("-" * 60)
        print("  시장 국면별 성능:")
        regime_kr = {"bull": "상승장", "bear": "하락장", "sideways": "횡보장"}
        for regime, stats in r["regime_stats"].items():
            if stats["trades"] > 0:
                print(
                    f"    {regime_kr[regime]:<6}: {stats['trades']:>3}회 | "
                    f"승률 {stats['win_rate']:>5.1f}% | "
                    f"평균손익 {stats['avg_pnl_pct']:>+.2f}%"
                )
        print("=" * 60)

        # 최근 거래 내역 (최대 15건)
        sell_trades = [t for t in r["trades"] if t["type"] == "SELL"]
        if sell_trades:
            print("\n  📋 최근 거래 내역 (최대 15건):")
            print(f"  {'일시':<18} {'진입가':>10} {'청산가':>10} {'손익%':>8} {'손익(원)':>10}  사유")
            print("  " + "-" * 70)
            for t in sell_trades[-15:]:
                sign = "✅" if t["pnl_krw"] >= 0 else "❌"
                print(
                    f"  {sign} {str(t['datetime'])[:16]:<16} "
                    f"{t['entry_price']:>10,.0f} "
                    f"{t['price']:>10,.0f} "
                    f"{t['pnl_pct']:>+8.2f}% "
                    f"{t['pnl_krw']:>+10,.0f}  "
                    f"{t['reason']}"
                )
