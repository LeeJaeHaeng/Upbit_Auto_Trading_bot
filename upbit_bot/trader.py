"""
메인 트레이더 모듈 (멀티포지션)

지정가 주문 기반 매매 흐름:
1. 마켓 스캔 → 최적 종목 선택 (이미 보유 중인 종목 제외)
2. 차트 분석 → 최적 진입가 산출 → 지정가 매수 주문 설정
3. 매수 체결 대기 (타임아웃 시 자동 취소 → 재스캔)
4. 체결 → 즉시 익절/손절 지정가 매도 주문 설정
5. 차트 지속 분석 → 익절/손절 가격 동적 조정
6. 매도 체결 → 수익 기록 → 다음 기회 탐색

여러 포지션을 동시에 보유하고 각각 독립적으로 관리합니다.
동시 보유 한도: config.MAX_POSITIONS (기본 3)
"""

import atexit
import json
import time
import logging
import threading
from datetime import datetime, timedelta
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


class Trader:
    def __init__(self, config):
        self.config = config
        self.client = UpbitClient(
            config.ACCESS_KEY,
            config.SECRET_KEY,
            paper_trading=config.PAPER_TRADING,
        )
        self.scanner = MarketScanner(config)
        self.market_env = MarketEnvironment()
        self.trade_logger = TradeLogger(config.LOG_FILE, config.PERFORMANCE_FILE)

        # ── 멀티포지션 상태 ──
        # positions: {market: pos_dict}  — 체결된 포지션
        # pending_buys: {market: pb_dict} — 매수 주문 대기 중
        self.positions: dict[str, dict] = {}
        self.pending_buys: dict[str, dict] = {}

        # 페이퍼 자금 — 이전 세션 잔고 자동 복원
        if config.PAPER_TRADING:
            self.paper_capital = _bdb.get_last_paper_capital(fallback=1_000_000)
        else:
            self.paper_capital = 1_000_000

        # DB 세션 ID
        mode = 'paper' if config.PAPER_TRADING else 'live'
        self._db_session_id = _bdb.start_trading_session(mode, config)
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='startup', mode=mode,
            krw_balance=self.paper_capital,
            note=f'봇 시작 (이전잔고 복원: {self.paper_capital:,.0f}원)',
        )

        # 연속 오류 카운터
        self._consecutive_errors = 0
        self._max_consecutive_errors = 10

        # 파일 경로
        self._state_file = Path(config.BASE_DIR) / "bot_state.json"
        self._live_status_file = Path(config.BASE_DIR) / "live_status.json"

    # ──────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────

    def run(self):
        atexit.register(self._emergency_shutdown)

        max_pos = getattr(self.config, 'MAX_POSITIONS', 3)
        mode = "📄 페이퍼 트레이딩" if self.config.PAPER_TRADING else "💰 실거래"
        print(f"\n{'='*60}")
        print(f"  🤖 업비트 AI 자동매매 봇 시작 ({mode})")
        print(f"  주문 방식: 지정가 (최적 진입가 분석)")
        print(f"  체크 주기: {self.config.CHECK_INTERVAL}초")
        print(f"  진입 최소 신호: {self.config.MIN_SIGNAL_COUNT}개 이상")
        print(f"  손절: {self.config.STOP_LOSS_PCT*100:.1f}% | 익절: {self.config.TAKE_PROFIT_PCT*100:.1f}%")
        print(f"  최대 동시 포지션: {max_pos}개")
        if self.config.PAPER_TRADING:
            print(f"  💰 모의잔고: {self.paper_capital:,.0f}원 (이전 세션에서 복원)")
        print(f"{'='*60}\n")

        self._try_recover_state()

        while True:
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # 1. 매수 대기 중인 모든 주문 체결 확인
                for mkt in list(self.pending_buys.keys()):
                    self._handle_pending_buy(mkt, now)

                # 2. 보유 중인 모든 포지션 관리
                for mkt in list(self.positions.keys()):
                    self._handle_position(mkt, now)

                # 3. 여유 슬롯 있으면 신규 진입 시도
                total_active = len(self.positions) + len(self.pending_buys)
                if total_active < max_pos:
                    self._handle_idle(now)
                else:
                    print(
                        f"[{now}] 📊 포지션 {len(self.positions)}개 + 대기 {len(self.pending_buys)}개 "
                        f"/ 최대 {max_pos}개 — 신규 진입 보류"
                    )

                self._consecutive_errors = 0
                self._write_live_status()
                time.sleep(self.config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n⛔ 봇 종료 요청 수신")
                self._shutdown()
                break
            except Exception as e:
                self._consecutive_errors += 1
                wait_sec = min(30 * self._consecutive_errors, 300)
                logger.error(
                    f"루프 오류 (연속 {self._consecutive_errors}회): {e} "
                    f"— {wait_sec}초 후 재시도",
                    exc_info=True,
                )
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.critical(f"연속 오류 {self._max_consecutive_errors}회 초과 → 긴급 종료")
                    self._emergency_shutdown()
                    break
                time.sleep(wait_sec)

    # ──────────────────────────────────────────
    # 시장 국면 / MTF 필터
    # ──────────────────────────────────────────

    def _check_mtf_trend(self, market: str) -> dict:
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
            ema_long  = getattr(self.config, "MTF_EMA_LONG",  50)
            mtf_ema_s = float(closes.ewm(span=ema_short, adjust=False).mean().iloc[-1])
            mtf_ema_l = float(closes.ewm(span=ema_long,  adjust=False).mean().iloc[-1])
            if mtf_ema_s >= mtf_ema_l:
                return {"allowed": True,  "reason": f"4h 상승 추세 (EMA{ema_short}={mtf_ema_s:,.0f} >= EMA{ema_long}={mtf_ema_l:,.0f})"}
            return {"allowed": False, "reason": f"4h 하락 추세 (EMA{ema_short}={mtf_ema_s:,.0f} < EMA{ema_long}={mtf_ema_l:,.0f}) → 진입 차단"}
        except Exception as e:
            logger.warning(f"MTF 체크 오류: {e} → 스킵")
            return {"allowed": True, "reason": f"MTF 오류: {e}"}

    def _check_trend_filter(self, market: str) -> dict:
        if not getattr(self.config, "USE_TREND_FILTER", True):
            return {"allowed": True, "regime": "unknown", "reason": "필터 비활성화"}
        try:
            df = pyupbit.get_ohlcv(market, interval=f"minute{self.config.CANDLE_UNIT}", count=220)
            if df is None or len(df) < 60:
                if not getattr(self.config, "PAPER_TRADING", True):
                    return {"allowed": False, "regime": "unknown", "reason": "데이터 부족 — 실거래 안전 차단"}
                return {"allowed": True, "regime": "unknown", "reason": "데이터 부족 — 필터 스킵 (페이퍼)"}
            df.columns = ["open", "high", "low", "close", "volume", "value"]
            closes = df["close"]
            ema50  = float(closes.ewm(span=50,  adjust=False).mean().iloc[-1])
            ema200 = float(closes.ewm(span=200, adjust=False).mean().iloc[-1])
            close  = float(closes.iloc[-1])
            strict = getattr(self.config, "TREND_FILTER_STRICT", False)
            price_below_ema200 = close < ema200
            ema50_below_ema200 = ema50  < ema200
            is_bear = ema50_below_ema200 if strict else (price_below_ema200 and ema50_below_ema200)
            if is_bear:
                return {"allowed": False, "regime": "bear",
                        "reason": f"하락장 감지 (현재가 {close:,.0f} < EMA200 {ema200:,.0f}, EMA50 {ema50:,.0f} < EMA200)",
                        "ema50": ema50, "ema200": ema200, "close": close}
            if price_below_ema200:
                return {"allowed": True, "regime": "sideways",
                        "reason": "횡보장 (현재가 < EMA200이나 EMA50 >= EMA200) → 진입 허용",
                        "ema50": ema50, "ema200": ema200, "close": close}
            return {"allowed": True, "regime": "bull",
                    "reason": f"상승장 (현재가 {close:,.0f} > EMA200 {ema200:,.0f}) → 진입 허용",
                    "ema50": ema50, "ema200": ema200, "close": close}
        except Exception as e:
            logger.warning(f"국면 필터 오류 ({e}) → 필터 스킵")
            return {"allowed": True, "regime": "unknown", "reason": f"오류 발생: {e}"}

    # ──────────────────────────────────────────
    # IDLE: 신규 진입 탐색
    # ──────────────────────────────────────────

    def _handle_idle(self, now: str):
        print(f"\n[{now}] 🔍 마켓 스캔 & 진입 기회 탐색...")

        best_market = self.scanner.select_best_market()
        if best_market is None:
            print(f"[{now}] ⏸️  진입 조건 충족 마켓 없음. 다음 사이클 대기...")
            return

        # 이미 보유 or 대기 중인 마켓은 건너뜀
        if best_market in self.positions or best_market in self.pending_buys:
            print(f"  ⏭️  {best_market} 이미 포지션/대기 중 → 건너뜀")
            return

        regime_emoji = {"bull": "📈", "bear": "📉", "sideways": "↔️", "unknown": "❓"}

        trend = self._check_trend_filter(best_market)
        print(f"  {regime_emoji.get(trend['regime'], '❓')} 시장 국면: {trend['reason']}")
        if not trend["allowed"]:
            print(f"  ⛔ 하락장 진입 차단 → 다음 사이클 대기")
            return

        mtf = self._check_mtf_trend(best_market)
        print(f"  📊 4h 추세: {mtf['reason']}")
        if not mtf["allowed"]:
            print(f"  ⛔ 4h 하락 추세 → 다음 사이클 대기")
            return

        self.market_env.print_market_environment(best_market)
        env_score = self.market_env.get_market_score(best_market)
        if env_score["score"] < -30:
            print(f"  ⛔ 시장 환경 불리 ({env_score['score']}/100) → 진입 보류")
            return

        # 이 진입 전용 OrderManager 생성
        order_mgr = OrderManager(self.client, self.config)
        entry_info = order_mgr.calculate_optimal_entry_price(best_market)
        if entry_info is None:
            print(f"  ❌ {best_market} 진입가 분석 실패")
            return

        tp_pct = entry_info["tp_pct"]
        sl_pct = abs(entry_info["sl_pct"])
        rr_ratio = tp_pct / sl_pct if sl_pct > 0 else 0
        min_rr = getattr(self.config, "MIN_RR_RATIO", 1.5)
        if rr_ratio < min_rr:
            print(f"  ⛔ R/R 비율 불충분 ({rr_ratio:.1f}x < {min_rr}x) → 다음 사이클 대기")
            return

        print(f"\n  📊 진입가 분석 결과:")
        print(f"    현재가      : {fmt_price(entry_info['current_price']):>12}원")
        print(f"    진입가      : {fmt_price(entry_info['entry_price']):>12}원 ({entry_info['method']})")
        print(f"    할인율      : {entry_info['discount_pct']:>12.2f}%")
        print(f"    익절 목표   : {fmt_price(entry_info['tp_price']):>12}원 ({entry_info['tp_pct']:+.2f}%)")
        print(f"    손절 기준   : {fmt_price(entry_info['sl_price']):>12}원 ({entry_info['sl_pct']:+.2f}%)")
        print(f"    R/R 비율    : {rr_ratio:>12.2f}x")

        trade_amount = self._calc_trade_amount()
        if self.config.PAPER_TRADING:
            trade_amount = min(trade_amount, self.paper_capital * 0.95)

        if getattr(self.config, "SCALED_ENTRY", False):
            first_ratio = getattr(self.config, "SCALED_ENTRY_1ST_RATIO", 0.5)
            first_amount = trade_amount * first_ratio
        else:
            first_amount = trade_amount

        if first_amount < 5000:
            logger.warning("매수 금액이 최소 주문금액(5,000원) 미만")
            return

        order = order_mgr.place_limit_buy(best_market, entry_info["entry_price"], first_amount)
        if order is None:
            return

        self.pending_buys[best_market] = {
            "market":               best_market,
            "order_mgr":            order_mgr,
            "entry_info":           entry_info,
            "trade_amount":         trade_amount,
            "trend_check_counter":  0,
        }
        print(f"\n  📝 지정가 매수 주문 설정 완료 [{best_market}] → 체결 대기 중...")

    # ──────────────────────────────────────────
    # BUY_WAITING: 매수 체결 대기
    # ──────────────────────────────────────────

    def _handle_pending_buy(self, market: str, now: str):
        pb = self.pending_buys.get(market)
        if pb is None:
            return
        order_mgr = pb["order_mgr"]
        order = order_mgr.active_buy_order
        if order is None:
            del self.pending_buys[market]
            return

        order_price   = order["price"]
        current_price = self.client.get_current_price(market)
        if current_price is None:
            return

        diff_pct = (current_price - order_price) / order_price * 100
        print(
            f"[{now}] ⏳ 매수 대기 | {market} | "
            f"주문가={fmt_price(order_price)} | 현재가={fmt_price(current_price)} ({diff_pct:+.2f}%)"
        )

        if order_mgr.check_buy_order_filled(market):
            self._on_buy_filled(market)
            return

        if order_mgr.check_buy_timeout():
            print(f"  ⏱️  매수 주문 타임아웃 [{market}] → 주문 취소 → 재스캔")
            order_mgr.cancel_buy_order(market)
            del self.pending_buys[market]
            return

        if diff_pct > 2.0:
            print(f"  📈 가격이 주문가 대비 {diff_pct:.1f}% 상승 [{market}] → 주문 취소 → 재분석")
            order_mgr.cancel_buy_order(market)
            del self.pending_buys[market]
            return

        pb["trend_check_counter"] += 1
        if pb["trend_check_counter"] % 3 == 0:
            trend = self._check_trend_filter(market)
            if not trend["allowed"]:
                print(f"  📉 매수 대기 중 하락장 전환 [{market}] → 주문 즉시 취소")
                order_mgr.cancel_buy_order(market)
                del self.pending_buys[market]

    def _on_buy_filled(self, market: str):
        pb = self.pending_buys.pop(market, None)
        if pb is None:
            return
        order_mgr  = pb["order_mgr"]
        entry_info = pb["entry_info"]
        order = order_mgr.active_buy_order
        if order is None or entry_info is None:
            logger.warning(f"매수 체결 처리 중 주문/진입정보 누락 [{market}]")
            return

        entry_price = order["price"]
        coin_qty    = order["volume"]
        fee         = order["fee"]
        trade_amount = pb["trade_amount"]
        actual_amount = order.get("amount_krw", trade_amount)

        if self.config.PAPER_TRADING:
            self.paper_capital -= (actual_amount + fee)

        # 신호 점수 기록
        df = pyupbit.get_ohlcv(market, interval=f"minute{self.config.CANDLE_UNIT}", count=self.config.CANDLE_COUNT)
        signal_score, signals, _ind_row = 0, {}, {}
        if df is not None and not df.empty:
            df.columns = ["open", "high", "low", "close", "volume", "value"]
            df = add_all_indicators(df, self.config)
            df = df.dropna()
            if not df.empty:
                result = get_signal_score(df.iloc[-1], self.config)
                signal_score = result["score"]
                signals      = result["signals"]
                _ind_row     = df.iloc[-1].to_dict()

        self.trade_logger.log_buy(
            market=market, price=entry_price, amount_krw=trade_amount,
            coin_qty=coin_qty, fee=fee, signal_score=signal_score, signals=signals,
        )
        _bdb.record_buy(
            session_id=self._db_session_id, market=market, price=entry_price,
            amount_krw=trade_amount, coin_qty=coin_qty, fee=fee,
            signal_score=signal_score, signals=signals, indicators=_ind_row,
        )
        print(f"\n🟢 매수 체결! | {market} | 가격={fmt_price(entry_price)} | 수량={coin_qty:.8f}")
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='buy',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=self.paper_capital,
            coin_market=market, coin_qty=coin_qty,
            coin_value_krw=entry_price * coin_qty,
            note=f'매수 {market} {entry_price:,.0f}원',
        )

        # TP/SL 매도 주문 즉시 설정
        tp_price = entry_info["tp_price"]
        sl_price = entry_info["sl_price"]
        order_mgr.place_limit_sell(market, tp_price, coin_qty, "tp")
        order_mgr.place_limit_sell(market, sl_price, coin_qty, "sl")
        print(f"  📤 익절 주문: {fmt_price(tp_price)} | 손절 주문: {fmt_price(sl_price)}")

        # DCA 단계 초기화
        dca_levels_pending = []
        dca_timeout_at = None
        if getattr(self.config, "SCALED_ENTRY", False):
            dca_levels = getattr(self.config, "DCA_LEVELS", [(0.015, 0.25), (0.030, 0.25)])
            timeout_min = getattr(self.config, "SCALED_ENTRY_TIMEOUT_MIN", 60)
            dca_timeout_at = datetime.now() + timedelta(minutes=timeout_min)
            for dip_pct, add_ratio in dca_levels:
                trigger_p  = entry_price * (1 - dip_pct)
                add_amount = trade_amount * add_ratio
                dca_levels_pending.append((trigger_p, add_amount))
            msgs = [f"{(entry_price - p) / entry_price * 100:.1f}%↓={fmt_price(p)}" for (p, _) in dca_levels_pending]
            print(f"  💧 물타기 설정 ({len(dca_levels_pending)}단계): {' / '.join(msgs)} | 만료={timeout_min}분")

        pyramid_amount = 0.0
        if getattr(self.config, "PYRAMID_ENABLED", False):
            ratio = getattr(self.config, "PYRAMID_ADD_RATIO", 0.5)
            pyramid_amount = trade_amount * ratio
            trig = getattr(self.config, "PYRAMID_TRIGGER_PCT", 0.015) * 100
            print(f"  🔥 불타기 설정: +{trig:.1f}% 도달 시 {pyramid_amount:,.0f}원 추가 매수")

        # 포지션 등록
        self.positions[market] = {
            "market":               market,
            "order_mgr":            order_mgr,
            "entry_price":          entry_price,
            "coin_qty":             coin_qty,
            "highest_price":        entry_price,
            "avg_entry_price":      entry_price,
            "entry_signal_score":   signal_score,
            "trade_amount":         trade_amount,
            "adjust_counter":       0,
            # DCA
            "dca_levels_pending":   dca_levels_pending,
            "dca_order_pending":    False,
            "dca_current_amount":   0.0,
            "dca_timeout_at":       dca_timeout_at,
            "dca_done":             False,
            # Pyramid
            "pyramid_done":         False,
            "pyramid_order_pending": False,
            "pyramid_amount":       pyramid_amount,
            # Other
            "breakeven_activated":  False,
            "trend_check_counter":  0,
        }
        self._save_state()

    # ──────────────────────────────────────────
    # POSITION: 포지션 관리
    # ──────────────────────────────────────────

    def _handle_position(self, market: str, now: str):
        pos = self.positions.get(market)
        if pos is None:
            return
        order_mgr = pos["order_mgr"]

        current_price = self.client.get_current_price(market)
        if current_price is None:
            return

        pos["highest_price"] = max(pos["highest_price"], current_price)
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

        tp_str = fmt_price(order_mgr.active_tp_order['price']) if order_mgr.active_tp_order else "-"
        sl_str = fmt_price(order_mgr.active_sl_order['price']) if order_mgr.active_sl_order else "-"
        print(
            f"[{now}] 📊 포지션 | {market} | "
            f"진입={fmt_price(pos['entry_price'])} | 현재={fmt_price(current_price)} ({pnl_pct:+.2f}%) | "
            f"TP={tp_str} SL={sl_str}"
        )

        # 익절/손절 체결 확인
        sell_result = order_mgr.check_sell_orders(market)
        if sell_result["filled"]:
            self._on_sell_filled(market, sell_result)
            return

        # 물타기 DCA
        if pos["dca_order_pending"] and order_mgr.active_buy_order is not None:
            if order_mgr.check_buy_order_filled(market):
                self._on_dca_filled(market)
        elif (
            not pos["dca_order_pending"]
            and pos["dca_levels_pending"]
            and order_mgr.active_buy_order is None
        ):
            if pos["dca_timeout_at"] and datetime.now() > pos["dca_timeout_at"]:
                logger.info(f"물타기 타임아웃 [{market}] → 현 포지션 확정")
                pos["dca_levels_pending"].clear()
                pos["dca_done"] = True
            else:
                next_trigger, next_amount = pos["dca_levels_pending"][0]
                if current_price <= next_trigger:
                    order = order_mgr.place_limit_buy(market, current_price, next_amount)
                    if order:
                        pos["dca_current_amount"] = next_amount
                        pos["dca_order_pending"]  = True
                        step = 2 + (len(getattr(self.config, "DCA_LEVELS", [])) - len(pos["dca_levels_pending"]))
                        print(f"  💧 물타기 {step}차 주문! 가격={fmt_price(current_price)} | 금액={next_amount:,.0f}원")

        # 불타기 피라미딩
        if (
            getattr(self.config, "PYRAMID_ENABLED", False)
            and not pos["pyramid_done"]
            and not pos["pyramid_order_pending"]
            and order_mgr.active_buy_order is None
            and pos["entry_price"] > 0
        ):
            trig = getattr(self.config, "PYRAMID_TRIGGER_PCT", 0.015)
            pnl_now = (current_price - pos["entry_price"]) / pos["entry_price"]
            if pnl_now >= trig:
                order = order_mgr.place_limit_buy(market, current_price, pos["pyramid_amount"])
                if order:
                    pos["pyramid_order_pending"] = True
                    print(f"  🔥 불타기! +{pnl_now*100:.2f}% | 가격={fmt_price(current_price)} | 금액={pos['pyramid_amount']:,.0f}원")
        if pos["pyramid_order_pending"] and order_mgr.active_buy_order is not None:
            if order_mgr.check_buy_order_filled(market):
                self._on_pyramid_filled(market)

        # 동적 TP/SL 조정 (매 5사이클)
        pos["adjust_counter"] += 1
        if pos["adjust_counter"] % 5 == 0:
            self._dynamic_adjust_exit(market, current_price)

    def _on_pyramid_filled(self, market: str):
        pos = self.positions.get(market)
        if pos is None:
            return
        order_mgr = pos["order_mgr"]
        order = order_mgr.active_buy_order
        if order is None:
            return

        p_price = order["price"]
        p_qty   = order["volume"]
        p_fee   = order["fee"]

        total_cost = pos["entry_price"] * pos["coin_qty"] + p_price * p_qty
        pos["coin_qty"] += p_qty
        pos["avg_entry_price"] = total_cost / pos["coin_qty"]
        pos["entry_price"] = pos["avg_entry_price"]

        if self.config.PAPER_TRADING:
            self.paper_capital -= (pos["pyramid_amount"] + p_fee)

        new_tp = order_mgr._round_to_tick(pos["entry_price"] * (1 + self.config.TAKE_PROFIT_PCT), pos["entry_price"])
        if getattr(self.config, "PYRAMID_SL_TO_ENTRY", True):
            new_sl = order_mgr._round_to_tick(pos["entry_price"] * (1 + self.config.FEE_RATE * 2), pos["entry_price"])
        else:
            new_sl = order_mgr._round_to_tick(pos["entry_price"] * (1 - self.config.STOP_LOSS_PCT), pos["entry_price"])

        order_mgr.cancel_sell_orders()
        order_mgr.place_limit_sell(market, new_tp, pos["coin_qty"], "tp")
        order_mgr.place_limit_sell(market, new_sl, pos["coin_qty"], "sl")
        order_mgr.active_buy_order = None
        pos["pyramid_done"]         = True
        pos["pyramid_order_pending"] = False
        pos["breakeven_activated"]  = False
        self._save_state()
        print(
            f"  🔥 불타기 체결! [{market}] | 평균단가={fmt_price(pos['entry_price'])} | "
            f"총수량={pos['coin_qty']:.8f} | TP={fmt_price(new_tp)} SL={fmt_price(new_sl)}"
        )

    def _on_dca_filled(self, market: str):
        pos = self.positions.get(market)
        if pos is None:
            return
        order_mgr = pos["order_mgr"]
        order = order_mgr.active_buy_order
        if order is None:
            return

        dca_price = order["price"]
        dca_qty   = order["volume"]
        dca_fee   = order["fee"]

        total_cost = pos["entry_price"] * pos["coin_qty"] + dca_price * dca_qty
        pos["coin_qty"] += dca_qty
        pos["avg_entry_price"] = total_cost / pos["coin_qty"]
        pos["entry_price"] = pos["avg_entry_price"]

        if self.config.PAPER_TRADING:
            self.paper_capital -= (pos["dca_current_amount"] + dca_fee)

        if pos["dca_levels_pending"]:
            pos["dca_levels_pending"].pop(0)
        if not pos["dca_levels_pending"]:
            pos["dca_done"] = True

        new_tp = order_mgr._round_to_tick(pos["entry_price"] * (1 + self.config.TAKE_PROFIT_PCT), pos["entry_price"])
        new_sl = order_mgr._round_to_tick(pos["entry_price"] * (1 - self.config.STOP_LOSS_PCT), pos["entry_price"])
        order_mgr.cancel_sell_orders()
        order_mgr.place_limit_sell(market, new_tp, pos["coin_qty"], "tp")
        order_mgr.place_limit_sell(market, new_sl, pos["coin_qty"], "sl")
        order_mgr.active_buy_order = None
        pos["dca_order_pending"]   = False
        pos["breakeven_activated"] = False
        self._save_state()
        print(
            f"  ✅ 물타기 체결! [{market}] | 평균단가={fmt_price(pos['entry_price'])} | "
            f"총수량={pos['coin_qty']:.8f} | TP={fmt_price(new_tp)} SL={fmt_price(new_sl)} | "
            f"남은단계={len(pos['dca_levels_pending'])}"
        )

    def _dynamic_adjust_exit(self, market: str, current_price: float):
        pos = self.positions.get(market)
        if pos is None:
            return
        order_mgr = pos["order_mgr"]

        df = pyupbit.get_ohlcv(market, interval=f"minute{self.config.CANDLE_UNIT}", count=self.config.CANDLE_COUNT)
        if df is None or df.empty:
            return
        df.columns = ["open", "high", "low", "close", "volume", "value"]
        df = add_all_indicators(df, self.config)
        df = df.dropna()
        if df.empty:
            return

        row     = df.iloc[-1]
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"]

        # 본전 보호 스탑
        breakeven_trigger = getattr(self.config, "BREAKEVEN_TRIGGER_PCT", 0.0)
        if breakeven_trigger > 0 and not pos["breakeven_activated"] and pnl_pct >= breakeven_trigger:
            fee_buffer    = self.config.FEE_RATE * 2
            breakeven_sl  = order_mgr._round_to_tick(pos["entry_price"] * (1 + fee_buffer), current_price)
            if order_mgr.active_sl_order and breakeven_sl > order_mgr.active_sl_order["price"]:
                pos["breakeven_activated"] = True
                print(f"  🛡️ 본전 보호 활성화! [{market}] (수익 {pnl_pct*100:.2f}%) SL → {fmt_price(breakeven_sl)}")
                new_tp = order_mgr.active_tp_order["price"] if order_mgr.active_tp_order else \
                         order_mgr._round_to_tick(pos["entry_price"] * (1 + self.config.TAKE_PROFIT_PCT), current_price)
                pre = order_mgr.check_sell_orders(market)
                if pre["filled"]:
                    self._on_sell_filled(market, pre)
                    return
                order_mgr.update_exit_prices(market, new_tp, breakeven_sl, pos["coin_qty"])
                self._save_state()

        # 트레일링 스탑
        if pnl_pct > 0.02:
            new_sl = pos["highest_price"] * (1 - self.config.TRAILING_STOP_PCT)
            if order_mgr.active_sl_order:
                old_sl = order_mgr.active_sl_order["price"]
                if new_sl > old_sl:
                    new_sl = order_mgr._round_to_tick(new_sl, current_price)
                    new_tp = max(
                        pos["entry_price"] * (1 + self.config.TAKE_PROFIT_PCT),
                        row["bb_upper"] * 0.998,
                    )
                    new_tp = order_mgr._round_to_tick(new_tp, current_price)
                    print(f"  📈 트레일링 스탑 조정 [{market}]: SL {fmt_price(old_sl)} → {fmt_price(new_sl)}")
                    pre = order_mgr.check_sell_orders(market)
                    if pre["filled"]:
                        self._on_sell_filled(market, pre)
                        return
                    order_mgr.update_exit_prices(market, new_tp, new_sl, pos["coin_qty"])

        # 하락장 전환 감지
        trend = self._check_trend_filter(market)
        if not trend["allowed"] and trend["regime"] == "bear":
            logger.warning(f"포지션 보유 중 하락장 전환 [{market}]: {trend['reason']}")
            if pnl_pct > 0:
                tight_sl = order_mgr._round_to_tick(current_price * (1 - self.config.TRAILING_STOP_PCT), current_price)
                tight_tp = order_mgr._round_to_tick(current_price * (1 + self.config.TAKE_PROFIT_PCT * 0.5), current_price)
                if order_mgr.active_sl_order and tight_sl > order_mgr.active_sl_order["price"]:
                    print(f"  📉 하락장 전환 → 수익 보호 SL 강화 [{market}]")
                    pre = order_mgr.check_sell_orders(market)
                    if pre["filled"]:
                        self._on_sell_filled(market, pre)
                        return
                    order_mgr.update_exit_prices(market, tight_tp, tight_sl, pos["coin_qty"])
                    self._save_state()
            else:
                print(f"  📉 하락장 전환 + 손실 포지션 [{market}] → 즉시 시장가 청산")
                order_mgr.cancel_sell_orders()
                order = self.client.sell_market_order(market, pos["coin_qty"])
                if order:
                    exit_price = order.get("price", current_price)
                    fee        = order.get("fee", 0)
                    if self.config.PAPER_TRADING:
                        self.paper_capital += order.get("revenue", pos["coin_qty"] * current_price)
                    self._record_sell(market, exit_price, fee, "하락장 전환 긴급 청산")
                return

        # 위험 신호 감지 → 즉시 시장가 청산
        macd_dead_cross = row["macd_hist"] < 0 and row["macd"] > 0
        rsi_falling     = row["rsi"] > self.config.RSI_OVERBOUGHT
        volume_spike    = row["volume_ratio"] > 3.0
        danger_count    = sum([macd_dead_cross, rsi_falling, volume_spike])
        min_exit_pct    = getattr(self.config, "TECHNICAL_EXIT_MIN_PCT", 0.0)
        tech_exit_ok    = pnl_pct < 0 or pnl_pct >= min_exit_pct
        if danger_count >= 2 and tech_exit_ok:
            print(f"  ⚠️ 위험 신호 {danger_count}개 [{market}] (손익 {pnl_pct*100:+.2f}%) → 시장가 즉시 청산")
            order_mgr.cancel_sell_orders()
            order = self.client.sell_market_order(market, pos["coin_qty"])
            if order:
                exit_price = order.get("price", current_price)
                fee        = order.get("fee", 0)
                if self.config.PAPER_TRADING:
                    self.paper_capital += order.get("revenue", pos["coin_qty"] * current_price)
                self._record_sell(market, exit_price, fee, "위험신호 시장가 청산")

    def _on_sell_filled(self, market: str, sell_result: dict):
        pos = self.positions.get(market)
        if pos is None:
            return
        exit_price = sell_result["price"]
        sell_type  = sell_result["type"]
        reason     = "익절" if sell_type == "tp" else "손절"
        fee        = exit_price * pos["coin_qty"] * self.config.FEE_RATE

        pos["order_mgr"].cancel_sell_orders()
        if self.config.PAPER_TRADING:
            revenue = pos["coin_qty"] * exit_price - fee
            self.paper_capital += revenue

        self._record_sell(market, exit_price, fee, f"지정가 {reason}")

    def _record_sell(self, market: str, exit_price: float, fee: float, reason: str):
        pos = self.positions.pop(market, None)
        if pos is None:
            logger.warning(f"매도 기록 시 포지션 없음 [{market}]")
            return

        entry_price = pos["entry_price"]
        coin_qty    = pos["coin_qty"]

        self.trade_logger.log_sell(
            market=market, entry_price=entry_price, exit_price=exit_price,
            coin_qty=coin_qty, fee=fee, reason=reason,
        )
        buy_value  = entry_price * coin_qty
        sell_value = exit_price * coin_qty - fee
        pnl_krw    = sell_value - buy_value - (buy_value * self.config.FEE_RATE)
        pnl_pct    = pnl_krw / buy_value * 100 if buy_value else 0.0
        _bdb.record_sell(
            session_id=self._db_session_id, market=market,
            entry_price=entry_price, exit_price=exit_price,
            coin_qty=coin_qty, fee=fee, pnl_krw=pnl_krw, pnl_pct=pnl_pct, reason=reason,
        )

        display_pnl = (exit_price - entry_price) / entry_price * 100
        emoji = "🟢" if exit_price >= entry_price else "🔴"
        print(
            f"\n{emoji} 매도 완료 | {market} | "
            f"진입={fmt_price(entry_price)} → 청산={fmt_price(exit_price)} | "
            f"손익={display_pnl:+.2f}% | 사유={reason}"
        )
        if self.config.PAPER_TRADING:
            print(f"   💰 페이퍼 잔고: {self.paper_capital:,.0f}원")

        _krw_live = self.paper_capital
        if not self.config.PAPER_TRADING:
            try:
                bals = self.client.upbit.get_balances()
                _krw_live = next((float(b['balance']) for b in (bals or []) if b['currency'] == 'KRW'), self.paper_capital)
            except Exception:
                pass
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='sell',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=_krw_live, note=reason,
        )
        self._save_state()
        self.trade_logger.print_summary()

    # ──────────────────────────────────────────
    # 투자금 계산
    # ──────────────────────────────────────────

    def _calc_trade_amount(self) -> float:
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

    # ──────────────────────────────────────────
    # 상태 영속화 (비정상 종료 복구)
    # ──────────────────────────────────────────

    def _save_state(self):
        if not self.positions and not self.pending_buys:
            self._clear_state()
            return
        state_data = {
            "saved_at":    datetime.now().isoformat(),
            "paper_capital": self.paper_capital,
            "positions":   [],
            "pending_buys": [],
        }
        for mkt, pos in self.positions.items():
            om = pos["order_mgr"]
            entry = {
                "market":      mkt,
                "entry_price": pos["entry_price"],
                "coin_qty":    pos["coin_qty"],
                "highest_price": pos["highest_price"],
            }
            if om.active_tp_order:
                entry["tp_price"] = om.active_tp_order["price"]
            if om.active_sl_order:
                entry["sl_price"] = om.active_sl_order["price"]
            state_data["positions"].append(entry)
        for mkt, pb in self.pending_buys.items():
            om = pb["order_mgr"]
            if om.active_buy_order:
                state_data["pending_buys"].append({
                    "market":      mkt,
                    "buy_price":   om.active_buy_order["price"],
                    "trade_amount": pb["trade_amount"],
                })
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"상태 파일 저장 실패: {e}")

    def _clear_state(self):
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception as e:
            logger.warning(f"상태 파일 삭제 실패: {e}")

    def _try_recover_state(self):
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
        except Exception as e:
            logger.warning(f"상태 파일 읽기 실패: {e}")
            self._clear_state()
            return

        # 구버전 단일 포지션 형식 호환
        if "positions" not in state_data and state_data.get("state") == "position":
            state_data = {"positions": [{
                "market":        state_data.get("market"),
                "entry_price":   state_data.get("entry_price", 0.0),
                "coin_qty":      state_data.get("coin_qty", 0.0),
                "highest_price": state_data.get("highest_price", 0.0),
                "tp_price":      state_data.get("tp_price"),
                "sl_price":      state_data.get("sl_price"),
            }], "pending_buys": [], "paper_capital": state_data.get("paper_capital")}

        positions_data = state_data.get("positions", [])
        saved_at = state_data.get("saved_at", "unknown")

        if not positions_data:
            self._clear_state()
            return

        print(f"\n{'='*60}")
        print(f"  ⚠️  이전 실행 상태 파일 감지! (저장: {saved_at})")
        print(f"  포지션 {len(positions_data)}개 발견:")
        for p in positions_data:
            print(f"    {p.get('market')} @ {fmt_price(p.get('entry_price', 0))} | 수량={p.get('coin_qty', 0):.8f}")
        print(f"{'='*60}")

        answer = self._input_with_timeout(
            "  복구 방법 선택 (y=포지션복구 / s=전체즉시청산 / n=무시, 30초 대기): ",
            timeout=30.0, default="n",
        )

        if answer == "y":
            pc = state_data.get("paper_capital")
            if self.config.PAPER_TRADING and pc is not None:
                self.paper_capital = float(pc)
            for p in positions_data:
                mkt         = p.get("market")
                entry_price = p.get("entry_price", 0.0)
                coin_qty    = p.get("coin_qty", 0.0)
                tp_price    = p.get("tp_price")
                sl_price    = p.get("sl_price")
                if not mkt or coin_qty <= 0:
                    continue
                order_mgr = OrderManager(self.client, self.config)
                if tp_price:
                    order_mgr.place_limit_sell(mkt, tp_price, coin_qty, "tp")
                if sl_price:
                    order_mgr.place_limit_sell(mkt, sl_price, coin_qty, "sl")
                self.positions[mkt] = {
                    "market": mkt, "order_mgr": order_mgr,
                    "entry_price": entry_price, "coin_qty": coin_qty,
                    "highest_price": p.get("highest_price", entry_price),
                    "avg_entry_price": entry_price, "entry_signal_score": 0,
                    "trade_amount": 0.0, "adjust_counter": 0,
                    "dca_levels_pending": [], "dca_order_pending": False,
                    "dca_current_amount": 0.0, "dca_timeout_at": None,
                    "dca_done": True, "pyramid_done": True,
                    "pyramid_order_pending": False, "pyramid_amount": 0.0,
                    "breakeven_activated": False, "trend_check_counter": 0,
                }
                logger.info(f"포지션 복구 완료: {mkt} @ {fmt_price(entry_price)}")
            print(f"  ✅ {len(self.positions)}개 포지션 복구 완료 — 모니터링 재개")

        elif answer == "s":
            print("  📤 전체 포지션 즉시 시장가 청산 실행 중...")
            for p in positions_data:
                mkt      = p.get("market")
                qty      = p.get("coin_qty", 0.0)
                ep       = p.get("entry_price", 0.0)
                current  = self.client.get_current_price(mkt) or ep
                if mkt and qty > 0:
                    order = self.client.sell_market_order(mkt, qty)
                    if order:
                        exit_price = order.get("price", current)
                        fee        = order.get("fee", 0.0)
                        if self.config.PAPER_TRADING:
                            self.paper_capital += order.get("revenue", qty * current)
                        # 임시 포지션 등록 후 기록
                        om = OrderManager(self.client, self.config)
                        self.positions[mkt] = {
                            "market": mkt, "order_mgr": om,
                            "entry_price": ep, "coin_qty": qty,
                            "highest_price": ep, "avg_entry_price": ep,
                            "entry_signal_score": 0, "trade_amount": 0.0,
                            "adjust_counter": 0, "dca_levels_pending": [],
                            "dca_order_pending": False, "dca_current_amount": 0.0,
                            "dca_timeout_at": None, "dca_done": True,
                            "pyramid_done": True, "pyramid_order_pending": False,
                            "pyramid_amount": 0.0, "breakeven_activated": False,
                            "trend_check_counter": 0,
                        }
                        self._record_sell(mkt, exit_price, fee, "재시작 후 즉시 청산")
            self._clear_state()

        else:
            print("  상태 파일 무시 — IDLE 상태로 시작합니다.")
            print("  ⚠️ 업비트 앱에서 미체결 주문을 직접 확인하세요.")
            self._clear_state()

    # ──────────────────────────────────────────
    # live_status.json 갱신
    # ──────────────────────────────────────────

    def _write_live_status(self):
        try:
            total_active = len(self.positions) + len(self.pending_buys)
            if total_active > 0:
                state_str = "position" if self.positions else "buy_wait"
            else:
                state_str = "idle"

            status: dict = {
                "state":         state_str,
                "mode":          "live" if not self.config.PAPER_TRADING else "paper",
                "paper_capital": self.paper_capital,
                "last_updated":  datetime.now().isoformat(),
                "positions":     [],
                "pending_buys":  [],
            }

            for mkt, pos in self.positions.items():
                om = pos["order_mgr"]
                cur = self.client.get_current_price(mkt) or pos["entry_price"]
                avg = pos["avg_entry_price"] if pos["avg_entry_price"] > 0 else pos["entry_price"]
                upnl_pct = (cur - avg) / avg * 100
                upnl_krw = (cur - avg) * pos["coin_qty"]
                p_dict = {
                    "market":              mkt,
                    "entry_price":         pos["entry_price"],
                    "avg_entry_price":     avg,
                    "coin_qty":            pos["coin_qty"],
                    "current_price":       cur,
                    "highest_price":       pos["highest_price"],
                    "unrealized_pct":      round(upnl_pct, 4),
                    "unrealized_krw":      round(upnl_krw, 0),
                    "position_value_krw":  round(cur * pos["coin_qty"], 0),
                    "dca_done":            pos["dca_done"],
                    "breakeven_activated": pos["breakeven_activated"],
                    "tp_price":            om.active_tp_order["price"] if om.active_tp_order else 0,
                    "sl_price":            om.active_sl_order["price"] if om.active_sl_order else 0,
                }
                status["positions"].append(p_dict)

            for mkt, pb in self.pending_buys.items():
                om = pb["order_mgr"]
                if om.active_buy_order:
                    status["pending_buys"].append({
                        "market":      mkt,
                        "buy_price":   om.active_buy_order.get("price", 0),
                        "buy_amount":  pb["trade_amount"],
                    })

            # 단일 포지션 대시보드 호환 필드 (첫 번째 포지션)
            if status["positions"]:
                p0 = status["positions"][0]
                status.update({
                    "market":          p0["market"],
                    "entry_price":     p0["entry_price"],
                    "avg_entry_price": p0["avg_entry_price"],
                    "coin_qty":        p0["coin_qty"],
                    "current_price":   p0["current_price"],
                    "highest_price":   p0["highest_price"],
                    "unrealized_pct":  p0["unrealized_pct"],
                    "unrealized_krw":  p0["unrealized_krw"],
                    "position_value_krw": p0["position_value_krw"],
                    "dca_done":        p0["dca_done"],
                    "breakeven_activated": p0["breakeven_activated"],
                    "tp_price":        p0["tp_price"],
                    "sl_price":        p0["sl_price"],
                })
            elif status["pending_buys"]:
                pb0 = status["pending_buys"][0]
                status.update({
                    "market":             pb0["market"],
                    "pending_buy_price":  pb0["buy_price"],
                    "pending_buy_amount": pb0["buy_amount"],
                })

            with open(self._live_status_file, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"live_status 기록 실패 (무시): {e}")

    def _clear_live_status(self):
        try:
            status = {
                "state":         "stopped",
                "mode":          "live" if not self.config.PAPER_TRADING else "paper",
                "paper_capital": self.paper_capital,
                "last_updated":  datetime.now().isoformat(),
            }
            with open(self._live_status_file, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ──────────────────────────────────────────
    # 종료 처리
    # ──────────────────────────────────────────

    def _emergency_shutdown(self):
        if self.positions or self.pending_buys:
            logger.warning(f"예상치 못한 종료 감지 — 상태 파일 저장 (포지션 {len(self.positions)}개)")
            self._save_state()
        self._clear_live_status()

    def _shutdown(self):
        # 대기 중인 매수 주문 취소
        for mkt, pb in list(self.pending_buys.items()):
            print(f"  📝 매수 주문 취소 [{mkt}]...")
            pb["order_mgr"].cancel_buy_order(mkt)
        self.pending_buys.clear()

        # 보유 포지션 처리
        if self.positions:
            mkts = ", ".join(self.positions.keys())
            print(f"  ⚠️ 미청산 포지션 존재: {mkts}")
            answer = self._input_with_timeout(
                "  강제 청산하시겠습니까? (y/n, 30초 내 응답): ", timeout=30.0, default="n"
            )
            if answer == "y":
                for mkt in list(self.positions.keys()):
                    pos = self.positions[mkt]
                    pos["order_mgr"].cancel_sell_orders()
                    current = self.client.get_current_price(mkt) or pos["entry_price"]
                    order = self.client.sell_market_order(mkt, pos["coin_qty"])
                    if order:
                        exit_price = order.get("price", pos["entry_price"])
                        fee        = order.get("fee", 0)
                        if self.config.PAPER_TRADING:
                            self.paper_capital += order.get("revenue", 0)
                        self._record_sell(mkt, exit_price, fee, "강제 청산 (봇 종료)")
            else:
                print("  ℹ️ 기존 지정가 매도 주문은 유지됩니다.")

        self.trade_logger.print_summary()
        _bdb.record_balance(
            session_id=self._db_session_id, trigger='shutdown',
            mode='paper' if self.config.PAPER_TRADING else 'live',
            krw_balance=self.paper_capital, note='봇 종료',
        )
        _bdb.end_trading_session(self._db_session_id, self.trade_logger.performance, self.paper_capital)
        self._clear_live_status()

    @staticmethod
    def _input_with_timeout(prompt: str, timeout: float = 30.0, default: str = "n") -> str:
        result    = [default]
        answered  = threading.Event()

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
