"""
메인 트레이더 모듈

지정가 주문 기반 매매 흐름:
1. 마켓 스캔 → 최적 종목 선택
2. 차트 분석 → 최적 진입가 산출 → 지정가 매수 주문 설정
3. 매수 체결 대기 (타임아웃 시 자동 취소 → 재스캔)
4. 체결 → 즉시 익절/손절 지정가 매도 주문 설정
5. 차트 지속 분석 → 익절/손절 가격 동적 조정
6. 매도 체결 → 수익 기록 → 1번으로 복귀
"""

import time
import logging
import threading
from datetime import datetime
from typing import Any

import numpy as np
import pyupbit

from api_client import UpbitClient
from indicators import add_all_indicators, get_signal_score
from market_scanner import MarketScanner
from order_manager import OrderManager, fmt_price
from market_indicators import MarketEnvironment
from trade_logger import TradeLogger

logger = logging.getLogger(__name__)


# 상태 정의
STATE_IDLE = "idle"               # 대기 (마켓 스캔 필요)
STATE_SCANNING = "scanning"       # 마켓 스캔 중
STATE_BUY_WAITING = "buy_wait"    # 지정가 매수 체결 대기
STATE_POSITION = "position"       # 포지션 보유 (익절/손절 대기)


class Trader:
    def __init__(self, config):
        self.config = config
        self.client = UpbitClient(
            config.ACCESS_KEY,
            config.SECRET_KEY,
            paper_trading=config.PAPER_TRADING,
        )
        self.scanner = MarketScanner(config)
        self.order_mgr = OrderManager(self.client, config)
        self.market_env = MarketEnvironment()
        self.trade_logger = TradeLogger(config.LOG_FILE, config.PERFORMANCE_FILE)

        # 상태 머신
        self.state = STATE_IDLE
        self.current_market: str | None = None
        self.entry_price = 0.0
        self.coin_qty = 0.0
        self.highest_price = 0.0
        self.entry_signal_score = 0
        self._pending_entry: dict[str, Any] | None = None
        self._pending_trade_amount: float = 0.0

        # 페이퍼 자금
        self.paper_capital = 1_000_000

        # 동적 조정 카운터 (매 N사이클마다 익절/손절 재평가)
        self.adjust_counter = 0
        self.adjust_every = 5

    def run(self):
        """메인 트레이딩 루프"""
        mode = "📄 페이퍼 트레이딩" if self.config.PAPER_TRADING else "💰 실거래"
        print(f"\n{'='*60}")
        print(f"  🤖 업비트 AI 자동매매 봇 시작 ({mode})")
        print(f"  주문 방식: 지정가 (최적 진입가 분석)")
        print(f"  체크 주기: {self.config.CHECK_INTERVAL}초")
        print(f"  진입 최소 신호: {self.config.MIN_SIGNAL_COUNT}개 이상")
        print(f"  손절: {self.config.STOP_LOSS_PCT*100:.1f}% | 익절: {self.config.TAKE_PROFIT_PCT*100:.1f}%")
        print(f"{'='*60}\n")

        scan_cycle = 0

        while True:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if self.state == STATE_IDLE:
                    self._handle_idle(now)

                elif self.state == STATE_BUY_WAITING:
                    self._handle_buy_waiting(now)

                elif self.state == STATE_POSITION:
                    self._handle_position(now)

                time.sleep(self.config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n⛔ 봇 종료 요청 수신")
                self._shutdown()
                break
            except Exception as e:
                logger.error(f"루프 오류: {e}", exc_info=True)
                time.sleep(30)

    # ──────────────────────────────────────────
    # 상태 핸들러
    # ──────────────────────────────────────────

    # ──────────────────────────────────────────
    # 시장 국면 필터 (하락장 진입 차단)
    # ──────────────────────────────────────────

    def _check_trend_filter(self, market: str) -> dict:
        """
        EMA 기반 시장 국면을 판별합니다.

        반환값:
          {
            'allowed': True/False,   # True=진입 허용, False=차단
            'regime':  'bull'/'bear'/'sideways',
            'reason':  설명 문자열,
            'ema50':   float,
            'ema200':  float,
            'close':   float,
          }
        """
        if not getattr(self.config, "USE_TREND_FILTER", True):
            return {"allowed": True, "regime": "unknown", "reason": "필터 비활성화"}

        try:
            # EMA200 계산을 위해 최소 220개 캔들 필요
            df = pyupbit.get_ohlcv(
                market,
                interval=f"minute{self.config.CANDLE_UNIT}",
                count=220,
            )
            if df is None or len(df) < 60:
                if not getattr(self.config, "PAPER_TRADING", True):
                    # 실거래 모드: 데이터 부족 시 안전하게 차단
                    return {"allowed": False, "regime": "unknown", "reason": "데이터 부족 — 실거래 안전 차단"}
                return {"allowed": True, "regime": "unknown", "reason": "데이터 부족 — 필터 스킵 (페이퍼)"}

            df.columns = ["open", "high", "low", "close", "volume", "value"]
            closes = df["close"]
            ema50  = float(closes.ewm(span=50,  adjust=False).mean().iloc[-1])
            ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
            close  = float(closes.iloc[-1])

            strict = getattr(self.config, "TREND_FILTER_STRICT", False)

            # 하락장 판단
            price_below_ema200 = close < ema200
            ema50_below_ema200 = ema50  < ema200

            if strict:
                is_bear = ema50_below_ema200
            else:
                is_bear = price_below_ema200 and ema50_below_ema200

            if is_bear:
                regime = "bear"
                reason = (
                    f"하락장 감지 (현재가 {close:,.0f} < EMA200 {ema200:,.0f}, "
                    f"EMA50 {ema50:,.0f} < EMA200)"
                )
                allowed = False
            elif price_below_ema200:
                regime = "sideways"
                reason = f"횡보장 (현재가 < EMA200이나 EMA50 >= EMA200) → 진입 허용"
                allowed = True
            else:
                regime = "bull"
                reason = f"상승장 (현재가 {close:,.0f} > EMA200 {ema200:,.0f}) → 진입 허용"
                allowed = True

            return {
                "allowed": allowed,
                "regime":  regime,
                "reason":  reason,
                "ema50":   ema50,
                "ema200":  ema200,
                "close":   close,
            }

        except Exception as e:
            logger.warning(f"국면 필터 오류 ({e}) → 필터 스킵")
            return {"allowed": True, "regime": "unknown", "reason": f"오류 발생: {e}"}

    def _handle_idle(self, now: str):
        """IDLE: 마켓 스캔 → 최적 종목 선택 → 국면 필터 → 진입가 분석 → 지정가 매수"""
        print(f"\n[{now}] 🔍 마켓 스캔 & 진입 기회 탐색...")

        best_market = self.scanner.select_best_market()
        if best_market is None:
            print(f"[{now}] ⏸️  진입 조건 충족 마켓 없음. 다음 사이클 대기...")
            return

        self.current_market = best_market

        # ── 시장 국면 필터 (하락장 차단) ──
        trend = self._check_trend_filter(best_market)
        regime_emoji = {"bull": "📈", "bear": "📉", "sideways": "↔️", "unknown": "❓"}
        print(f"  {regime_emoji.get(trend['regime'], '❓')} 시장 국면: {trend['reason']}")
        if not trend["allowed"]:
            print(f"  ⛔ 하락장 진입 차단 → 다음 사이클 대기")
            return

        # 시장 환경 체크 (기술적 지표 외 매개변수)
        self.market_env.print_market_environment(best_market)
        env_score = self.market_env.get_market_score(best_market)
        if env_score["score"] < -30:
            print(f"  ⛔ 시장 환경 불리 ({env_score['score']}/100) → 진입 보류")
            return

        # 최적 진입가 분석
        entry_info = self.order_mgr.calculate_optimal_entry_price(best_market)
        if entry_info is None:
            print(f"  ❌ {best_market} 진입가 분석 실패")
            return

        print(f"\n  📊 진입가 분석 결과:")
        print(f"    현재가      : {fmt_price(entry_info['current_price']):>12}원")
        print(f"    진입가      : {fmt_price(entry_info['entry_price']):>12}원 ({entry_info['method']})")
        print(f"    할인율      : {entry_info['discount_pct']:>12.2f}%")
        print(f"    익절 목표   : {fmt_price(entry_info['tp_price']):>12}원 ({entry_info['tp_pct']:+.2f}%)")
        print(f"    손절 기준   : {fmt_price(entry_info['sl_price']):>12}원 ({entry_info['sl_pct']:+.2f}%)")
        print(f"    지지선      : {', '.join(fmt_price(s) for s in entry_info['support_levels'])}")

        # 지정가 매수 주문 설정
        trade_amount = self.config.TRADE_AMOUNT_KRW
        if self.config.PAPER_TRADING:
            trade_amount = min(trade_amount, self.paper_capital * 0.95)
        if trade_amount < 5000:
            logger.warning("매수 금액이 최소 주문금액(5,000원) 미만")
            return

        order = self.order_mgr.place_limit_buy(
            best_market,
            entry_info["entry_price"],
            trade_amount,
        )
        if order is None:
            return

        # 상태 전이: IDLE → BUY_WAITING
        self.state = STATE_BUY_WAITING
        self._pending_entry = entry_info
        self._pending_trade_amount = trade_amount
        print(f"\n  📝 지정가 매수 주문 설정 완료 → 체결 대기 중...")

    def _handle_buy_waiting(self, now: str):
        """BUY_WAITING: 매수 체결 대기, 타임아웃 관리"""
        order = self.order_mgr.active_buy_order
        if order is None:
            self.state = STATE_IDLE
            return

        market = order["market"]
        order_price = order["price"]
        current_price = self.client.get_current_price(market)
        if current_price is None:
            return

        diff_pct = (current_price - order_price) / order_price * 100

        print(
            f"[{now}] ⏳ 매수 대기 | {market} | "
            f"주문가={fmt_price(order_price)} | 현재가={fmt_price(current_price)} ({diff_pct:+.2f}%)"
        )

        # 체결 확인
        if self.order_mgr.check_buy_order_filled(market):
            self._on_buy_filled(market)
            return

        # 타임아웃 확인 (30분)
        if self.order_mgr.check_buy_timeout():
            print(f"  ⏱️  매수 주문 타임아웃 → 주문 취소 → 재스캔")
            self.order_mgr.cancel_buy_order(market)
            self.state = STATE_IDLE
            return

        # 가격이 주문가에서 너무 멀어졌으면 취소 후 재분석
        if diff_pct > 2.0:
            print(f"  📈 가격이 주문가 대비 {diff_pct:.1f}% 상승 → 주문 취소 → 재분석")
            self.order_mgr.cancel_buy_order(market)
            self.state = STATE_IDLE

    def _on_buy_filled(self, market: str):
        """매수 체결 시 처리"""
        order = self.order_mgr.active_buy_order
        entry_info = self._pending_entry
        if order is None or entry_info is None:
            logger.warning("매수 체결 처리 중 주문/진입정보 누락 -> 상태 초기화")
            self.state = STATE_IDLE
            self.order_mgr.clear_all()
            return

        self.entry_price = order["price"]
        self.coin_qty = order["volume"]
        self.highest_price = self.entry_price
        self.current_market = market

        fee = order["fee"]
        if self.config.PAPER_TRADING:
            # 매수금액 + 수수료를 함께 차감 (실거래 시뮬레이션)
            self.paper_capital -= (self._pending_trade_amount + fee)

        # 신호 점수 기록
        df = pyupbit.get_ohlcv(
            market, interval=f"minute{self.config.CANDLE_UNIT}", count=self.config.CANDLE_COUNT
        )
        signal_score = 0
        signals = {}
        if df is not None and not df.empty:
            df.columns = ["open", "high", "low", "close", "volume", "value"]
            df = add_all_indicators(df, self.config)
            df = df.dropna()
            if not df.empty:
                result = get_signal_score(df.iloc[-1], self.config)
                signal_score = result["score"]
                signals = result["signals"]

        self.entry_signal_score = signal_score

        self.trade_logger.log_buy(
            market=market,
            price=self.entry_price,
            amount_krw=self._pending_trade_amount,
            coin_qty=self.coin_qty,
            fee=fee,
            signal_score=signal_score,
            signals=signals,
        )

        print(f"\n🟢 매수 체결! | {market} | 가격={fmt_price(self.entry_price)} | 수량={self.coin_qty:.8f}")

        # 즉시 익절/손절 매도 주문 설정
        tp_price = entry_info["tp_price"]
        sl_price = entry_info["sl_price"]
        self.order_mgr.place_limit_sell(market, tp_price, self.coin_qty, "tp")
        self.order_mgr.place_limit_sell(market, sl_price, self.coin_qty, "sl")

        print(f"  📤 익절 주문: {fmt_price(tp_price)} | 손절 주문: {fmt_price(sl_price)}")

        # 상태 전이
        self.state = STATE_POSITION
        self.adjust_counter = 0

    def _handle_position(self, now: str):
        """POSITION: 포지션 보유 중 — 체결 확인 + 차트 분석 + 동적 조정"""
        market = self.current_market
        if market is None:
            logger.warning("포지션 상태인데 current_market이 None -> 상태 초기화")
            self.state = STATE_IDLE
            self.order_mgr.clear_all()
            return

        current_price = self.client.get_current_price(market)
        if current_price is None:
            return

        self.highest_price = max(self.highest_price, current_price)
        pnl_pct = (current_price - self.entry_price) / self.entry_price * 100

        tp_str = fmt_price(self.order_mgr.active_tp_order['price']) if self.order_mgr.active_tp_order else "-"
        sl_str = fmt_price(self.order_mgr.active_sl_order['price']) if self.order_mgr.active_sl_order else "-"

        print(
            f"[{now}] 📊 포지션 | {market} | "
            f"진입={fmt_price(self.entry_price)} | 현재={fmt_price(current_price)} ({pnl_pct:+.2f}%) | "
            f"TP={tp_str} SL={sl_str}"
        )

        # ── 익절/손절 체결 확인 ──
        sell_result = self.order_mgr.check_sell_orders(market)
        if sell_result["filled"]:
            self._on_sell_filled(sell_result)
            return

        # ── 차트 분석 → 동적 익절/손절 조정 ──
        self.adjust_counter += 1
        if self.adjust_counter % self.adjust_every == 0:
            self._dynamic_adjust_exit(market, current_price)

    def _dynamic_adjust_exit(self, market: str, current_price: float):
        """차트를 재분석하여 익절/손절 가격을 동적으로 조정합니다."""
        df = pyupbit.get_ohlcv(
            market, interval=f"minute{self.config.CANDLE_UNIT}", count=self.config.CANDLE_COUNT
        )
        if df is None or df.empty:
            return

        df.columns = ["open", "high", "low", "close", "volume", "value"]
        df = add_all_indicators(df, self.config)
        df = df.dropna()
        if df.empty:
            return

        row = df.iloc[-1]
        pnl_pct = (current_price - self.entry_price) / self.entry_price

        # ── 트레일링 스탑 로직 ──
        # 수익 중이면 손절선을 올려서 수익 보호
        if pnl_pct > 0.02:  # 2% 이상 수익이면
            # 트레일링: 고점 대비 TRAILING_STOP_PCT 아래
            new_sl = self.highest_price * (1 - self.config.TRAILING_STOP_PCT)
            # 기존 손절보다 높은 경우에만 조정 (손절선은 올리기만 함)
            if self.order_mgr.active_sl_order:
                old_sl = self.order_mgr.active_sl_order["price"]
                if new_sl > old_sl:
                    new_sl = self.order_mgr._round_to_tick(new_sl, current_price)
                    print(f"  📈 트레일링 스탑 조정: SL {fmt_price(old_sl)} → {fmt_price(new_sl)}")

                    # 익절 목표도 볼린저 상단 고려하여 조정
                    new_tp = max(
                        self.entry_price * (1 + self.config.TAKE_PROFIT_PCT),
                        row["bb_upper"] * 0.998,  # 볼린저 상단 근처
                    )
                    new_tp = self.order_mgr._round_to_tick(new_tp, current_price)

                    # 주문 취소-재설정 전 마지막 체결 확인 (race condition 방지)
                    pre_check = self.order_mgr.check_sell_orders(market)
                    if pre_check["filled"]:
                        self._on_sell_filled(pre_check)
                        return
                    self.order_mgr.update_exit_prices(
                        market, new_tp, new_sl, self.coin_qty
                    )

        # ── 위험 신호 감지: 즉시 시장가 청산 ──
        # MACD 데드크로스 + RSI 급락 + 거래량 급증 = 급락 신호
        macd_dead_cross = row["macd_hist"] < 0 and row["macd"] > 0
        rsi_falling = row["rsi"] > self.config.RSI_OVERBOUGHT
        volume_spike = row["volume_ratio"] > 3.0

        danger_count = sum([macd_dead_cross, rsi_falling, volume_spike])
        if danger_count >= 2 and pnl_pct > 0:
            print(f"  ⚠️ 위험 신호 {danger_count}개 감지! → 시장가 즉시 청산")
            self.order_mgr.cancel_sell_orders()
            order = self.client.sell_market_order(market, self.coin_qty)
            if order:
                exit_price = order.get("price", current_price)
                fee = order.get("fee", 0)
                if self.config.PAPER_TRADING:
                    self.paper_capital += order.get("revenue", self.coin_qty * current_price)
                self._record_sell(exit_price, fee, "위험신호 시장가 청산")

    def _on_sell_filled(self, sell_result: dict):
        """매도 체결 시 처리"""
        exit_price = sell_result["price"]
        sell_type = sell_result["type"]
        reason = "익절" if sell_type == "tp" else "손절"

        fee = exit_price * self.coin_qty * self.config.FEE_RATE

        # 반대편 주문 취소
        self.order_mgr.cancel_sell_orders()

        if self.config.PAPER_TRADING:
            revenue = self.coin_qty * exit_price - fee
            self.paper_capital += revenue

        self._record_sell(exit_price, fee, f"지정가 {reason}")

    def _record_sell(self, exit_price: float, fee: float, reason: str):
        """매도 기록 + 상태 초기화"""
        market = self.current_market
        if market is None:
            logger.warning("매도 기록 시 current_market이 None -> UNKNOWN으로 기록")
            market = "UNKNOWN"

        self.trade_logger.log_sell(
            market=market,
            entry_price=self.entry_price,
            exit_price=exit_price,
            coin_qty=self.coin_qty,
            fee=fee,
            reason=reason,
        )

        pnl_pct = (exit_price - self.entry_price) / self.entry_price * 100
        emoji = "🟢" if exit_price >= self.entry_price else "🔴"
        print(
            f"\n{emoji} 매도 완료 | {market} | "
            f"진입={fmt_price(self.entry_price)} → 청산={fmt_price(exit_price)} | "
            f"손익={pnl_pct:+.2f}% | 사유={reason}"
        )
        if self.config.PAPER_TRADING:
            print(f"   💰 페이퍼 잔고: {self.paper_capital:,.0f}원")

        # 상태 초기화
        self.state = STATE_IDLE
        self.current_market = None
        self.entry_price = 0.0
        self.coin_qty = 0.0
        self.highest_price = 0.0
        self.order_mgr.clear_all()

        self.trade_logger.print_summary()

    @staticmethod
    def _input_with_timeout(prompt: str, timeout: float = 30.0, default: str = "n") -> str:
        """타임아웃이 있는 input(). timeout초 내 응답 없으면 default를 반환합니다."""
        result = [default]
        answered = threading.Event()

        def _read():
            try:
                result[0] = input(prompt).strip().lower()
            except Exception:
                pass
            answered.set()

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        if not answered.wait(timeout):
            print(f"\n  ⏱️ {timeout:.0f}초 내 응답 없음 → 기본값 '{default}' 적용")
        return result[0]

    def _shutdown(self):
        """봇 종료 처리"""
        if self.state == STATE_BUY_WAITING:
            print("  📝 대기 중인 매수 주문 취소...")
            if self.current_market is not None:
                self.order_mgr.cancel_buy_order(self.current_market)

        elif self.state == STATE_POSITION:
            print("  ⚠️ 미청산 포지션 존재. 강제 청산하시겠습니까?")
            answer = self._input_with_timeout("강제 청산 (y/n, 30초 내 응답): ", timeout=30.0, default="n")
            if answer == "y":
                if self.current_market is None:
                    logger.warning("강제청산 중 current_market이 None -> 청산 생략")
                    return
                self.order_mgr.cancel_sell_orders()
                order = self.client.sell_market_order(self.current_market, self.coin_qty)
                if order:
                    exit_price = order.get("price", self.entry_price)
                    fee = order.get("fee", 0)
                    if self.config.PAPER_TRADING:
                        self.paper_capital += order.get("revenue", 0)
                    self._record_sell(exit_price, fee, "강제 청산 (봇 종료)")
            else:
                print("  ℹ️ 기존 지정가 매도 주문은 유지됩니다.")

        self.trade_logger.print_summary()
