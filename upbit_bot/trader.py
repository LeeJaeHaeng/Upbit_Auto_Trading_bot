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

import atexit
import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pyupbit

from api_client import UpbitClient
from indicators import add_all_indicators, get_signal_score
from market_scanner import MarketScanner
from order_manager import OrderManager, fmt_price
from market_indicators import MarketEnvironment
from trade_logger import TradeLogger
import backtest_db as _bdb

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

        # 페이퍼 자금 — 이전 세션 잔고 자동 복원 (없으면 초기값 100만원)
        if config.PAPER_TRADING:
            self.paper_capital = _bdb.get_last_paper_capital(fallback=1_000_000)
        else:
            self.paper_capital = 1_000_000  # 실거래 모드는 실제 잔고 사용

        # DB 세션 ID (봇 실행마다 고유, 거래 기록 연결용)
        mode = 'paper' if config.PAPER_TRADING else 'live'
        self._db_session_id = _bdb.start_trading_session(mode, config)
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='startup', mode=mode,
            krw_balance=self.paper_capital,
            note=f'봇 시작 (이전잔고 복원: {self.paper_capital:,.0f}원)',
        )

        # 동적 조정 카운터 (매 N사이클마다 익절/손절 재평가)
        self.adjust_counter = 0
        self.adjust_every = 5

        # 분할 투자 (DCA) 추적 변수
        # ── 물타기 다단계 DCA ──
        self._dca_levels_pending = []   # [(trigger_price, amount), ...] 아직 안 터진 단계
        self._dca_order_pending = False # DCA 주문 체결 대기 중
        self._dca_current_amount = 0.0  # 현재 체결 대기 중인 DCA 금액
        self._dca_timeout_at = None     # 전체 DCA 대기 만료 시각
        self._dca_done = False          # 모든 DCA 완료 여부
        self._avg_entry_price = 0.0     # 평균 단가
        # ── 불타기 피라미딩 ──
        self._pyramid_done = False      # 불타기 완료 여부
        self._pyramid_order_pending = False  # 불타기 주문 체결 대기 중
        self._pyramid_amount = 0.0      # 불타기 투자금액

        # 본전 보호 활성화 추적
        self._breakeven_activated = False

        # 연속 오류 카운터 (일정 횟수 초과 시 긴급 종료)
        self._consecutive_errors = 0
        self._max_consecutive_errors = 10

        # 상태 영속화 파일 (비정상 종료 후 복구용)
        self._state_file = Path(config.BASE_DIR) / "bot_state.json"
        # 대시보드 실시간 현황 공유 파일 (매 사이클 갱신)
        self._live_status_file = Path(config.BASE_DIR) / "live_status.json"

        # BUY_WAITING/POSITION 중 국면 재확인 카운터
        self._trend_check_counter = 0
        self._trend_check_every = 3  # 매 3사이클(3분)마다 국면 재확인

    def run(self):
        """메인 트레이딩 루프"""
        # 예상치 못한 종료(atexit, SIGTERM 등) 시 상태 저장 등록
        atexit.register(self._emergency_shutdown)

        mode = "📄 페이퍼 트레이딩" if self.config.PAPER_TRADING else "💰 실거래"
        print(f"\n{'='*60}")
        print(f"  🤖 업비트 AI 자동매매 봇 시작 ({mode})")
        print(f"  주문 방식: 지정가 (최적 진입가 분석)")
        print(f"  체크 주기: {self.config.CHECK_INTERVAL}초")
        print(f"  진입 최소 신호: {self.config.MIN_SIGNAL_COUNT}개 이상")
        print(f"  손절: {self.config.STOP_LOSS_PCT*100:.1f}% | 익절: {self.config.TAKE_PROFIT_PCT*100:.1f}%")
        if self.config.PAPER_TRADING:
            print(f"  💰 모의잔고: {self.paper_capital:,.0f}원 (이전 세션에서 복원)")
        print(f"{'='*60}\n")

        # 이전 실행 상태 복구 시도 (비정상 종료 후 재시작 시)
        self._try_recover_state()

        while True:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if self.state == STATE_IDLE:
                    self._handle_idle(now)

                elif self.state == STATE_BUY_WAITING:
                    self._handle_buy_waiting(now)

                elif self.state == STATE_POSITION:
                    self._handle_position(now)

                self._consecutive_errors = 0  # 정상 사이클 시 오류 카운터 리셋
                self._write_live_status()     # 대시보드용 실시간 상태 갱신
                time.sleep(self.config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n⛔ 봇 종료 요청 수신")
                self._shutdown()
                break
            except Exception as e:
                self._consecutive_errors += 1
                wait_sec = min(30 * self._consecutive_errors, 300)  # 최대 5분 대기
                logger.error(
                    f"루프 오류 (연속 {self._consecutive_errors}회): {e} "
                    f"— {wait_sec}초 후 재시도",
                    exc_info=True,
                )
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.critical(
                        f"연속 오류 {self._max_consecutive_errors}회 초과 → 긴급 상태 저장 후 종료"
                    )
                    self._emergency_shutdown()
                    break
                time.sleep(wait_sec)

    # ──────────────────────────────────────────
    # 상태 핸들러
    # ──────────────────────────────────────────

    # ──────────────────────────────────────────
    # 다중 시간대 추세 확인 (4h EMA)
    # ──────────────────────────────────────────

    def _check_mtf_trend(self, market: str) -> dict:
        """
        4시간봉 EMA20/EMA50 추세를 확인합니다.
        60분봉 신호가 4h 추세와 일치할 때만 진입을 허용합니다.

        반환: {'allowed': bool, 'reason': str, 'ema_short': float, 'ema_long': float}
        """
        if not getattr(self.config, "MTF_CHECK", False):
            return {"allowed": True, "reason": "MTF 비활성화"}

        try:
            mtf_unit = getattr(self.config, "MTF_CANDLE_UNIT", 240)
            df = pyupbit.get_ohlcv(market, interval=f"minute{mtf_unit}", count=60)
            if df is None or len(df) < 55:
                return {"allowed": True, "reason": "4h 데이터 부족 — MTF 스킵"}

            df.columns = ["open", "high", "low", "close", "volume", "value"]
            closes = df["close"]
            ema_short = getattr(self.config, "MTF_EMA_SHORT", 20)
            ema_long = getattr(self.config, "MTF_EMA_LONG", 50)
            mtf_ema_s = float(closes.ewm(span=ema_short, adjust=False).mean().iloc[-1])
            mtf_ema_l = float(closes.ewm(span=ema_long,  adjust=False).mean().iloc[-1])

            # 4h 단기 EMA > 장기 EMA → 상승 추세, 진입 허용
            if mtf_ema_s >= mtf_ema_l:
                return {
                    "allowed": True,
                    "reason": f"4h 상승 추세 (EMA{ema_short}={mtf_ema_s:,.0f} ≥ EMA{ema_long}={mtf_ema_l:,.0f})",
                    "ema_short": mtf_ema_s,
                    "ema_long": mtf_ema_l,
                }
            else:
                return {
                    "allowed": False,
                    "reason": f"4h 하락 추세 (EMA{ema_short}={mtf_ema_s:,.0f} < EMA{ema_long}={mtf_ema_l:,.0f}) → 진입 차단",
                    "ema_short": mtf_ema_s,
                    "ema_long": mtf_ema_l,
                }
        except Exception as e:
            logger.warning(f"MTF 체크 오류: {e} → 스킵")
            return {"allowed": True, "reason": f"MTF 오류: {e}"}

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

        # ── 4시간봉 추세 확인 (MTF 필터) ──
        mtf = self._check_mtf_trend(best_market)
        print(f"  📊 4h 추세: {mtf['reason']}")
        if not mtf["allowed"]:
            print(f"  ⛔ 4h 하락 추세 — 60분봉 신호 무시 → 다음 사이클 대기")
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

        # ── 리스크/리워드 비율 검증 (Paul Tudor Jones: R/R 최소 1.5:1) ──
        tp_pct = entry_info["tp_pct"]
        sl_pct = abs(entry_info["sl_pct"])
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else 0
        min_rr = getattr(self.config, "MIN_RR_RATIO", 1.5)
        if rr_ratio < min_rr:
            print(
                f"  ⛔ R/R 비율 불충분 ({rr_ratio:.1f}x < {min_rr}x) "
                f"[TP={tp_pct:.1f}% / SL={sl_pct:.1f}%] → 다음 사이클 대기"
            )
            return

        print(f"\n  📊 진입가 분석 결과:")
        print(f"    현재가      : {fmt_price(entry_info['current_price']):>12}원")
        print(f"    진입가      : {fmt_price(entry_info['entry_price']):>12}원 ({entry_info['method']})")
        print(f"    할인율      : {entry_info['discount_pct']:>12.2f}%")
        print(f"    익절 목표   : {fmt_price(entry_info['tp_price']):>12}원 ({entry_info['tp_pct']:+.2f}%)")
        print(f"    손절 기준   : {fmt_price(entry_info['sl_price']):>12}원 ({entry_info['sl_pct']:+.2f}%)")
        print(f"    R/R 비율    : {rr_ratio:>12.2f}x")
        print(f"    지지선      : {', '.join(fmt_price(s) for s in entry_info['support_levels'])}")

        # 지정가 매수 주문 설정 — 잔고 비례 or 고정금액
        trade_amount = self._calc_trade_amount()
        if self.config.PAPER_TRADING:
            trade_amount = min(trade_amount, self.paper_capital * 0.95)

        # 물타기(SCALED_ENTRY) 사용 시: 1차 매수는 전체 금액의 일부만 투입
        # SCALED_ENTRY_1ST_RATIO 설정이 실제로 반영되도록 수정
        if getattr(self.config, "SCALED_ENTRY", False):
            first_ratio = getattr(self.config, "SCALED_ENTRY_1ST_RATIO", 0.5)
            first_amount = trade_amount * first_ratio
        else:
            first_amount = trade_amount

        if first_amount < 5000:
            logger.warning("매수 금액이 최소 주문금액(5,000원) 미만")
            return

        order = self.order_mgr.place_limit_buy(
            best_market,
            entry_info["entry_price"],
            first_amount,
        )
        if order is None:
            return

        # 상태 전이: IDLE → BUY_WAITING
        self.state = STATE_BUY_WAITING
        self._pending_entry = entry_info
        self._pending_trade_amount = trade_amount  # 전체 투자금 (DCA 비율 계산 기준)
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
            return

        # 매수 대기 중 하락장 전환 감지 (매 N사이클마다 국면 재확인)
        self._trend_check_counter += 1
        if self._trend_check_counter % self._trend_check_every == 0:
            trend = self._check_trend_filter(market)
            if not trend["allowed"]:
                print(
                    f"  📉 매수 대기 중 하락장 전환 감지: {trend['reason']}"
                    f"\n  ⛔ 매수 주문 즉시 취소 → 재스캔"
                )
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
        actual_buy_amount = order.get("amount_krw", self._pending_trade_amount)
        if self.config.PAPER_TRADING:
            # 실제 매수금액 + 수수료 차감 (SCALED_ENTRY 시 1차 매수금액 기준)
            self.paper_capital -= (actual_buy_amount + fee)

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
                _ind_row = df.iloc[-1].to_dict()
            else:
                _ind_row = {}

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
        _bdb.record_buy(
            session_id=self._db_session_id,
            market=market,
            price=self.entry_price,
            amount_krw=self._pending_trade_amount,
            coin_qty=self.coin_qty,
            fee=fee,
            signal_score=signal_score,
            signals=signals,
            indicators=_ind_row,
        )

        print(f"\n🟢 매수 체결! | {market} | 가격={fmt_price(self.entry_price)} | 수량={self.coin_qty:.8f}")
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='buy',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=self.paper_capital,
            coin_market=market,
            coin_qty=self.coin_qty,
            coin_value_krw=self.entry_price * self.coin_qty,
            note=f'매수 {market} {self.entry_price:,.0f}원',
        )

        # 즉시 익절/손절 매도 주문 설정
        tp_price = entry_info["tp_price"]
        sl_price = entry_info["sl_price"]
        self.order_mgr.place_limit_sell(market, tp_price, self.coin_qty, "tp")
        self.order_mgr.place_limit_sell(market, sl_price, self.coin_qty, "sl")

        print(f"  📤 익절 주문: {fmt_price(tp_price)} | 손절 주문: {fmt_price(sl_price)}")

        # ── 물타기 다단계 DCA 초기화 ──
        self._avg_entry_price = self.entry_price
        self._breakeven_activated = False
        self._dca_levels_pending = []
        self._dca_done = False
        self._pyramid_done = False
        self._pyramid_order_pending = False
        from datetime import timedelta
        scaled = getattr(self.config, "SCALED_ENTRY", False)
        if scaled:
            dca_levels = getattr(self.config, "DCA_LEVELS", [(0.015, 0.25), (0.030, 0.25)])
            timeout_min = getattr(self.config, "SCALED_ENTRY_TIMEOUT_MIN", 60)
            self._dca_timeout_at = datetime.now() + timedelta(minutes=timeout_min)
            for dip_pct, add_ratio in dca_levels:
                trigger_p = self.entry_price * (1 - dip_pct)
                add_amount = self._pending_trade_amount * add_ratio
                self._dca_levels_pending.append((trigger_p, add_amount))
            msgs = [f"{(self.entry_price - p) / self.entry_price * 100:.1f}%↓={fmt_price(p)}" for (p, _) in self._dca_levels_pending]
            print(f"  💧 물타기 설정 ({len(self._dca_levels_pending)}단계): {' / '.join(msgs)} | 만료={timeout_min}분")
        # ── 불타기 피라미딩 초기화 ──
        if getattr(self.config, "PYRAMID_ENABLED", False):
            ratio = getattr(self.config, "PYRAMID_ADD_RATIO", 0.5)
            self._pyramid_amount = self._pending_trade_amount * ratio
            trig = getattr(self.config, "PYRAMID_TRIGGER_PCT", 0.015) * 100
            print(f"  🔥 불타기 설정: +{trig:.1f}% 도달 시 {self._pyramid_amount:,.0f}원 추가 매수")

        # 상태 전이
        self.state = STATE_POSITION
        self.adjust_counter = 0

        # 포지션 정보를 파일에 저장 (비정상 종료 시 복구용)
        self._save_state()

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

        # ── 물타기 다단계 DCA ──
        if self._dca_order_pending and self.order_mgr.active_buy_order is not None:
            if self.order_mgr.check_buy_order_filled(market):
                self._on_dca_filled()
        elif (
            not self._dca_order_pending
            and self._dca_levels_pending
            and self.order_mgr.active_buy_order is None
        ):
            if self._dca_timeout_at and datetime.now() > self._dca_timeout_at:
                logger.info("물타기 타임아웃 → 현 포지션 확정")
                self._dca_levels_pending.clear()
                self._dca_done = True
            else:
                next_trigger, next_amount = self._dca_levels_pending[0]
                if current_price <= next_trigger:
                    order = self.order_mgr.place_limit_buy(market, current_price, next_amount)
                    if order:
                        self._dca_current_amount = next_amount
                        self._dca_order_pending = True
                        level_no = getattr(self.config, "DCA_LEVELS", [(0,0),(0,0)])
                        step = len(level_no) - len(self._dca_levels_pending) + 2
                        print(f"  💧 물타기 {step}차 주문! 가격={fmt_price(current_price)} | 금액={next_amount:,.0f}원")

        # ── 불타기 피라미딩 ──
        if (
            getattr(self.config, "PYRAMID_ENABLED", False)
            and not self._pyramid_done
            and not self._pyramid_order_pending
            and self.order_mgr.active_buy_order is None
            and self.entry_price > 0
        ):
            trig = getattr(self.config, "PYRAMID_TRIGGER_PCT", 0.015)
            pnl_now = (current_price - self.entry_price) / self.entry_price
            if pnl_now >= trig:
                order = self.order_mgr.place_limit_buy(market, current_price, self._pyramid_amount)
                if order:
                    self._pyramid_order_pending = True
                    print(f"  🔥 불타기! +{pnl_now*100:.2f}% 수익 | 가격={fmt_price(current_price)} | 금액={self._pyramid_amount:,.0f}원")
        if self._pyramid_order_pending and self.order_mgr.active_buy_order is not None:
            if self.order_mgr.check_buy_order_filled(market):
                self._on_pyramid_filled()

        # ── 차트 분석 → 동적 익절/손절 조정 ──
        self.adjust_counter += 1
        if self.adjust_counter % self.adjust_every == 0:
            self._dynamic_adjust_exit(market, current_price)


    def _on_pyramid_filled(self):
        """불타기 체결 처리 — 평균단가 재계산 + SL 상향 조정"""
        order = self.order_mgr.active_buy_order
        if order is None:
            return
        p_price = order["price"]
        p_qty   = order["volume"]
        p_fee   = order["fee"]

        total_cost = self.entry_price * self.coin_qty + p_price * p_qty
        self.coin_qty += p_qty
        self._avg_entry_price = total_cost / self.coin_qty
        self.entry_price = self._avg_entry_price

        if self.config.PAPER_TRADING:
            self.paper_capital -= (self._pyramid_amount + p_fee)

        new_tp = self.order_mgr._round_to_tick(
            self.entry_price * (1 + self.config.TAKE_PROFIT_PCT), self.entry_price)
        # 불타기 후 SL → 평균단가+수수료 (손실 없이 탈출 보장)
        if getattr(self.config, "PYRAMID_SL_TO_ENTRY", True):
            new_sl = self.order_mgr._round_to_tick(
                self.entry_price * (1 + self.config.FEE_RATE * 2), self.entry_price)
        else:
            new_sl = self.order_mgr._round_to_tick(
                self.entry_price * (1 - self.config.STOP_LOSS_PCT), self.entry_price)
        self.order_mgr.cancel_sell_orders()
        self.order_mgr.place_limit_sell(self.current_market, new_tp, self.coin_qty, "tp")
        self.order_mgr.place_limit_sell(self.current_market, new_sl, self.coin_qty, "sl")
        self.order_mgr.active_buy_order = None
        self._pyramid_done = True
        self._pyramid_order_pending = False
        self._breakeven_activated = False
        self._save_state()
        print(
            f"  🔥 불타기 체결! | 평균단가={fmt_price(self.entry_price)} | "
            f"총수량={self.coin_qty:.8f} | TP={fmt_price(new_tp)} SL={fmt_price(new_sl)} (본전+수수료)"
        )
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

        # ── 본전 보호 스탑 (Breakeven Stop) ──
        breakeven_trigger = getattr(self.config, "BREAKEVEN_TRIGGER_PCT", 0.0)
        if breakeven_trigger > 0 and not self._breakeven_activated and pnl_pct >= breakeven_trigger:
            fee_buffer = self.config.FEE_RATE * 2
            breakeven_sl = self.order_mgr._round_to_tick(
                self.entry_price * (1 + fee_buffer), current_price
            )
            if self.order_mgr.active_sl_order and breakeven_sl > self.order_mgr.active_sl_order["price"]:
                self._breakeven_activated = True
                print(
                    f"  🛡️ 본전 보호 활성화! (수익 {pnl_pct*100:.2f}% ≥ {breakeven_trigger*100:.1f}%) "
                    f"SL → {fmt_price(breakeven_sl)}"
                )
                # 기존 SL 주문을 본전 스탑으로 교체
                new_tp = self.order_mgr.active_tp_order["price"] if self.order_mgr.active_tp_order else \
                         self.order_mgr._round_to_tick(
                             self.entry_price * (1 + self.config.TAKE_PROFIT_PCT), current_price
                         )
                pre_check = self.order_mgr.check_sell_orders(market)
                if pre_check["filled"]:
                    self._on_sell_filled(pre_check)
                    return
                self.order_mgr.update_exit_prices(market, new_tp, breakeven_sl, self.coin_qty)
                self._save_state()

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

        # ── 포지션 보유 중 하락장 전환 감지 → SL 강화 ──
        trend = self._check_trend_filter(market)
        if not trend["allowed"] and trend["regime"] == "bear":
            logger.warning(f"포지션 보유 중 하락장 전환 감지: {trend['reason']}")
            if pnl_pct > 0:
                # 수익 중: 현재가 기준 타이트한 손절 (수익 보호 최우선)
                tight_sl = current_price * (1 - self.config.TRAILING_STOP_PCT)
                tight_sl = self.order_mgr._round_to_tick(tight_sl, current_price)
                # 익절도 당기기 (TP = 현재가 기준 절반 남은 수익)
                tight_tp = current_price * (1 + self.config.TAKE_PROFIT_PCT * 0.5)
                tight_tp = self.order_mgr._round_to_tick(tight_tp, current_price)
                if self.order_mgr.active_sl_order and tight_sl > self.order_mgr.active_sl_order["price"]:
                    print(
                        f"  📉 하락장 전환 → 수익 보호 SL 강화: "
                        f"{fmt_price(self.order_mgr.active_sl_order['price'])} → {fmt_price(tight_sl)}"
                    )
                    pre_check = self.order_mgr.check_sell_orders(market)
                    if pre_check["filled"]:
                        self._on_sell_filled(pre_check)
                        return
                    self.order_mgr.update_exit_prices(market, tight_tp, tight_sl, self.coin_qty)
                    self._save_state()
            else:
                # 손실 중: 하락장 전환 시 즉시 시장가 청산 (추가 손실 방지)
                print(
                    f"  📉 하락장 전환 + 손실 포지션 → 즉시 시장가 청산 "
                    f"(현재 손익: {pnl_pct*100:+.2f}%)"
                )
                self.order_mgr.cancel_sell_orders()
                order = self.client.sell_market_order(market, self.coin_qty)
                if order:
                    exit_price = order.get("price", current_price)
                    fee = order.get("fee", 0)
                    if self.config.PAPER_TRADING:
                        self.paper_capital += order.get("revenue", self.coin_qty * current_price)
                    self._record_sell(exit_price, fee, "하락장 전환 긴급 청산")
                return

        # ── 위험 신호 감지: 즉시 시장가 청산 ──
        # MACD 데드크로스 + RSI 급락 + 거래량 급증 = 급락 신호
        macd_dead_cross = row["macd_hist"] < 0 and row["macd"] > 0
        rsi_falling = row["rsi"] > self.config.RSI_OVERBOUGHT
        volume_spike = row["volume_ratio"] > 3.0

        danger_count = sum([macd_dead_cross, rsi_falling, volume_spike])
        min_exit_pct = getattr(self.config, "TECHNICAL_EXIT_MIN_PCT", 0.0)
        # 손실 중이거나 충분한 수익(min_exit_pct 이상)일 때만 기술적 신호 청산 허용
        tech_exit_allowed = pnl_pct < 0 or pnl_pct >= min_exit_pct
        if danger_count >= 2 and tech_exit_allowed:
            print(f"  ⚠️ 위험 신호 {danger_count}개 감지 (손익 {pnl_pct*100:+.2f}%) → 시장가 즉시 청산")
            self.order_mgr.cancel_sell_orders()
            order = self.client.sell_market_order(market, self.coin_qty)
            if order:
                exit_price = order.get("price", current_price)
                fee = order.get("fee", 0)
                if self.config.PAPER_TRADING:
                    self.paper_capital += order.get("revenue", self.coin_qty * current_price)
                self._record_sell(exit_price, fee, "위험신호 시장가 청산")

    def _on_dca_filled(self):
        """DCA 2차 매수 체결 처리 — 평균 단가 재계산 + TP/SL 재설정"""
        order = self.order_mgr.active_buy_order
        if order is None:
            return

        dca_price = order["price"]
        dca_qty = order["volume"]
        dca_fee = order["fee"]

        # 평균 단가 = (1차 금액 + 2차 금액) / 총 수량
        total_cost = self.entry_price * self.coin_qty + dca_price * dca_qty
        self.coin_qty += dca_qty
        self._avg_entry_price = total_cost / self.coin_qty
        self.entry_price = self._avg_entry_price  # PnL 계산 기준도 평균단가로

        if self.config.PAPER_TRADING:
            self.paper_capital -= (self._dca_current_amount + dca_fee)

        # 다음 DCA 레벨로 이동
        if self._dca_levels_pending:
            self._dca_levels_pending.pop(0)
        if not self._dca_levels_pending:
            self._dca_done = True

        # TP/SL을 평균단가 기준으로 재설정
        new_tp = self.order_mgr._round_to_tick(
            self.entry_price * (1 + self.config.TAKE_PROFIT_PCT), self.entry_price)
        new_sl = self.order_mgr._round_to_tick(
            self.entry_price * (1 - self.config.STOP_LOSS_PCT), self.entry_price)
        self.order_mgr.cancel_sell_orders()
        self.order_mgr.place_limit_sell(self.current_market, new_tp, self.coin_qty, "tp")
        self.order_mgr.place_limit_sell(self.current_market, new_sl, self.coin_qty, "sl")
        self.order_mgr.active_buy_order = None
        self._dca_order_pending = False
        self._breakeven_activated = False
        remaining = len(self._dca_levels_pending)
        self._save_state()
        print(
            f"  ✅ 물타기 체결! | 평균단가={fmt_price(self.entry_price)} | "
            f"총수량={self.coin_qty:.8f} | TP={fmt_price(new_tp)} SL={fmt_price(new_sl)} | "
            f"남은단계={remaining}"
        )

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

        buy_value = self.entry_price * self.coin_qty
        sell_value = exit_price * self.coin_qty - fee
        pnl_krw = sell_value - buy_value - (buy_value * self.config.FEE_RATE)
        pnl_pct = pnl_krw / buy_value * 100 if buy_value else 0.0
        _bdb.record_sell(
            session_id=self._db_session_id,
            market=market,
            entry_price=self.entry_price,
            exit_price=exit_price,
            coin_qty=self.coin_qty,
            fee=fee,
            pnl_krw=pnl_krw,
            pnl_pct=pnl_pct,
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
        if not self.config.PAPER_TRADING:
            try:
                _live_balances = self.client.upbit.get_balances()
                _krw_live = next((float(b['balance']) for b in (_live_balances or []) if b['currency'] == 'KRW'), self.paper_capital)
            except Exception:
                _krw_live = self.paper_capital
        else:
            _krw_live = self.paper_capital
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='sell',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=_krw_live,
            note=f'{reason}',
        )

        # 상태 초기화
        self.state = STATE_IDLE
        self.current_market = None
        self.entry_price = 0.0
        self.coin_qty = 0.0
        self.highest_price = 0.0
        self.order_mgr.clear_all()

        # DCA / 피라미딩 변수 초기화
        self._dca_levels_pending = []
        self._dca_current_amount = 0.0
        self._dca_done = False
        self._dca_order_pending = False
        self._dca_timeout_at = None
        self._avg_entry_price = 0.0
        self._breakeven_activated = False
        self._pyramid_done = False
        self._pyramid_order_pending = False
        self._pyramid_amount = 0.0

        # 포지션 정리 완료 — 상태 파일 삭제
        self._clear_state()

        self.trade_logger.print_summary()

    # ──────────────────────────────────────────
    # 상태 영속화 (비정상 종료 복구)
    # ──────────────────────────────────────────

    def _save_state(self):
        """현재 포지션 정보를 파일에 저장합니다. 비정상 종료 후 재시작 시 복구에 사용됩니다."""
        if self.state not in (STATE_POSITION, STATE_BUY_WAITING):
            return

        state_data = {
            "state": self.state,
            "market": self.current_market,
            "entry_price": self.entry_price,
            "coin_qty": self.coin_qty,
            "highest_price": self.highest_price,
            "paper_capital": self.paper_capital,
            "saved_at": datetime.now().isoformat(),
        }
        if self.order_mgr.active_tp_order:
            state_data["tp_price"] = self.order_mgr.active_tp_order["price"]
        if self.order_mgr.active_sl_order:
            state_data["sl_price"] = self.order_mgr.active_sl_order["price"]
        if self.order_mgr.active_buy_order:
            state_data["buy_price"] = self.order_mgr.active_buy_order["price"]

        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)
            logger.debug(f"봇 상태 저장 완료: {self.state} | {self.current_market}")
        except Exception as e:
            logger.warning(f"상태 파일 저장 실패: {e}")

    def _calc_trade_amount(self) -> float:
        """잔고 비례 투자금 계산.
        TRADE_AMOUNT_PCT > 0 이면 현재 자금의 N%, 아니면 고정 TRADE_AMOUNT_KRW 사용.
        """
        pct = getattr(self.config, "TRADE_AMOUNT_PCT", 0)
        if pct > 0:
            if self.config.PAPER_TRADING:
                capital = self.paper_capital
            else:
                try:
                    bal = self.client.upbit.get_balances()
                    capital = next((float(b["balance"]) for b in (bal or []) if b["currency"] == "KRW"), self.paper_capital)
                except Exception:
                    capital = self.paper_capital
            amount = capital * pct
            min_a = getattr(self.config, "TRADE_AMOUNT_MIN", 10_000)
            max_a = getattr(self.config, "TRADE_AMOUNT_MAX", 500_000)
            return max(min_a, min(amount, max_a))
        return float(self.config.TRADE_AMOUNT_KRW)

    def _clear_state(self):
        """상태 파일을 삭제합니다."""
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception as e:
            logger.warning(f"상태 파일 삭제 실패: {e}")

    def _write_live_status(self):
        """대시보드에서 읽을 실시간 현황을 live_status.json에 기록합니다 (매 사이클)."""
        try:
            status: dict = {
                "state": self.state,
                "mode": "live" if not self.config.PAPER_TRADING else "paper",
                "paper_capital": self.paper_capital,
                "last_updated": datetime.now().isoformat(),
            }

            if self.state in (STATE_BUY_WAITING, STATE_POSITION):
                status["market"] = self.current_market

            if self.state == STATE_BUY_WAITING:
                buy_order = self.order_mgr.active_buy_order
                if buy_order:
                    status["pending_buy_price"] = buy_order.get("price", 0)
                    status["pending_buy_amount"] = self._pending_trade_amount

            if self.state == STATE_POSITION and self.entry_price > 0:
                current_price = self.client.get_current_price(self.current_market) or self.entry_price
                avg_price = self._avg_entry_price if self._avg_entry_price > 0 else self.entry_price
                unrealized_pct = (current_price - avg_price) / avg_price * 100
                unrealized_krw = (current_price - avg_price) * self.coin_qty

                status.update({
                    "entry_price": self.entry_price,
                    "avg_entry_price": avg_price,
                    "coin_qty": self.coin_qty,
                    "current_price": current_price,
                    "highest_price": self.highest_price,
                    "unrealized_pct": round(unrealized_pct, 4),
                    "unrealized_krw": round(unrealized_krw, 0),
                    "position_value_krw": round(current_price * self.coin_qty, 0),
                    "dca_done": self._dca_done,
                    "breakeven_activated": self._breakeven_activated,
                })
                tp_order = self.order_mgr.active_tp_order
                sl_order = self.order_mgr.active_sl_order
                if tp_order:
                    status["tp_price"] = tp_order.get("price", 0)
                if sl_order:
                    status["sl_price"] = sl_order.get("price", 0)

            with open(self._live_status_file, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"live_status 기록 실패 (무시): {e}")

    def _clear_live_status(self):
        """봇 종료 시 live_status.json에 종료 상태를 기록합니다."""
        try:
            status = {
                "state": "stopped",
                "mode": "live" if not self.config.PAPER_TRADING else "paper",
                "paper_capital": self.paper_capital,
                "last_updated": datetime.now().isoformat(),
            }
            with open(self._live_status_file, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _try_recover_state(self):
        """봇 시작 시 이전 상태 파일이 있으면 복구를 시도합니다."""
        if not self._state_file.exists():
            return

        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
        except Exception as e:
            logger.warning(f"상태 파일 읽기 실패: {e}")
            self._clear_state()
            return

        saved_state = state_data.get("state")
        market = state_data.get("market", "UNKNOWN")
        entry_price = state_data.get("entry_price", 0.0)
        coin_qty = state_data.get("coin_qty", 0.0)
        saved_at = state_data.get("saved_at", "unknown")

        print(f"\n{'='*60}")
        print(f"  ⚠️  이전 실행 상태 파일 감지! (저장: {saved_at})")
        print(f"  상태: {saved_state} | 마켓: {market}")
        print(f"  진입가: {fmt_price(entry_price)} | 수량: {coin_qty:.8f}")

        if saved_state == STATE_POSITION and market and market != "UNKNOWN":
            tp_price = state_data.get("tp_price", 0.0)
            sl_price = state_data.get("sl_price", 0.0)
            if tp_price:
                print(f"  TP: {fmt_price(tp_price)} | SL: {fmt_price(sl_price)}")
            print(f"{'='*60}")
            answer = self._input_with_timeout(
                "  복구 방법 선택 (y=포지션복구 / s=즉시청산 / n=무시, 30초 대기): ",
                timeout=30.0,
                default="n",
            )

            if answer == "y":
                # 포지션 상태 복구
                self.state = STATE_POSITION
                self.current_market = market
                self.entry_price = entry_price
                self.coin_qty = coin_qty
                self.highest_price = state_data.get("highest_price", entry_price)
                if self.config.PAPER_TRADING:
                    self.paper_capital = state_data.get("paper_capital", self.paper_capital)
                # TP/SL 매도 주문 복원
                if tp_price:
                    self.order_mgr.place_limit_sell(market, tp_price, coin_qty, "tp")
                if sl_price:
                    self.order_mgr.place_limit_sell(market, sl_price, coin_qty, "sl")
                self.adjust_counter = 0
                logger.info(f"포지션 복구 완료: {market} @ {fmt_price(entry_price)}")
                print(f"  ✅ 포지션 복구 완료 — 모니터링 재개")

            elif answer == "s":
                # 즉시 시장가 청산
                print("  📤 즉시 시장가 청산 실행 중...")
                self.current_market = market
                self.entry_price = entry_price
                self.coin_qty = coin_qty
                current = self.client.get_current_price(market)
                if current and coin_qty > 0:
                    order = self.client.sell_market_order(market, coin_qty)
                    if order:
                        exit_price = order.get("price", current)
                        fee = order.get("fee", 0.0)
                        if self.config.PAPER_TRADING:
                            self.paper_capital += order.get(
                                "revenue", coin_qty * current
                            )
                        self._record_sell(exit_price, fee, "재시작 후 즉시 청산")
                        return  # _record_sell 내부에서 _clear_state() 호출됨
                self._clear_state()

            else:
                print("  상태 파일 무시 — IDLE 상태로 시작합니다.")
                print("  ⚠️ 업비트 앱에서 미체결 주문을 직접 확인하세요.")
                self._clear_state()

        elif saved_state == STATE_BUY_WAITING:
            print(f"{'='*60}")
            print("  매수 주문 대기 중이었습니다.")
            print("  ⚠️ 업비트 앱에서 미체결 매수 주문을 직접 확인/취소하세요.")
            self._input_with_timeout("  확인 후 Enter (30초 대기): ", timeout=30.0, default="")
            self._clear_state()

        else:
            self._clear_state()

    def _emergency_shutdown(self):
        """atexit 등 예상치 못한 종료 시 포지션 정보를 파일에 저장합니다."""
        if self.state in (STATE_POSITION, STATE_BUY_WAITING):
            logger.warning(
                f"예상치 못한 종료 감지 (상태={self.state}) → 상태 파일 저장"
            )
            self._save_state()
        self._clear_live_status()

    # ──────────────────────────────────────────

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
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='shutdown',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=self.paper_capital,
            note='봇 종료',
        )
        _bdb.end_trading_session(
            self._db_session_id,
            self.trade_logger.performance,
            self.paper_capital,
        )
        self._clear_live_status()
