"""
마켓 스캐너 모듈
업비트 KRW 마켓 전체를 스캔하여 진입 기회가 높은 코인을 선별합니다.

선택 기준:
1. 거래량 상위 (유동성 확보 필수)
2. 기술적 신호 점수 (RSI+MACD+BB+EMA+거래량 다중 지표)
3. 변동성 필터 (너무 정체된 코인 제외)
4. 최근 급락 회복 패턴 (반등 가능성)
5. 블랙리스트 코인 제외 (위험 코인)
"""

import time
import logging
import pandas as pd
import pyupbit

from indicators import add_all_indicators, get_signal_score

logger = logging.getLogger(__name__)

# 투자 제외 코인 (극단적 변동성, 유동성 부족, 상장폐지 위험 등)
BLACKLIST = {
    "KRW-LUNA",   # 루나 폭락 전례
    "KRW-LUNC",
    "KRW-LUNA2",
}

# 최소 거래대금 기준 (24시간, 단위: 원)
# 이 이하이면 슬리피지가 커서 불리함
MIN_24H_TRADE_VALUE = 5_000_000_000  # 50억원


class MarketScanner:
    def __init__(self, config):
        self.config = config

    def get_all_krw_markets(self) -> list[str]:
        """업비트 KRW 마켓 목록 전체를 가져옵니다."""
        try:
            tickers = pyupbit.get_tickers(fiat="KRW")
            return [t for t in tickers if t not in BLACKLIST]
        except Exception as e:
            logger.error(f"마켓 목록 조회 오류: {e}")
            return ["KRW-BTC"]  # 폴백

    def get_24h_stats(self, markets: list[str]) -> pd.DataFrame:
        """
        여러 마켓의 24시간 통계를 가져옵니다.
        - 거래대금, 전일 대비 변동률 포함
        """
        try:
            # 현재가 일괄 조회
            if pyupbit.get_current_price(markets) is None:
                return pd.DataFrame()

            rows = []
            # 24시간 OHLCV (일봉 1개)
            for market in markets:
                try:
                    df = pyupbit.get_ohlcv(market, interval="day", count=2)
                    if df is None or len(df) < 2:
                        continue
                    today = df.iloc[-1]
                    yesterday = df.iloc[-2]
                    change_pct = (today["close"] - yesterday["close"]) / yesterday["close"] * 100
                    trade_value = today["value"]  # 거래대금 (원)
                    rows.append({
                        "market": market,
                        "close": today["close"],
                        "change_pct": change_pct,
                        "trade_value_24h": trade_value,
                        "high_24h": today["high"],
                        "low_24h": today["low"],
                        "volume_24h": today["volume"],
                    })
                    time.sleep(0.05)
                except Exception:
                    continue

            return pd.DataFrame(rows)
        except Exception as e:
            logger.error(f"24시간 통계 조회 오류: {e}")
            return pd.DataFrame()

    def score_market(self, market: str) -> dict:
        """
        특정 마켓의 진입 기회 점수를 계산합니다.

        반환: {
            'market': str,
            'signal_score': int,       # 기술적 신호 수 (0~5)
            'opportunity_score': float, # 종합 기회 점수
            'signals': dict,
            'details': dict,
            'current_price': float,
            'atr_pct': float,
        }
        """
        try:
            df = pyupbit.get_ohlcv(
                market,
                interval=f"minute{self.config.CANDLE_UNIT}",
                count=self.config.CANDLE_COUNT,
            )
            if df is None or len(df) < 50:
                return None

            df.columns = ["open", "high", "low", "close", "volume", "value"]
            df = add_all_indicators(df, self.config)
            df = df.dropna()
            if df.empty:
                return None

            row = df.iloc[-1]
            signal_result = get_signal_score(row, self.config)

            # 변동성 점수 (너무 낮으면 0점, 적정 변동성이 높은 점수)
            atr_pct = row["atr_pct"]
            if atr_pct < self.config.MIN_VOLATILITY_PCT:
                volatility_score = 0
            elif atr_pct > 5.0:  # 너무 폭발적이면 위험
                volatility_score = 0.5
            else:
                volatility_score = min(atr_pct / 2.0, 1.0)  # 0~1 정규화

            # 볼린저 밴드 위치 점수 (하단에 가까울수록 매수 기회)
            bb_pct = row["bb_pct"]
            bb_score = max(0, 1 - bb_pct)  # 하단(0)일수록 1점

            # RSI 점수 (과매도일수록 높은 점수)
            rsi = row["rsi"]
            if rsi < 20:
                rsi_score = 1.0
            elif rsi < 30:
                rsi_score = 0.8
            elif rsi < 40:
                rsi_score = 0.5
            else:
                rsi_score = max(0, (70 - rsi) / 70)

            # 종합 기회 점수
            opportunity_score = (
                signal_result["score"] * 2.0   # 신호 수 (최대 10점)
                + volatility_score * 2.0         # 변동성 (최대 2점)
                + bb_score * 1.5                 # 밴드 위치 (최대 1.5점)
                + rsi_score * 1.5                # RSI (최대 1.5점)
            )

            return {
                "market": market,
                "signal_score": signal_result["score"],
                "opportunity_score": opportunity_score,
                "signals": signal_result["signals"],
                "details": signal_result["details"],
                "current_price": row["close"],
                "atr_pct": atr_pct,
                "rsi": rsi,
                "bb_pct": bb_pct,
            }
        except Exception as e:
            logger.debug(f"{market} 스코어 계산 실패: {e}")
            return None

    def scan_and_rank(
        self,
        top_n: int = 5,
        min_trade_value: float = MIN_24H_TRADE_VALUE,
        focus_markets: list[str] = None,
    ) -> list[dict]:
        """
        전체 KRW 마켓을 스캔하여 상위 N개 종목을 반환합니다.

        Args:
            top_n: 반환할 상위 종목 수
            min_trade_value: 최소 24시간 거래대금 (원)
            focus_markets: None이면 전체 스캔, 리스트 제공 시 해당 마켓만 스캔

        Returns:
            기회 점수 순으로 정렬된 마켓 딕셔너리 리스트
        """
        print("\n🔍 마켓 스캔 중...")

        if focus_markets:
            markets = focus_markets
        else:
            all_markets = self.get_all_krw_markets()
            # 거래대금 필터링
            print(f"   전체 마켓 {len(all_markets)}개 → 거래대금 필터 적용 중...")
            stats_df = self.get_24h_stats(all_markets)
            if not stats_df.empty:
                filtered = stats_df[stats_df["trade_value_24h"] >= min_trade_value]
                markets = filtered["market"].tolist()
                print(f"   거래대금 {min_trade_value/1e8:.0f}억원 이상: {len(markets)}개")
            else:
                markets = all_markets[:30]  # 폴백: 상위 30개만

        # 각 마켓 스코어 계산
        results = []
        for i, market in enumerate(markets):
            print(f"   [{i+1}/{len(markets)}] {market} 분석 중...", end="\r")
            score_data = self.score_market(market)
            if score_data and score_data["signal_score"] >= 1:  # 최소 1개 신호 이상
                results.append(score_data)
            time.sleep(0.08)  # API 레이트 리밋

        print()  # 줄바꿈

        # 기회 점수 기준 정렬
        results.sort(key=lambda x: x["opportunity_score"], reverse=True)

        top_results = results[:top_n]
        self._print_scan_results(top_results)
        return top_results

    def select_best_market(
        self,
        min_signal_score: int = None,
        focus_markets: list[str] = None,
    ) -> str:
        """
        가장 좋은 진입 기회의 마켓 하나를 선택합니다.
        신호가 충분하지 않으면 None을 반환합니다 (진입 안 함).
        """
        if min_signal_score is None:
            min_signal_score = self.config.MIN_SIGNAL_COUNT

        candidates = self.scan_and_rank(top_n=10, focus_markets=focus_markets)

        # 신호 점수 조건 충족하는 가장 높은 점수의 마켓 선택
        for candidate in candidates:
            if candidate["signal_score"] >= min_signal_score:
                selected = candidate["market"]
                print(f"\n✅ 선택된 마켓: {selected} (신호점수={candidate['signal_score']}/5, 기회점수={candidate['opportunity_score']:.2f})")
                return selected

        print(f"\n⏸️  현재 진입 조건 충족 마켓 없음 (신호점수 {min_signal_score}개 이상 필요)")
        return None

    def _print_scan_results(self, results: list[dict]):
        if not results:
            print("   스캔 결과 없음")
            return
        print(f"\n  {'마켓':<12} {'기회점수':>8} {'신호':>6} {'RSI':>6} {'ATR%':>6} {'BB%':>6}")
        print("  " + "-" * 55)
        for r in results:
            signals_str = "".join(
                "●" if v else "○"
                for v in r["signals"].values()
            )
            print(
                f"  {r['market']:<12} {r['opportunity_score']:>8.2f} "
                f"{signals_str} "
                f"{r['rsi']:>6.1f} {r['atr_pct']:>6.2f} {r['bb_pct']:>6.2f}"
            )
        print("  (● = 신호 ON | ○ = 신호 OFF | 순서: RSI·MACD·BB·EMA·거래량)")
