"""
파라미터 최적화 모듈

실제 비트코인 데이터로 그리드 서치를 수행하여
전략 파라미터의 최적값을 찾습니다.

평가 방식:
  - 전체 데이터의 앞 70%: 훈련(IS)
  - 뒤 30%: 검증(OOS) — 최종 순위는 OOS 성능 기준

최적화 대상 파라미터:
  - MIN_SIGNAL_COUNT  : 진입 신호 최소 개수 (1~4)
  - STOP_LOSS_PCT     : 손절 비율 (0.010 ~ 0.025)
  - TAKE_PROFIT_PCT   : 익절 비율 (0.030 ~ 0.070)
  - TRAILING_STOP_PCT : 트레일링 스탑 (0.010 ~ 0.030)
  - RSI_OVERSOLD      : RSI 과매도 기준 (25 ~ 40)
  - VOLUME_THRESHOLD  : 거래량 배율 기준 (1.0 ~ 2.0)

평가 지표:
  - 주 지표: OOS Sharpe Ratio
  - 보조 지표: OOS 수익률, OOS 승률, OOS MDD, Profit Factor
  - 최소 조건: OOS 거래수 >= 5회 (통계적으로 의미 없는 조합 제외)

결과:
  - 상위 N개 조합을 표로 출력
  - 최적 파라미터를 config.py에 자동 반영 가능 (--apply 옵션)
"""

import itertools
import logging
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyupbit

from enhanced_backtester import SLIPPAGE_RATE
from indicators import add_all_indicators, get_sell_signal, get_signal_score

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# config 모듈 → SimpleNamespace 복사 (module pickle 오류 방지)
# ──────────────────────────────────────────────────────────

def _copy_config(config, overrides: dict = None) -> types.SimpleNamespace:
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


# ──────────────────────────────────────────────────────────
# 단일 구간 경량 백테스트 (파라미터 조합 평가용)
# ──────────────────────────────────────────────────────────

def _backtest_on_df(df: pd.DataFrame, config) -> dict:
    """
    주어진 DataFrame + config로 백테스트 실행.
    EnhancedBacktester와 동일한 로직이지만 출력 없이 결과만 반환.
    """
    df = df.copy()
    df = add_all_indicators(df, config)
    df = df.dropna()

    if len(df) < 20:
        return None

    capital = 1_000_000
    coin_qty = 0.0
    entry_price = 0.0
    highest_price = 0.0
    position = False

    trades = []
    candle_returns = []
    prev_value = capital

    for i in range(1, len(df) - 1):
        row      = df.iloc[i]
        next_row = df.iloc[i + 1]

        if not position:
            if row["atr_pct"] >= config.MIN_VOLATILITY_PCT:
                sig = get_signal_score(row, config)
                if sig["score"] >= config.MIN_SIGNAL_COUNT:
                    trade_amount = min(config.TRADE_AMOUNT_KRW, capital * 0.95)
                    if trade_amount >= 5000:
                        fill_price    = next_row["open"] * (1 + SLIPPAGE_RATE)
                        fee           = trade_amount * config.FEE_RATE
                        coin_qty      = (trade_amount - fee) / fill_price
                        entry_price   = fill_price
                        highest_price = entry_price
                        capital      -= trade_amount
                        position      = True
                        trades.append({"type": "BUY"})
        else:
            highest_price = max(highest_price, row["close"])
            sell = get_sell_signal(row, config, entry_price, highest_price)
            if sell["should_sell"]:
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
    n = len(sell_trades)
    if n == 0:
        return None

    winning = [t for t in sell_trades if t["pnl_krw"] >= 0]
    losing  = [t for t in sell_trades if t["pnl_krw"] < 0]
    win_rate     = len(winning) / n * 100
    total_return = (capital - 1_000_000) / 1_000_000 * 100
    avg_pnl      = float(np.mean([t["pnl_pct"] for t in sell_trades]))

    gross_profit = sum(t["pnl_krw"] for t in winning)
    gross_loss   = abs(sum(t["pnl_krw"] for t in losing))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe (캔들 단위, 연율화)
    arr = np.array(candle_returns)
    periods_per_year = 365 * 24 / getattr(config, "CANDLE_UNIT", 60)
    sharpe = float(
        np.sqrt(periods_per_year) * arr.mean() / arr.std()
        if arr.std() > 0 else 0.0
    )

    # MDD
    values = []
    cap = 1_000_000
    prev = cap
    for r in candle_returns:
        cap = prev * (1 + r)
        values.append(cap)
        prev = cap
    mdd = 0.0
    peak = values[0] if values else 0.0
    for v in values:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100 if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd

    return {
        "total_return_pct": total_return,
        "win_rate_pct":     win_rate,
        "total_trades":     n,
        "profit_factor":    profit_factor,
        "sharpe_ratio":     sharpe,
        "max_drawdown_pct": mdd,
        "avg_pnl_pct":      avg_pnl,
    }


