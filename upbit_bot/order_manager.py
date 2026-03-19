"""
주문 관리 모듈
지정가 매수/매도 주문 생명주기를 관리합니다.

흐름:
1. 차트 분석 → 최적 진입가(지지선, BB 하단, 매물대 등) 산출
2. 지정가 매수 주문 설정 → 체결 대기
3. 체결 확인 → 즉시 익절/손절 지정가 주문 설정
4. 차트 지속 분석 → 상황에 따라 익절/손절 가격 동적 조정
5. 미체결 시 일정 시간 후 자동 취소
"""

import time
import logging
from datetime import datetime, timedelta

import pyupbit
import pandas as pd


def fmt_price(price: float) -> str:
    """가격을 적절한 자릿수로 포맷합니다 (저가 코인 대응)."""
    if price >= 100:
        return f"{price:,.0f}"
    elif price >= 1:
        return f"{price:,.2f}"
    elif price >= 0.01:
        return f"{price:,.4f}"
    else:
        return f"{price:,.6f}"

from indicators import add_all_indicators

logger = logging.getLogger(__name__)

# 미체결 주문 자동 취소 시간 (분)
ORDER_TIMEOUT_MINUTES = 30


class OrderManager:
    def __init__(self, client, config):
        """
        Args:
            client: UpbitClient 인스턴스
            config: config 모듈
        """
        self.client = client
        self.config = config

        # 활성 주문 추적
        self.active_buy_order = None     # 대기 중인 매수 주문
        self.active_tp_order = None      # 대기 중인 익절 매도 주문
        self.active_sl_order = None      # 대기 중인 손절 매도 주문
        self.buy_order_placed_at = None  # 매수 주문 설정 시각

    # ──────────────────────────────────────────
    # 최적 진입가 계산
    # ──────────────────────────────────────────

    def calculate_optimal_entry_price(self, market: str) -> dict:
        """
        차트를 분석하여 최적 매수 진입가를 계산합니다.

        분석 기준:
        1. 볼린저 밴드 하단 (반등 기대)
        2. 최근 지지선 (직전 저점들)
        3. EMA 지지 (20, 50 EMA)
        4. 현재가 대비 적정 할인율

        반환: {
            'entry_price': float,       # 추천 진입가
            'current_price': float,     # 현재가
            'discount_pct': float,      # 현재가 대비 할인율
            'method': str,              # 산출 방법
            'support_levels': list,     # 지지선 목록
            'tp_price': float,          # 추천 익절가
            'sl_price': float,          # 추천 손절가
        }
        """
        df = pyupbit.get_ohlcv(
            market,
            interval=f"minute{self.config.CANDLE_UNIT}",
            count=self.config.CANDLE_COUNT,
        )
        if df is None or df.empty:
            return None

        df.columns = ["open", "high", "low", "close", "volume", "value"]
        df = add_all_indicators(df, self.config)
        df = df.dropna()
        if df.empty:
            return None

        current_price = df.iloc[-1]["close"]
        row = df.iloc[-1]

        # ── 1. 지지선 찾기 (최근 N개 캔들의 저점 클러스터) ──
        support_levels = self._find_support_levels(df, n_levels=3)

        # ── 2. 볼린저 밴드 하단 ──
        bb_lower = row["bb_lower"]

        # ── 3. EMA 지지 ──
        ema_short = row["ema_short"]
        ema_long = row["ema_long"]

        # ── 4. ATR 기반 적정 할인율 ──
        atr = row["atr"]
        # ATR의 50~100% 아래를 진입가로 설정 (변동성에 비례하게 할인)
        atr_discount = atr * 0.6

        # ── 후보 가격 수집 ──
        candidates = []

        # 볼린저 하단 근처 (약간 위)
        candidates.append(("볼린저밴드 하단", bb_lower * 1.002))

        # EMA 지지선 중 현재가 아래인 것
        if ema_short < current_price:
            candidates.append(("EMA 단기 지지", ema_short))
        if ema_long < current_price:
            candidates.append(("EMA 장기 지지", ema_long))

        # 최근 지지선 중 현재가 아래인 것
        for level in support_levels:
            if level < current_price:
                candidates.append(("지지선", level))

        # ATR 할인 가격
        candidates.append(("ATR 할인", current_price - atr_discount))

        if not candidates:
            # 폴백: 현재가 대비 0.5% 할인
            candidates.append(("기본 할인", current_price * 0.995))

        # ── 최적 진입가 선택 ──
        # 현재가에서 너무 멀면 체결이 안 되므로, 현재가의 0.3~1.5% 아래 범위 내에서 선택
        min_price = current_price * 0.985  # 최대 1.5% 할인
        max_price = current_price * 0.997  # 최소 0.3% 할인

        valid_candidates = [
            (method, price) for method, price in candidates
            if min_price <= price <= max_price
        ]

        if valid_candidates:
            # 가장 높은 가격 선택 (체결 확률 높음)
            method, entry_price = max(valid_candidates, key=lambda x: x[1])
        else:
            # 범위 내에 없으면 현재가 0.5% 할인
            entry_price = current_price * 0.995
            method = "기본 0.5% 할인"

        # ── 업비트 호가 단위 맞추기 ──
        entry_price = self._round_to_tick(entry_price, current_price)

        # ── 익절/손절 가격 설정 ──
        tp_price = self._round_to_tick(
            entry_price * (1 + self.config.TAKE_PROFIT_PCT),
            current_price,
        )
        sl_price = self._round_to_tick(
            entry_price * (1 - self.config.STOP_LOSS_PCT),
            current_price,
        )

        discount_pct = (current_price - entry_price) / current_price * 100

        return {
            "entry_price": entry_price,
            "current_price": current_price,
            "discount_pct": discount_pct,
            "method": method,
            "support_levels": support_levels,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_pct": (tp_price - entry_price) / entry_price * 100,
            "sl_pct": (sl_price - entry_price) / entry_price * 100,
        }

    def _find_support_levels(self, df: pd.DataFrame, n_levels: int = 3) -> list[float]:
        """최근 캔들에서 지지선(로컬 저점)을 찾습니다."""
        lows = df["low"].values
        supports = []
        window = 5

        for i in range(window, len(lows) - window):
            # 양쪽 window개 캔들보다 낮은 저점 = 로컬 저점
            if lows[i] == min(lows[i - window:i + window + 1]):
                supports.append(lows[i])

        # 클러스터링: 비슷한 가격대 합치기 (0.5% 이내)
        if not supports:
            return [df["low"].min()]

        supports.sort(reverse=True)
        clustered = [supports[0]]
        for s in supports[1:]:
            if abs(s - clustered[-1]) / clustered[-1] > 0.005:
                clustered.append(s)
            if len(clustered) >= n_levels:
                break

        return clustered

    def _round_to_tick(self, price: float, reference: float) -> float:
        """
        업비트 호가 단위에 맞춤 (KRW 마켓).
        가격대별 호가 단위가 다름:
        - 2,000,000 이상: 1,000원
        - 1,000,000 이상: 500원
        - 500,000 이상: 100원
        - 100,000 이상: 50원
        - 10,000 이상: 10원
        - 1,000 이상: 5원
        - 100 이상: 1원
        - 10 이상: 0.1원
        - 1 이상: 0.01원
        """
        if reference >= 2_000_000:
            tick = 1000
        elif reference >= 1_000_000:
            tick = 500
        elif reference >= 500_000:
            tick = 100
        elif reference >= 100_000:
            tick = 50
        elif reference >= 10_000:
            tick = 10
        elif reference >= 1_000:
            tick = 5
        elif reference >= 100:
            tick = 1
        elif reference >= 10:
            tick = 0.1
        elif reference >= 1:
            tick = 0.01
        elif reference >= 0.1:
            tick = 0.001
        elif reference >= 0.01:
            tick = 0.0001
        else:
            tick = 0.00001
        rounded = round(price / tick) * tick
        # 부동소수점 정밀도 보정
        return round(rounded, 10)

    # ──────────────────────────────────────────
    # 주문 실행
    # ──────────────────────────────────────────

    def place_limit_buy(self, market: str, price: float, amount_krw: float) -> dict:
        """
        지정가 매수 주문을 설정합니다.
        """
        coin_qty = amount_krw / price
        fee = amount_krw * self.config.FEE_RATE

        if self.config.PAPER_TRADING:
            order_info = {
                "uuid": f"paper_buy_{int(time.time())}",
                "type": "limit_buy",
                "market": market,
                "price": price,
                "volume": coin_qty,
                "amount_krw": amount_krw,
                "fee": fee,
                "status": "wait",
                "placed_at": datetime.now(),
            }
            self.active_buy_order = order_info
            self.buy_order_placed_at = datetime.now()
            logger.info(
                f"[PAPER] 지정가 매수 주문: {market} | "
                f"가격={fmt_price(price)} | 수량={coin_qty:.8f} | 금액={amount_krw:,.0f}원"
            )
            return order_info

        # 실거래
        try:
            order = self.client.upbit.buy_limit_order(market, price, coin_qty)
            order_info = {
                "uuid": order.get("uuid"),
                "type": "limit_buy",
                "market": market,
                "price": price,
                "volume": coin_qty,
                "amount_krw": amount_krw,
                "fee": fee,
                "status": "wait",
                "placed_at": datetime.now(),
            }
            self.active_buy_order = order_info
            self.buy_order_placed_at = datetime.now()
            logger.info(f"[LIVE] 지정가 매수 주문: {order}")
            return order_info
        except Exception as e:
            logger.error(f"지정가 매수 주문 실패: {e}")
            return None

    def place_limit_sell(self, market: str, price: float, coin_qty: float, order_type: str = "tp") -> dict:
        """
        지정가 매도 주문을 설정합니다.
        order_type: "tp" (익절) 또는 "sl" (손절)
        """
        if self.config.PAPER_TRADING:
            order_info = {
                "uuid": f"paper_sell_{order_type}_{int(time.time())}",
                "type": f"limit_sell_{order_type}",
                "market": market,
                "price": price,
                "volume": coin_qty,
                "status": "wait",
                "placed_at": datetime.now(),
            }
            if order_type == "tp":
                self.active_tp_order = order_info
            else:
                self.active_sl_order = order_info
            label = "익절" if order_type == "tp" else "손절"
            logger.info(
                f"[PAPER] 지정가 {label} 주문: {market} | "
                f"가격={fmt_price(price)} | 수량={coin_qty:.8f}"
            )
            return order_info

        # 실거래
        try:
            order = self.client.upbit.sell_limit_order(market, price, coin_qty)
            order_info = {
                "uuid": order.get("uuid"),
                "type": f"limit_sell_{order_type}",
                "market": market,
                "price": price,
                "volume": coin_qty,
                "status": "wait",
                "placed_at": datetime.now(),
            }
            if order_type == "tp":
                self.active_tp_order = order_info
            else:
                self.active_sl_order = order_info
            return order_info
        except Exception as e:
            logger.error(f"지정가 매도 주문 실패: {e}")
            return None

    # ──────────────────────────────────────────
    # 체결 확인 및 관리
    # ──────────────────────────────────────────

    def check_buy_order_filled(self, market: str) -> bool:
        """매수 주문 체결 여부를 확인합니다."""
        if self.active_buy_order is None:
            return False

        if self.config.PAPER_TRADING:
            # 페이퍼: 현재가가 주문가 이하이면 체결로 간주
            current_price = self.client.get_current_price(market)
            if current_price is None:
                return False
            if current_price <= self.active_buy_order["price"]:
                self.active_buy_order["status"] = "done"
                logger.info(
                    f"[PAPER] 매수 주문 체결! | 주문가={self.active_buy_order['price']:,.0f} | "
                    f"현재가={current_price:,.0f}"
                )
                return True
            return False

        # 실거래: 주문 상태 조회
        try:
            order = self.client.upbit.get_order(self.active_buy_order["uuid"])
            if order and order.get("state") == "done":
                self.active_buy_order["status"] = "done"
                logger.info(f"[LIVE] 매수 주문 체결: {order}")
                return True
            return False
        except Exception as e:
            logger.error(f"주문 상태 조회 오류: {e}")
            return False

    def check_sell_orders(self, market: str) -> dict:
        """
        매도 주문(익절/손절) 체결 여부를 확인합니다.

        반환: {
            'filled': True/False,
            'type': 'tp' | 'sl' | None,
            'price': float,
        }
        """
        current_price = self.client.get_current_price(market)
        if current_price is None:
            return {"filled": False, "type": None, "price": 0}

        if self.config.PAPER_TRADING:
            # 페이퍼: 현재가 기준 체결 판단
            # 익절: 현재가 >= 익절가
            if self.active_tp_order and current_price >= self.active_tp_order["price"]:
                self.active_tp_order["status"] = "done"
                logger.info(f"[PAPER] 익절 체결! 가격={self.active_tp_order['price']:,.0f}")
                return {"filled": True, "type": "tp", "price": self.active_tp_order["price"]}
            # 손절: 현재가 <= 손절가
            if self.active_sl_order and current_price <= self.active_sl_order["price"]:
                self.active_sl_order["status"] = "done"
                logger.info(f"[PAPER] 손절 체결! 가격={self.active_sl_order['price']:,.0f}")
                return {"filled": True, "type": "sl", "price": self.active_sl_order["price"]}
            return {"filled": False, "type": None, "price": current_price}

        # 실거래: 주문 상태 조회
        for order_type, order in [("tp", self.active_tp_order), ("sl", self.active_sl_order)]:
            if order is None:
                continue
            try:
                result = self.client.upbit.get_order(order["uuid"])
                if result and result.get("state") == "done":
                    order["status"] = "done"
                    return {"filled": True, "type": order_type, "price": order["price"]}
            except Exception:
                continue

        return {"filled": False, "type": None, "price": current_price}

    def check_buy_timeout(self) -> bool:
        """매수 주문이 타임아웃되었는지 확인합니다."""
        if self.buy_order_placed_at is None:
            return False
        elapsed = datetime.now() - self.buy_order_placed_at
        return elapsed > timedelta(minutes=ORDER_TIMEOUT_MINUTES)

    def cancel_buy_order(self, market: str) -> bool:
        """매수 주문을 취소합니다."""
        if self.active_buy_order is None:
            return True

        if self.config.PAPER_TRADING:
            logger.info(f"[PAPER] 매수 주문 취소: {self.active_buy_order['price']:,.0f}")
            self.active_buy_order = None
            self.buy_order_placed_at = None
            return True

        try:
            self.client.upbit.cancel_order(self.active_buy_order["uuid"])
            logger.info(f"[LIVE] 매수 주문 취소: {self.active_buy_order['uuid']}")
            self.active_buy_order = None
            self.buy_order_placed_at = None
            return True
        except Exception as e:
            logger.error(f"매수 주문 취소 실패: {e}")
            return False

    def cancel_sell_orders(self) -> bool:
        """모든 매도 주문을 취소합니다."""
        success = True
        for order_type, order in [("tp", self.active_tp_order), ("sl", self.active_sl_order)]:
            if order is None or order["status"] == "done":
                continue
            if self.config.PAPER_TRADING:
                logger.info(f"[PAPER] {order_type} 매도 주문 취소")
            else:
                try:
                    self.client.upbit.cancel_order(order["uuid"])
                except Exception as e:
                    logger.error(f"{order_type} 매도 주문 취소 실패: {e}")
                    success = False

        self.active_tp_order = None
        self.active_sl_order = None
        return success

    def update_exit_prices(self, market: str, new_tp: float, new_sl: float, coin_qty: float):
        """
        차트 분석 결과에 따라 익절/손절 가격을 동적으로 조정합니다.
        기존 주문 취소 후 새 가격으로 재설정.
        """
        current_price = self.client.get_current_price(market)
        if current_price is None:
            return

        ref = current_price
        new_tp = self._round_to_tick(new_tp, ref)
        new_sl = self._round_to_tick(new_sl, ref)

        # 기존 주문 취소
        self.cancel_sell_orders()
        time.sleep(0.2)

        # 새 주문 설정
        self.place_limit_sell(market, new_tp, coin_qty, "tp")
        self.place_limit_sell(market, new_sl, coin_qty, "sl")

        logger.info(f"익절/손절 가격 조정: TP={new_tp:,.0f} / SL={new_sl:,.0f}")

    def clear_all(self):
        """모든 주문 상태 초기화"""
        self.active_buy_order = None
        self.active_tp_order = None
        self.active_sl_order = None
        self.buy_order_placed_at = None
