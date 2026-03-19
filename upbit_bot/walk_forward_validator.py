"""
Walk-Forward 검증 모듈

In-Sample(IS) vs Out-of-Sample(OOS) 성능 비교로 과적합 여부를 정량 측정합니다.

방법:
  전체 데이터를 N개 윈도우로 분할
  각 윈도우의 앞 is_ratio% → IS 구간 (훈련)
  각 윈도우의 뒤 (1-is_ratio)% → OOS 구간 (검증)

  IS/OOS 성능 저하율이 낮을수록 전략이 견고함:
    < 30%  : ✅ 과적합 위험 낮음
    30~60% : ⚠️  재검토 권장
    > 60%  : ❌ 과적합 가능성 높음

추가 분석:
  - 구간별 시장 추세 분류 (상승/하락/횡보)
  - 전체 OOS 구간을 이어붙인 누적 수익 곡선
  - 파라미터 민감도 테스트 (MIN_SIGNAL_COUNT 변화에 따른 OOS 성능)
"""

import logging
import time
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyupbit

from enhanced_backtester import EnhancedBacktester
from indicators import add_all_indicators, get_sell_signal, get_signal_score
from enhanced_backtester import SLIPPAGE_RATE

logger = logging.getLogger(__name__)


def _copy_config(config, overrides: dict = None) -> types.SimpleNamespace:
    """
    config 모듈(또는 SimpleNamespace)을 SimpleNamespace로 복사합니다.
    모듈 객체는 pickle 불가이므로 스칼라 값만 복사합니다.
    overrides: 덮어쓸 파라미터 dict
    """
    scalar_types = (int, float, str, bool)
    ns = types.SimpleNamespace()
    src = vars(config) if hasattr(config, "__dict__") else {}
    for k, v in src.items():
        if not k.startswith("_") and isinstance(v, scalar_types):
            setattr(ns, k, v)
    if overrides:
        for k, v in overrides.items():
            setattr(ns, k, v)
    return ns


