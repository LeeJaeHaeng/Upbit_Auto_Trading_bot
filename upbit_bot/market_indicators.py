"""
시장 환경 지표 모듈 (기술적 지표 외 매개변수)

기술적 지표(RSI, MACD 등) 외에 추가로 분석해야 할 시장 환경 요소들:
1. 공포탐욕 지수 (Fear & Greed Index) — 시장 전체 심리
2. 김치 프리미엄 (바이낸스 vs 업비트 가격 차이) — 한국 시장 과열 여부
3. 호가창 매수/매도 압력 비율 — 단기 수급
4. BTC 도미넌스 변화 — 알트코인 순환 판단
5. 거래량 추세 — 시장 관심도 변화
"""

import logging
import time

import pyupbit
import requests

logger = logging.getLogger(__name__)


class MarketEnvironment:
    """시장 환경 지표를 수집하고 종합 점수를 산출합니다."""

    def __init__(self):
        self._cache = {}
        self._cache_ttl = 300  # 5분 캐시

    def _is_cached(self, key: str) -> bool:
        if key in self._cache:
            ts, _ = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return True
        return False

    def _get_cached(self, key: str):
        return self._cache[key][1]

    def _set_cache(self, key: str, value):
        self._cache[key] = (time.time(), value)

    # ──────────────────────────────────────────
    # 1. 공포탐욕 지수
    # ──────────────────────────────────────────

    def get_fear_greed_index(self) -> dict:
        """
        Alternative.me 공포탐욕 지수를 가져옵니다.
        0 = 극도의 공포, 100 = 극도의 탐욕

        반환: {'value': int, 'classification': str, 'signal': str}
        """
        if self._is_cached("fgi"):
            return self._get_cached("fgi")

        try:
            resp = requests.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=10,
            )
            data = resp.json()["data"][0]
            value = int(data["value"])
            classification = data["value_classification"]

            # 투자 신호 해석
            if value <= 20:
                signal = "극도의 공포 → 매수 기회 (역발상)"
            elif value <= 35:
                signal = "공포 → 매수 유리"
            elif value <= 55:
                signal = "중립 → 관망"
            elif value <= 75:
                signal = "탐욕 → 주의"
            else:
                signal = "극도의 탐욕 → 매수 자제 (과열)"

            result = {
                "value": value,
                "classification": classification,
                "signal": signal,
            }
            self._set_cache("fgi", result)
            return result
        except Exception as e:
            logger.warning(f"공포탐욕 지수 조회 실패: {e}")
            return {"value": 50, "classification": "Neutral", "signal": "조회 실패"}

    # ──────────────────────────────────────────
    # 2. 김치 프리미엄
    # ──────────────────────────────────────────

    def get_kimchi_premium(self, market: str = "KRW-BTC") -> dict:
        """
        업비트 vs 바이낸스 가격 차이 (김치 프리미엄).
        양수 = 업비트가 더 비쌈 (국내 과열).

        반환: {'premium_pct': float, 'upbit_price': float, 'binance_price_krw': float, 'signal': str}
        """
        if self._is_cached(f"kimchi_{market}"):
            return self._get_cached(f"kimchi_{market}")

        try:
            # 업비트 가격
            upbit_price = pyupbit.get_current_price(market)
            if upbit_price is None:
                raise ValueError("업비트 가격 조회 실패")

            # 바이낸스 가격 (USDT)
            coin = market.split("-")[1]
            binance_symbol = f"{coin}USDT"
            resp = requests.get(
                f"https://api.binance.com/api/v3/ticker/price?symbol={binance_symbol}",
                timeout=10,
            )
            binance_usdt = float(resp.json()["price"])

            # 환율 (USD/KRW)
            resp2 = requests.get(
                "https://api.exchangerate-api.com/v4/latest/USD",
                timeout=10,
            )
            usd_krw = resp2.json()["rates"]["KRW"]

            binance_krw = binance_usdt * usd_krw
            premium_pct = (upbit_price - binance_krw) / binance_krw * 100

            if premium_pct > 5:
                signal = "김프 높음 → 매수 자제 (과열)"
            elif premium_pct > 2:
                signal = "김프 주의 → 신중한 매수"
            elif premium_pct < -1:
                signal = "역프 → 매수 유리"
            else:
                signal = "김프 정상 범위"

            result = {
                "premium_pct": premium_pct,
                "upbit_price": upbit_price,
                "binance_price_krw": binance_krw,
                "usd_krw": usd_krw,
                "signal": signal,
            }
            self._set_cache(f"kimchi_{market}", result)
            return result
        except Exception as e:
            logger.warning(f"김치 프리미엄 조회 실패: {e}")
            return {"premium_pct": 0, "upbit_price": 0, "binance_price_krw": 0, "signal": "조회 실패"}

    # ──────────────────────────────────────────
    # 3. 호가창 매수/매도 압력
    # ──────────────────────────────────────────

    def get_orderbook_pressure(self, market: str) -> dict:
        """
        호가창의 매수/매도 물량 비율을 분석합니다.
        bid_ratio > 0.6 → 매수 우위, < 0.4 → 매도 우위

        반환: {'bid_ratio': float, 'bid_total': float, 'ask_total': float, 'signal': str}
        """
        try:
            ob = pyupbit.get_orderbook(market)
            if not ob or len(ob) == 0:
                return {"bid_ratio": 0.5, "signal": "조회 실패"}

            orderbook = ob[0]
            units = orderbook.get("orderbook_units", [])
            bid_total = sum(u["bid_size"] for u in units)
            ask_total = sum(u["ask_size"] for u in units)
            total = bid_total + ask_total

            bid_ratio = bid_total / total if total > 0 else 0.5

            if bid_ratio > 0.65:
                signal = "매수세 강함 → 상승 압력"
            elif bid_ratio > 0.55:
                signal = "매수 우위"
            elif bid_ratio < 0.35:
                signal = "매도세 강함 → 하락 압력"
            elif bid_ratio < 0.45:
                signal = "매도 우위"
            else:
                signal = "매수/매도 균형"

            return {
                "bid_ratio": bid_ratio,
                "bid_total": bid_total,
                "ask_total": ask_total,
                "signal": signal,
            }
        except Exception as e:
            logger.warning(f"호가창 분석 실패: {e}")
            return {"bid_ratio": 0.5, "signal": "조회 실패"}

    # ──────────────────────────────────────────
    # 4. 거래량 추세 (24시간 대비)
    # ──────────────────────────────────────────

    def get_volume_trend(self, market: str) -> dict:
        """
        최근 거래량 추세를 분석합니다.
        3일 평균 대비 오늘 거래량 비율.

        반환: {'ratio': float, 'today_volume': float, 'avg_3d_volume': float, 'signal': str}
        """
        try:
            df = pyupbit.get_ohlcv(market, interval="day", count=4)
            if df is None or len(df) < 4:
                return {"ratio": 1.0, "signal": "데이터 부족"}

            avg_3d = df["volume"].iloc[:3].mean()
            today = df["volume"].iloc[-1]
            ratio = today / avg_3d if avg_3d > 0 else 1.0

            if ratio > 2.0:
                signal = "거래량 급증 → 변동성 주의"
            elif ratio > 1.3:
                signal = "거래량 증가 → 시장 관심 상승"
            elif ratio < 0.5:
                signal = "거래량 급감 → 관심 저하"
            else:
                signal = "거래량 정상"

            return {
                "ratio": ratio,
                "today_volume": today,
                "avg_3d_volume": avg_3d,
                "signal": signal,
            }
        except Exception as e:
            logger.warning(f"거래량 추세 분석 실패: {e}")
            return {"ratio": 1.0, "signal": "조회 실패"}

    # ──────────────────────────────────────────
    # 종합 시장 점수
    # ──────────────────────────────────────────

    def get_market_score(self, market: str) -> dict:
        """
        모든 시장 환경 지표를 종합하여 -100 ~ +100 점수를 산출합니다.
        양수: 매수 유리, 음수: 매수 불리

        반환: {'score': int, 'details': dict, 'recommendation': str}
        """
        fgi = self.get_fear_greed_index()
        kimchi = self.get_kimchi_premium(market)
        orderbook = self.get_orderbook_pressure(market)
        volume = self.get_volume_trend(market)

        score = 0

        # 공포탐욕 (극도의 공포가 역발상 매수 기회)
        fgi_val = fgi["value"]
        if fgi_val <= 20:
            score += 30        # 극도의 공포 → 강한 매수 신호
        elif fgi_val <= 35:
            score += 15        # 공포 → 매수 유리
        elif fgi_val <= 55:
            score += 0         # 중립
        elif fgi_val <= 75:
            score -= 15        # 탐욕 → 주의
        else:
            score -= 30        # 극도의 탐욕 → 매수 자제

        # 김치 프리미엄
        premium = kimchi["premium_pct"]
        if premium > 5:
            score -= 25        # 과열
        elif premium > 2:
            score -= 10
        elif premium < -1:
            score += 15        # 역프 → 기회
        else:
            score += 5         # 정상

        # 호가 압력
        bid_ratio = orderbook["bid_ratio"]
        score += int((bid_ratio - 0.5) * 60)  # ±30 범위

        # 거래량 추세
        vol_ratio = volume["ratio"]
        if vol_ratio > 2.0:
            score += 5         # 관심은 높지만 변동성 위험도 있음
        elif vol_ratio > 1.3:
            score += 10
        elif vol_ratio < 0.5:
            score -= 15

        # 점수 클램핑
        score = max(-100, min(100, score))

        if score >= 30:
            recommendation = "매수 유리한 환경"
        elif score >= 10:
            recommendation = "보통 (진입 가능)"
        elif score >= -10:
            recommendation = "중립 (신중히 접근)"
        elif score >= -30:
            recommendation = "매수 불리 (관망 권장)"
        else:
            recommendation = "매수 자제 (시장 과열/위험)"

        return {
            "score": score,
            "recommendation": recommendation,
            "details": {
                "fear_greed": fgi,
                "kimchi_premium": kimchi,
                "orderbook_pressure": orderbook,
                "volume_trend": volume,
            },
        }

    def print_market_environment(self, market: str):
        """시장 환경 요약을 출력합니다."""
        result = self.get_market_score(market)
        d = result["details"]

        print(f"\n  📡 시장 환경 분석 ({market}):")
        print(f"    공포탐욕 지수   : {d['fear_greed']['value']} ({d['fear_greed']['classification']}) — {d['fear_greed']['signal']}")
        print(f"    김치 프리미엄   : {d['kimchi_premium']['premium_pct']:+.2f}% — {d['kimchi_premium']['signal']}")
        print(f"    호가창 매수비율 : {d['orderbook_pressure']['bid_ratio']:.1%} — {d['orderbook_pressure']['signal']}")
        print(f"    거래량 추세     : {d['volume_trend']['ratio']:.2f}x (3일 평균 대비) — {d['volume_trend']['signal']}")
        print(f"    ─── 종합 점수   : {result['score']:+d}/100 → {result['recommendation']}")
