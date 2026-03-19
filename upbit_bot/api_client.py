"""
업비트 API 클라이언트 래퍼
pyupbit 라이브러리를 사용하여 업비트 API를 추상화합니다.
"""

import time
import logging
from typing import Optional
import pandas as pd
import pyupbit

logger = logging.getLogger(__name__)


class UpbitClient:
    def __init__(self, access_key: str, secret_key: str, paper_trading: bool = True):
        self.paper_trading = paper_trading
        self.upbit = None
        if not paper_trading and access_key != "YOUR_ACCESS_KEY":
            try:
                self.upbit = pyupbit.Upbit(access_key, secret_key)
                logger.info("업비트 실거래 모드로 연결됨")
            except Exception as e:
                logger.error(f"업비트 연결 실패: {e}")
                raise
        else:
            logger.info("페이퍼 트레이딩 모드 (실제 주문 없음)")

    def get_candles(
        self,
        market: str,
        unit: int = 60,
        count: int = 200,
    ) -> Optional[pd.DataFrame]:
        """분봉 데이터를 가져옵니다."""
        try:
            df = pyupbit.get_ohlcv(market, interval=f"minute{unit}", count=count)
            if df is None or df.empty:
                logger.warning(f"캔들 데이터 없음: {market}")
                return None
            df.columns = ["open", "high", "low", "close", "volume", "value"]
            df.index.name = "datetime"
            return df
        except Exception as e:
            logger.error(f"캔들 데이터 조회 오류: {e}")
            return None

    def get_current_price(self, market: str) -> Optional[float]:
        """현재가를 가져옵니다."""
        try:
            return pyupbit.get_current_price(market)
        except Exception as e:
            logger.error(f"현재가 조회 오류: {e}")
            return None

    def get_balance_krw(self) -> float:
        """원화 잔고를 가져옵니다."""
        if self.paper_trading or self.upbit is None:
            return 0.0
        try:
            return self.upbit.get_balance("KRW")
        except Exception as e:
            logger.error(f"잔고 조회 오류: {e}")
            return 0.0

    def get_balance_coin(self, ticker: str) -> float:
        """코인 잔고를 가져옵니다. ticker: 'BTC' 등"""
        if self.paper_trading or self.upbit is None:
            return 0.0
        try:
            return self.upbit.get_balance(ticker)
        except Exception as e:
            logger.error(f"코인 잔고 조회 오류: {e}")
            return 0.0

    def buy_market_order(self, market: str, amount_krw: float) -> Optional[dict]:
        """
        시장가 매수 주문.
        paper_trading=True이면 실제 주문 없이 딕셔너리를 반환합니다.
        """
        if self.paper_trading or self.upbit is None:
            current_price = self.get_current_price(market)
            if current_price is None:
                return None
            fee = amount_krw * 0.0005
            coin_qty = (amount_krw - fee) / current_price
            logger.info(
                f"[PAPER] 시장가 매수: {market} | 금액={amount_krw:,.0f}원 | "
                f"가격={current_price:,.0f} | 수량={coin_qty:.8f} | 수수료={fee:.0f}원"
            )
            return {
                "type": "paper_buy",
                "market": market,
                "price": current_price,
                "amount_krw": amount_krw,
                "coin_qty": coin_qty,
                "fee": fee,
            }
        try:
            order = self.upbit.buy_market_order(market, amount_krw)
            logger.info(f"[LIVE] 시장가 매수 주문: {order}")
            return order
        except Exception as e:
            logger.error(f"매수 주문 오류: {e}")
            return None

    def sell_market_order(self, market: str, coin_qty: float) -> Optional[dict]:
        """
        시장가 매도 주문.
        paper_trading=True이면 실제 주문 없이 딕셔너리를 반환합니다.
        """
        if self.paper_trading or self.upbit is None:
            current_price = self.get_current_price(market)
            if current_price is None:
                return None
            revenue = coin_qty * current_price
            fee = revenue * 0.0005
            net_revenue = revenue - fee
            logger.info(
                f"[PAPER] 시장가 매도: {market} | 수량={coin_qty:.8f} | "
                f"가격={current_price:,.0f} | 금액={net_revenue:,.0f}원 | 수수료={fee:.0f}원"
            )
            return {
                "type": "paper_sell",
                "market": market,
                "price": current_price,
                "coin_qty": coin_qty,
                "revenue": net_revenue,
                "fee": fee,
            }
        try:
            order = self.upbit.sell_market_order(market, coin_qty)
            logger.info(f"[LIVE] 시장가 매도 주문: {order}")
            return order
        except Exception as e:
            logger.error(f"매도 주문 오류: {e}")
            return None

    def get_orderbook(self, market: str) -> Optional[dict]:
        """호가창 정보를 가져옵니다 (스프레드 확인용)."""
        try:
            ob = pyupbit.get_orderbook(market)
            if ob and len(ob) > 0:
                return ob[0]
            return None
        except Exception as e:
            logger.error(f"호가창 조회 오류: {e}")
            return None