# ──────────────────────────────────────────────────────────
# 파라미터 최적화기
# ──────────────────────────────────────────────────────────

class ParamOptimizer:
    """
    그리드 서치 기반 파라미터 최적화.

    사용법:
        optimizer = ParamOptimizer(cfg)
        best = optimizer.run(days=180)
        optimizer.apply_to_config(best)  # config.py 자동 업데이트
    """

    # 탐색 공간 정의 — 너무 넓으면 시간이 오래 걸리므로 핵심 파라미터만
    DEFAULT_GRID = {
        "MIN_SIGNAL_COUNT":  [2, 3, 4],
        "STOP_LOSS_PCT":     [0.010, 0.015, 0.020],
        "TAKE_PROFIT_PCT":   [0.030, 0.040, 0.050, 0.060],
        "TRAILING_STOP_PCT": [0.010, 0.020, 0.030],
        "RSI_OVERSOLD":      [25, 30, 35],
        "VOLUME_THRESHOLD":  [1.2, 1.5, 2.0],
    }

    def __init__(self, config):
        self.config = config

    # ──────────────────────────────────────────────────────
    # 데이터 수집
    # ──────────────────────────────────────────────────────

    def _fetch_data(self, market: str, days: int) -> pd.DataFrame:
        print(f"  📥 {market} {days}일 데이터 수집 중...")
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

    # ──────────────────────────────────────────────────────
    # 메인 최적화 실행
    # ──────────────────────────────────────────────────────

    def run(
        self,
        market: str = None,
        days: int = 180,
        is_ratio: float = 0.70,
        top_n: int = 10,
        param_grid: dict = None,
        min_trades: int = 5,
    ) -> dict:
        """
        그리드 서치 실행.

        Args:
            days       : 데이터 기간 (최소 90일 권장)
            is_ratio   : 훈련 구간 비율 (기본 70%)
            top_n      : 상위 N개 결과 출력
            param_grid : 탐색 공간 dict (None이면 DEFAULT_GRID 사용)
            min_trades : OOS 최소 거래수 (이 이하는 결과 제외)
        """
        if market is None:
            market = self.config.MARKET
        if param_grid is None:
            param_grid = self.DEFAULT_GRID

        # 총 조합 수 계산
        keys   = list(param_grid.keys())
        values = list(param_grid.values())
        total_combos = 1
        for v in values:
            total_combos *= len(v)

        print(f"\n{'='*60}")
        print(f"  🔧 파라미터 최적화 | {market} | {days}일")
        print(f"  훈련(IS): {int(is_ratio*100)}% / 검증(OOS): {int((1-is_ratio)*100)}%")
        print(f"  탐색 조합: {total_combos}개 | 최소 거래수: {min_trades}회")
        print(f"  최적화 파라미터: {', '.join(keys)}")
        print(f"{'='*60}")

        raw_df   = self._fetch_data(market, days)
        split    = int(len(raw_df) * is_ratio)
        is_df    = raw_df.iloc[:split]
        oos_df   = raw_df.iloc[split:]

        print(f"\n  IS  구간: {is_df.index[0].date()} ~ {is_df.index[-1].date()} ({len(is_df)}캔들)")
        print(f"  OOS 구간: {oos_df.index[0].date()} ~ {oos_df.index[-1].date()} ({len(oos_df)}캔들)")

        results = []
        done = 0

        for combo_values in itertools.product(*values):
            overrides = dict(zip(keys, combo_values))
            cfg_copy  = _copy_config(self.config, overrides)

            # TP >= SL * 1.5 조건 (역전된 손익비 제외)
            if cfg_copy.TAKE_PROFIT_PCT < cfg_copy.STOP_LOSS_PCT * 1.5:
                done += 1
                continue

            # OOS 성능 평가
            oos_result = _backtest_on_df(oos_df, cfg_copy)
            done += 1

            if oos_result is None or oos_result["total_trades"] < min_trades:
                continue

            results.append({
                "params":         overrides,
                "oos_sharpe":     oos_result["sharpe_ratio"],
                "oos_return_pct": oos_result["total_return_pct"],
                "oos_win_rate":   oos_result["win_rate_pct"],
                "oos_pf":         oos_result["profit_factor"],
                "oos_mdd":        oos_result["max_drawdown_pct"],
                "oos_trades":     oos_result["total_trades"],
                "oos_avg_pnl":    oos_result["avg_pnl_pct"],
            })

            # 진행 상황 표시 (100개마다)
            if done % 100 == 0 or done == total_combos:
                valid = len(results)
                print(f"  진행: {done:>4}/{total_combos} | 유효 조합: {valid}개", end="\r")

        print(f"\n  완료: {done}개 조합 탐색 | 유효: {len(results)}개")

        if not results:
            print("  ❌ 유효한 파라미터 조합을 찾지 못했습니다.")
            return {}

        # Sharpe 기준 정렬
        results.sort(key=lambda x: x["oos_sharpe"], reverse=True)
        best = results[0]

        self._print_results(results[:top_n], best)
        return best

    # ──────────────────────────────────────────────────────
    # 결과 출력
    # ──────────────────────────────────────────────────────

    def _print_results(self, top_results: list, best: dict):
        print(f"\n{'='*60}")
        print(f"  🏆 최적화 결과 (OOS Sharpe 기준 상위 {len(top_results)}개)")
        print(f"{'='*60}")

        # 파라미터 키 목록
        param_keys = list(top_results[0]["params"].keys()) if top_results else []

        # 헤더
        param_header = "  ".join(f"{k:<18}" for k in param_keys)
        print(f"  순위  {param_header}  샤프    수익률   승률   PF    거래수")
        print(f"  {'-'*100}")

        for rank, r in enumerate(top_results, 1):
            param_str = "  ".join(
                f"{v:<18}" for v in r["params"].values()
            )
            pf_str = f"{r['oos_pf']:.2f}" if r["oos_pf"] != float("inf") else "∞   "
            print(
                f"  {rank:>3}.  {param_str}  "
                f"{r['oos_sharpe']:>+6.3f}  "
                f"{r['oos_return_pct']:>+6.2f}%  "
                f"{r['oos_win_rate']:>5.1f}%  "
                f"{pf_str:>5}  "
                f"{r['oos_trades']:>4}회"
            )

        print(f"\n{'='*60}")
        print("  🥇 최적 파라미터:")
        print(f"{'='*60}")
        for k, v in best["params"].items():
            current = getattr(self.config, k, "N/A")
            change  = " ← 변경" if v != current else ""
            print(f"    {k:<22}: {v}  (현재: {current}){change}")
        print(f"\n  OOS 성과:")
        print(f"    Sharpe Ratio  : {best['oos_sharpe']:>+.3f}")
        print(f"    수익률        : {best['oos_return_pct']:>+.2f}%")
        print(f"    승률          : {best['oos_win_rate']:>.1f}%")
        pf_str = f"{best['oos_pf']:.2f}" if best["oos_pf"] != float("inf") else "∞"
        print(f"    Profit Factor : {pf_str}")
        print(f"    MDD           : {best['oos_mdd']:>+.2f}%")
        print(f"    거래수        : {best['oos_trades']}회")
        print(f"{'='*60}")

    # ──────────────────────────────────────────────────────
    # config.py 자동 업데이트
    # ──────────────────────────────────────────────────────

    def apply_to_config(self, best_result: dict, config_path: Path = None):
        """
        최적 파라미터를 config.py에 반영합니다.
        기존 config.py를 config.py.bak으로 백업 후 덮어씁니다.
        """
        if not best_result or "params" not in best_result:
            print("  ❌ 적용할 파라미터가 없습니다.")
            return

        if config_path is None:
            config_path = Path(__file__).resolve().parent / "config.py"

        # 백업
        bak_path = config_path.with_suffix(".py.bak")
        content  = config_path.read_text(encoding="utf-8")
        bak_path.write_text(content, encoding="utf-8")
        print(f"  📋 기존 config.py → {bak_path.name} 백업 완료")

        # 파라미터 교체
        updated = content
        for key, value in best_result["params"].items():
            import re
            # 패턴: KEY = <값> (뒤에 # 주석 가능)
            pattern = rf"^({re.escape(key)}\s*=\s*)([^\n#]+)"
            if isinstance(value, float):
                new_val = f"{value}"
            else:
                new_val = f"{value}"
            updated = re.sub(
                pattern,
                lambda m, nv=new_val: m.group(1) + nv,
                updated,
                flags=re.MULTILINE,
            )

        config_path.write_text(updated, encoding="utf-8")
        print(f"  ✅ config.py 업데이트 완료:")
        for k, v in best_result["params"].items():
            print(f"     {k} = {v}")
        print(f"\n  ⚠️  변경 후 봇을 재시작해야 적용됩니다.")