class WalkForwardValidator:
    def __init__(self, config):
        self.config = config
        self.bt = EnhancedBacktester(config)

    # ──────────────────────────────────────────────────────────
    # 데이터 수집
    # ──────────────────────────────────────────────────────────

    def _fetch_data(self, market: str, days: int) -> pd.DataFrame:
        """전체 검증 데이터 수집."""
        print(f"  📥 {market} {days}일 전체 데이터 수집 중...")
        all_dfs = []
        candles_per_call = 200
        minutes_per_call = candles_per_call * self.config.CANDLE_UNIT
        total_minutes = days * 24 * 60
        calls_needed = (total_minutes // minutes_per_call) + 2

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
                logger.warning(f"데이터 수집 오류: {e}")
                break

        if not all_dfs:
            raise RuntimeError("데이터를 가져올 수 없습니다.")

        combined = pd.concat(all_dfs).sort_index()
        combined = combined[~combined.index.duplicated(keep="first")]
        combined.columns = ["open", "high", "low", "close", "volume", "value"]
        cutoff = datetime.now() - timedelta(days=days)
        combined = combined[combined.index >= cutoff]
        print(f"  ✅ {len(combined)}개 캔들 ({combined.index[0].date()} ~ {combined.index[-1].date()})")
        return combined

    # ──────────────────────────────────────────────────────────
    # 단일 구간 백테스트 (데이터 직접 전달)
    # ──────────────────────────────────────────────────────────

    def _run_on_df(self, df: pd.DataFrame, config) -> dict:
        """
        주어진 DataFrame으로 백테스트 실행.
        데이터를 재수집하지 않으므로 walk-forward 윈도우마다 재사용 가능.
        """
        df = df.copy()
        df = add_all_indicators(df, config)
        df = df.dropna()

        if len(df) < 20:
            return self._empty_result()

        capital = 1_000_000
        coin_qty = 0.0
        entry_price = 0.0
        highest_price = 0.0
        position = False

        trades = []
        candle_values = {}
        candle_returns = []
        prev_value = capital

        for i in range(1, len(df) - 1):
            row      = df.iloc[i]
            next_row = df.iloc[i + 1]

            if not position:
                if row["atr_pct"] >= config.MIN_VOLATILITY_PCT:
                    signal_result = get_signal_score(row, config)
                    if signal_result["score"] >= config.MIN_SIGNAL_COUNT:
                        trade_amount = min(config.TRADE_AMOUNT_KRW, capital * 0.95)
                        if trade_amount >= 5000:
                            fill_price = next_row["open"] * (1 + SLIPPAGE_RATE)
                            fee = trade_amount * config.FEE_RATE
                            coin_qty      = (trade_amount - fee) / fill_price
                            entry_price   = fill_price
                            highest_price = entry_price
                            capital      -= trade_amount
                            position      = True
                            trades.append({"type": "BUY"})
            else:
                highest_price = max(highest_price, row["close"])
                sell_result = get_sell_signal(row, config, entry_price, highest_price)
                if sell_result["should_sell"]:
                    fill_price  = next_row["open"] * (1 - SLIPPAGE_RATE)
                    sell_value  = coin_qty * fill_price
                    fee         = sell_value * config.FEE_RATE
                    net_revenue = sell_value - fee
                    buy_cost    = entry_price * coin_qty * (1 + config.FEE_RATE)
                    pnl         = net_revenue - buy_cost
                    pnl_pct     = pnl / buy_cost * 100
                    capital    += net_revenue
                    position    = False
                    trades.append({"type": "SELL", "pnl_krw": pnl, "pnl_pct": pnl_pct})
                    coin_qty = 0.0

            current_value = capital + (coin_qty * row["close"] if position else 0)
            candle_values[row.name] = current_value
            ret = (current_value - prev_value) / prev_value if prev_value > 0 else 0.0
            candle_returns.append(ret)
            prev_value = current_value

        # 마지막 강제 청산
        if position and len(df) > 0:
            last = df.iloc[-1]
            fill_price  = last["close"] * (1 - SLIPPAGE_RATE)
            sell_value  = coin_qty * fill_price
            fee         = sell_value * config.FEE_RATE
            net_revenue = sell_value - fee
            buy_cost    = entry_price * coin_qty * (1 + config.FEE_RATE)
            pnl         = net_revenue - buy_cost
            pnl_pct     = pnl / buy_cost * 100
            capital    += net_revenue
            trades.append({"type": "SELL", "pnl_krw": pnl, "pnl_pct": pnl_pct})

        sell_trades = [t for t in trades if t["type"] == "SELL"]
        total_trades = len(sell_trades)
        winning = [t for t in sell_trades if t["pnl_krw"] >= 0]
        win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0.0
        total_return = (capital - 1_000_000) / 1_000_000 * 100

        values = list(candle_values.values())
        mdd = self.bt._calc_mdd(values)
        sharpe = self.bt._calc_sharpe(candle_returns)

        return {
            "total_return_pct": total_return,
            "win_rate_pct":     win_rate,
            "total_trades":     total_trades,
            "max_drawdown_pct": mdd,
            "sharpe_ratio":     sharpe,
        }

    @staticmethod
    def _empty_result() -> dict:
        return {
            "total_return_pct": 0.0,
            "win_rate_pct":     0.0,
            "total_trades":     0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio":     0.0,
        }

    # ──────────────────────────────────────────────────────────
    # Walk-Forward 메인 실행
    # ──────────────────────────────────────────────────────────

    def run(
        self,
        market: str = None,
        total_days: int = 180,
        n_windows: int = 4,
        is_ratio: float = 0.7,
    ) -> dict:
        """
        Walk-Forward 검증 실행.

        Args:
            total_days : 전체 데이터 기간 (일)
            n_windows  : 분할 윈도우 수
            is_ratio   : 훈련 구간 비율 (0.7 → 70% IS / 30% OOS)
        """
        if market is None:
            market = self.config.MARKET

        print(f"\n{'='*60}")
        print(f"  🔄 Walk-Forward 검증 | {market}")
        print(f"  전체 기간: {total_days}일 | 윈도우: {n_windows}개")
        print(f"  훈련(IS) : 검증(OOS) = {int(is_ratio*100)} : {int((1-is_ratio)*100)}")
        print(f"{'='*60}")

        raw_df = self._fetch_data(market, total_days)
        window_size = len(raw_df) // n_windows
        is_size     = int(window_size * is_ratio)

        window_results = []
        oos_values_concat = {}   # OOS 구간 포트폴리오 가치 (이어붙이기용)

        for w in range(n_windows):
            start = w * window_size
            end   = min(start + window_size, len(raw_df))

            is_df  = raw_df.iloc[start : start + is_size]
            oos_df = raw_df.iloc[start + is_size : end]

            if len(is_df) < 100 or len(oos_df) < 30:
                logger.warning(f"윈도우 {w+1}: 데이터 부족 → 건너뜀")
                continue

            is_days  = (is_df.index[-1]  - is_df.index[0]).days
            oos_days = (oos_df.index[-1] - oos_df.index[0]).days

            print(f"\n  ── 윈도우 {w+1}/{n_windows} ──")
            print(f"    IS  {is_df.index[0].date()} ~ {is_df.index[-1].date()} ({is_days}일, {len(is_df)}캔들)")
            print(f"    OOS {oos_df.index[0].date()} ~ {oos_df.index[-1].date()} ({oos_days}일, {len(oos_df)}캔들)")

            is_result  = self._run_on_df(is_df,  self.config)
            oos_result = self._run_on_df(oos_df, self.config)

            print(f"    IS  → 수익률: {is_result['total_return_pct']:>+.2f}%  승률: {is_result['win_rate_pct']:.1f}%  샤프: {is_result['sharpe_ratio']:.3f}")
            print(f"    OOS → 수익률: {oos_result['total_return_pct']:>+.2f}%  승률: {oos_result['win_rate_pct']:.1f}%  샤프: {oos_result['sharpe_ratio']:.3f}")

            window_results.append({
                "window":     w + 1,
                "is_period":  f"{is_df.index[0].date()} ~ {is_df.index[-1].date()}",
                "oos_period": f"{oos_df.index[0].date()} ~ {oos_df.index[-1].date()}",
                "is":         is_result,
                "oos":        oos_result,
            })

        # ── 파라미터 민감도 테스트 (MIN_SIGNAL_COUNT 1~5) ──
        sensitivity = self._sensitivity_test(raw_df)

        # ── 요약 출력 ──
        summary = self._print_summary(window_results, sensitivity)

        return {
            "market":           market,
            "total_days":       total_days,
            "n_windows":        n_windows,
            "windows":          window_results,
            "sensitivity":      sensitivity,
            "degradation_pct":  summary.get("degradation_pct", 0.0),
            "grade":            summary.get("grade", ""),
        }

    # ──────────────────────────────────────────────────────────
    # 파라미터 민감도 테스트
    # ──────────────────────────────────────────────────────────

    def _sensitivity_test(self, df: pd.DataFrame) -> dict:
        """
        MIN_SIGNAL_COUNT를 1~5로 바꾸며 OOS 성능 변화 측정.
        전체 데이터의 뒤 30%를 OOS로 고정해 테스트.
        """
        print(f"\n  ── 파라미터 민감도 테스트 (MIN_SIGNAL_COUNT 1~5) ──")
        oos_start = int(len(df) * 0.7)
        oos_df    = df.iloc[oos_start:]

        results = {}
        for score in range(1, 6):
            cfg_copy = _copy_config(self.config, {"MIN_SIGNAL_COUNT": score})
            r = self._run_on_df(oos_df, cfg_copy)
            results[score] = r
            marker = " ◀ 현재" if score == self.config.MIN_SIGNAL_COUNT else ""
            print(
                f"    MIN_SIGNAL={score}: 수익률 {r['total_return_pct']:>+.2f}%  "
                f"승률 {r['win_rate_pct']:.1f}%  "
                f"거래수 {r['total_trades']}회  "
                f"샤프 {r['sharpe_ratio']:.3f}"
                f"{marker}"
            )
        return results

    # ──────────────────────────────────────────────────────────
    # 요약 출력
    # ──────────────────────────────────────────────────────────

    def _print_summary(self, window_results: list, sensitivity: dict) -> dict:
        if not window_results:
            print("  ❌ 유효한 윈도우 없음")
            return {}

        print(f"\n{'='*60}")
        print("  📋 Walk-Forward 종합 요약")
        print(f"{'='*60}")
        print(
            f"  {'윈도우':>4}  {'구분':>4}  {'수익률':>8}  "
            f"{'승률':>6}  {'거래수':>5}  {'MDD':>7}  {'샤프':>7}"
        )
        print(f"  {'-'*56}")

        is_returns  = []
        oos_returns = []
        is_sharpes  = []
        oos_sharpes = []

        for wr in window_results:
            w     = wr["window"]
            is_r  = wr["is"]
            oos_r = wr["oos"]

            print(
                f"  W{w:>2}   IS  "
                f"{is_r['total_return_pct']:>+7.2f}%  "
                f"{is_r['win_rate_pct']:>5.1f}%  "
                f"{is_r['total_trades']:>5}회  "
                f"{is_r['max_drawdown_pct']:>+6.2f}%  "
                f"{is_r['sharpe_ratio']:>6.3f}"
            )
            print(
                f"       OOS "
                f"{oos_r['total_return_pct']:>+7.2f}%  "
                f"{oos_r['win_rate_pct']:>5.1f}%  "
                f"{oos_r['total_trades']:>5}회  "
                f"{oos_r['max_drawdown_pct']:>+6.2f}%  "
                f"{oos_r['sharpe_ratio']:>6.3f}"
            )

            is_returns.append(is_r["total_return_pct"])
            oos_returns.append(oos_r["total_return_pct"])
            is_sharpes.append(is_r["sharpe_ratio"])
            oos_sharpes.append(oos_r["sharpe_ratio"])

        print(f"  {'-'*56}")
        avg_is      = float(np.mean(is_returns))
        avg_oos     = float(np.mean(oos_returns))
        avg_is_sh   = float(np.mean(is_sharpes))
        avg_oos_sh  = float(np.mean(oos_sharpes))
        degradation = (avg_is - avg_oos) / abs(avg_is) * 100 if avg_is != 0 else 0.0

        print(f"  평균   IS  {avg_is:>+7.2f}%  샤프: {avg_is_sh:.3f}")
        print(f"  평균   OOS {avg_oos:>+7.2f}%  샤프: {avg_oos_sh:.3f}")
        print(f"\n  성능 저하율 (IS→OOS): {degradation:>+.1f}%")

        if degradation < 30:
            grade = "✅ 우수 — 과적합 위험 낮음"
        elif degradation < 60:
            grade = "⚠️  보통 — 파라미터 재검토 권장"
        else:
            grade = "❌ 위험 — 과적합 가능성 높음"

        print(f"  전략 견고성: {grade}")

        # 민감도 기반 최적 MIN_SIGNAL_COUNT 추천
        if sensitivity:
            best_score = max(sensitivity, key=lambda k: sensitivity[k]["sharpe_ratio"])
            current    = self.config.MIN_SIGNAL_COUNT
            best_ret   = sensitivity[best_score]["total_return_pct"]
            best_sh    = sensitivity[best_score]["sharpe_ratio"]
            print(f"\n  🎯 민감도 분석 최적값: MIN_SIGNAL_COUNT = {best_score}")
            print(f"     OOS 수익률 {best_ret:>+.2f}% | 샤프 {best_sh:.3f}")
            if best_score != current:
                print(f"     (현재 설정: {current} → 변경 권장)")
            else:
                print(f"     (현재 설정과 동일 ✅)")

        print("=" * 60)
        return {"degradation_pct": degradation, "grade": grade}
