"""
신호별 예측 정확도 검증 모듈

각 지표 신호(RSI, MACD, 볼린저, EMA, 거래량)가 발생했을 때
N캔들 후 실제 가격이 올랐는지를 측정해 아래 지표를 계산합니다:

  - Accuracy   : (TP + TN) / 전체
  - Precision  : TP / (TP + FP)  — 신호 발생 시 실제로 상승한 비율
  - Recall     : TP / (TP + FN)  — 실제 상승 중 신호가 잡은 비율
  - F1         : 2 * P * R / (P + R)
  - Edge       : 신호 발생 시 평균 수익률 - 전체 평균 수익률

여러 예측 기간(horizons)에 대해 반복 계산하며,
복합 신호(MIN_SIGNAL_COUNT↑) 조건별 성능도 분석합니다.
"""

import logging
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyupbit

from indicators import add_all_indicators, get_signal_score

logger = logging.getLogger(__name__)


class SignalValidator:
    def __init__(self, config):
        self.config = config

    # ──────────────────────────────────────────────────────────
    # 데이터 수집
    # ──────────────────────────────────────────────────────────

    def fetch_data(self, market: str, days: int = 180) -> pd.DataFrame:
        """검증용 과거 데이터 수집 (길수록 통계 신뢰도 향상)."""
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
        print(f"  ✅ {len(combined)}개 캔들 수집 완료 ({combined.index[0].date()} ~ {combined.index[-1].date()})")
        return combined

    # ──────────────────────────────────────────────────────────
    # 분류 지표 계산
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _calc_metrics(tp: int, fp: int, tn: int, fn: int) -> dict:
        total = tp + fp + tn + fn
        precision = tp / (tp + fp) * 100 if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) * 100 if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0 else 0.0
        )
        accuracy = (tp + tn) / total * 100 if total > 0 else 0.0
        return {
            "precision": precision,
            "recall":    recall,
            "f1":        f1 / 100,   # 0~1 스케일
            "accuracy":  accuracy,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        }

    # ──────────────────────────────────────────────────────────
    # 메인 검증 실행
    # ──────────────────────────────────────────────────────────

    def validate(
        self,
        market: str = None,
        days: int = 180,
        horizons: list = None,
    ) -> dict:
        """
        신호별 정확도 검증.

        Args:
            market   : 검증할 마켓 (기본: config.MARKET)
            days     : 데이터 기간 (길수록 통계 신뢰도 향상, 최소 60일 권장)
            horizons : 예측 기간 (캔들 수 단위)
                       60분봉 기준 기본값 [1, 4, 12, 24] = [1h, 4h, 12h, 24h]
        """
        if market is None:
            market = self.config.MARKET
        if horizons is None:
            horizons = [1, 4, 12, 24]

        print(f"\n{'='*60}")
        print(f"  📊 신호 정확도 검증 | {market} | {days}일")
        print(f"  예측 기간: {[f'{h * self.config.CANDLE_UNIT}분' for h in horizons]}")
        print(f"{'='*60}")

        df = self.fetch_data(market, days)
        df = add_all_indicators(df, self.config)
        df = df.dropna().reset_index()   # datetime 컬럼 보존

        signal_names = ["rsi", "macd", "bollinger", "ema", "volume"]
        all_results: dict = {}

        for horizon in horizons:
            horizon_key = f"{horizon * self.config.CANDLE_UNIT}min"
            horizon_results: dict = {}
            n = len(df) - horizon

            # ── 개별 신호 분석 ──
            for sig_name in signal_names:
                tp = fp = tn = fn = 0
                returns_fired    = []
                returns_all      = []

                for i in range(n):
                    row        = df.iloc[i]
                    future_row = df.iloc[i + horizon]

                    signal_result = get_signal_score(row, self.config)
                    sig_fired  = bool(signal_result["signals"].get(sig_name, False))
                    future_ret = (future_row["close"] - row["close"]) / row["close"]
                    actual_up  = future_ret > 0

                    returns_all.append(future_ret)
                    if sig_fired:
                        returns_fired.append(future_ret)
                        if actual_up:
                            tp += 1
                        else:
                            fp += 1
                    else:
                        if actual_up:
                            fn += 1
                        else:
                            tn += 1

                if not returns_fired:
                    horizon_results[sig_name] = {"count": 0}
                    continue

                metrics = self._calc_metrics(tp, fp, tn, fn)
                avg_ret_fired    = float(np.mean(returns_fired)) * 100
                avg_ret_baseline = float(np.mean(returns_all))   * 100

                horizon_results[sig_name] = {
                    "count":                 len(returns_fired),
                    "accuracy":              metrics["accuracy"],
                    "precision":             metrics["precision"],
                    "recall":                metrics["recall"],
                    "f1":                    metrics["f1"],
                    "avg_return_when_fired": avg_ret_fired,
                    "avg_return_baseline":   avg_ret_baseline,
                    "edge":                  avg_ret_fired - avg_ret_baseline,
                    "tp": tp, "fp": fp, "tn": tn, "fn": fn,
                }

            # ── 복합 신호 (점수별) 분석 ──
            combo_records = []
            for i in range(n):
                row        = df.iloc[i]
                future_row = df.iloc[i + horizon]
                signal_result = get_signal_score(row, self.config)
                future_ret = (future_row["close"] - row["close"]) / row["close"]
                combo_records.append({
                    "score":      signal_result["score"],
                    "actual_up":  future_ret > 0,
                    "future_ret": future_ret,
                })

            combo_results: dict = {}
            for min_score in range(1, 6):
                fired     = [r for r in combo_records if r["score"] >= min_score]
                not_fired = [r for r in combo_records if r["score"] <  min_score]
                if not fired:
                    combo_results[f"score_{min_score}+"] = {"count": 0}
                    continue

                tp = sum(1 for r in fired     if     r["actual_up"])
                fp = sum(1 for r in fired     if not r["actual_up"])
                fn = sum(1 for r in not_fired if     r["actual_up"])
                tn = sum(1 for r in not_fired if not r["actual_up"])

                metrics  = self._calc_metrics(tp, fp, tn, fn)
                avg_ret  = float(np.mean([r["future_ret"] for r in fired])) * 100
                baseline = float(np.mean([r["future_ret"] for r in combo_records])) * 100

                combo_results[f"score_{min_score}+"] = {
                    "count":       len(fired),
                    "accuracy":    metrics["accuracy"],
                    "precision":   metrics["precision"],
                    "recall":      metrics["recall"],
                    "f1":          metrics["f1"],
                    "avg_return":  avg_ret,
                    "edge":        avg_ret - baseline,
                }

            horizon_results["combo"] = combo_results
            all_results[horizon_key] = horizon_results

        self._print_results(all_results, market, horizons)
        return all_results

    # ──────────────────────────────────────────────────────────
    # 결과 출력
    # ──────────────────────────────────────────────────────────

    def _print_results(self, results: dict, market: str, horizons: list):
        signal_kr = {
            "rsi":       "RSI    ",
            "macd":      "MACD   ",
            "bollinger": "볼린저 ",
            "ema":       "EMA    ",
            "volume":    "거래량 ",
        }

        for horizon in horizons:
            horizon_key = f"{horizon * self.config.CANDLE_UNIT}min"
            hr = results.get(horizon_key, {})

            print(f"\n  ── 예측 기간: {horizon_key} ──")
            print(
                f"  {'신호':<9} {'발생':>5} {'정확도':>7} "
                f"{'Precision':>10} {'Recall':>7} {'F1':>6} "
                f"{'평균수익':>8} {'엣지':>6}"
            )
            print(f"  {'-'*64}")

            for sig, kr in signal_kr.items():
                s = hr.get(sig, {})
                if not s or s.get("count", 0) == 0:
                    print(f"  {kr}  신호 없음")
                    continue
                print(
                    f"  {kr} "
                    f"{s['count']:>5}회 "
                    f"{s['accuracy']:>6.1f}% "
                    f"{s['precision']:>9.1f}% "
                    f"{s['recall']:>6.1f}% "
                    f"{s['f1']:>5.3f} "
                    f"{s['avg_return_when_fired']:>+7.2f}% "
                    f"{s['edge']:>+5.2f}%"
                )

            # 복합 신호
            combo = hr.get("combo", {})
            print(f"\n  복합 신호 조건별 성능:")
            print(
                f"  {'조건':<11} {'발생':>5} {'정확도':>7} "
                f"{'Precision':>10} {'F1':>6} {'평균수익':>8} {'엣지':>6}"
            )
            print(f"  {'-'*57}")
            for key, s in combo.items():
                if s.get("count", 0) == 0:
                    continue
                # 현재 설정과 일치하는 조건 강조
                marker = " ◀ 현재 설정" if key == f"score_{self.config.MIN_SIGNAL_COUNT}+" else ""
                print(
                    f"  {key:<11} "
                    f"{s['count']:>5}회 "
                    f"{s['accuracy']:>6.1f}% "
                    f"{s['precision']:>9.1f}% "
                    f"{s['f1']:>5.3f} "
                    f"{s['avg_return']:>+7.2f}% "
                    f"{s['edge']:>+5.2f}%"
                    f"{marker}"
                )

        # 권장사항 출력
        self._print_recommendations(results, horizons)

    def _print_recommendations(self, results: dict, horizons: list):
        """검증 결과를 바탕으로 파라미터 조정 권장사항 출력."""
        # 1h 기준으로 분석
        key_1h = f"{self.config.CANDLE_UNIT}min"
        hr = results.get(key_1h, {})
        if not hr:
            return

        print(f"\n{'='*60}")
        print("  💡 검증 결과 기반 권장사항")
        print(f"{'='*60}")

        signal_kr = {
            "rsi": "RSI", "macd": "MACD",
            "bollinger": "볼린저", "ema": "EMA", "volume": "거래량",
        }

        # 낮은 F1 신호 경고
        low_f1_signals = []
        for sig, kr in signal_kr.items():
            s = hr.get(sig, {})
            if s.get("count", 0) > 0 and s.get("f1", 0) < 0.5:
                low_f1_signals.append(kr)

        if low_f1_signals:
            print(f"\n  ⚠️  F1 < 0.5 (예측력 낮음): {', '.join(low_f1_signals)}")
            print("      → MIN_SIGNAL_COUNT 상향 또는 해당 신호 기준 강화 권장")

        # 음수 엣지 신호 경고
        neg_edge_signals = []
        for sig, kr in signal_kr.items():
            s = hr.get(sig, {})
            if s.get("count", 0) > 0 and s.get("edge", 0) < 0:
                neg_edge_signals.append(kr)

        if neg_edge_signals:
            print(f"\n  ❌ 음수 엣지 (신호 발생 시 오히려 손해): {', '.join(neg_edge_signals)}")
            print("      → 해당 신호 임계값 재검토 필요")

        # 최적 MIN_SIGNAL_COUNT 추천
        combo = hr.get("combo", {})
        best_score = None
        best_f1 = 0.0
        for key, s in combo.items():
            if s.get("count", 0) >= 10 and s.get("f1", 0) > best_f1:
                best_f1 = s["f1"]
                best_score = key

        if best_score:
            recommended = int(best_score.replace("score_", "").replace("+", ""))
            current     = self.config.MIN_SIGNAL_COUNT
            if recommended != current:
                print(f"\n  🎯 최적 MIN_SIGNAL_COUNT: {recommended} (현재: {current})")
                print(f"      F1={best_f1:.3f} — config.py 수정 권장")
            else:
                print(f"\n  ✅ 현재 MIN_SIGNAL_COUNT({current})가 최적 수준입니다 (F1={best_f1:.3f})")

        print("=" * 60)
