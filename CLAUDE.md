# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Upbit(업비트) 한국 암호화폐 거래소용 자동매매 봇. KRW 마켓 코인을 스캔하여 기술적 지표 기반으로 지정가 주문 매수/매도를 실행한다.

## Commands

All commands run from `upbit_bot/` directory:

```bash
# Paper trading (default, no real orders)
python main.py

# Live trading (real orders, requires API keys in .env)
python main.py --live

# 기존 백테스팅 (simple)
python main.py --backtest
python main.py --backtest --days 60

# 강화 백테스팅 — 슬리피지·다음캔들 체결·Sharpe/Sortino/Calmar·국면분석
python main.py --enhanced-backtest
python main.py --enhanced-backtest --days 90

# 신호별 정확도 검증 — Precision / Recall / F1 / Edge 분석
python main.py --validate
python main.py --validate --days 180

# Walk-Forward 검증 — IS vs OOS 과적합 측정 + MIN_SIGNAL_COUNT 민감도 테스트
python main.py --walk-forward
python main.py --walk-forward --days 180 --windows 4

# Market scan only (top 10 coins)
python main.py --scan

# Streamlit dashboard
python main.py --dashboard

# Start Django + Streamlit dashboards (Windows)
./start_dashboards.bat
# or
powershell -File start_dashboards.ps1

# Stop dashboards
powershell -File stop_dashboards.ps1
```

Install dependencies:
```bash
pip install -r upbit_bot/requirements.txt
```

## Architecture

### State Machine (trader.py)

The bot is a 4-state loop running every 60 seconds:

1. **STATE_IDLE** → Market scan, score coins, calculate optimal entry price
2. **STATE_BUY_WAITING** → Limit buy pending (30-min timeout, then cancel & return to IDLE)
3. **STATE_POSITION** → Position held, monitor exit signals, dynamic TP/SL adjustment
4. Return to IDLE after sell

### Module Relationships

```
main.py (CLI entry)
  ├─→ Trader (state machine)
  │     ├─→ MarketScanner        — Screens all KRW coins, ranks by opportunity score
  │     ├─→ OrderManager         — Optimal entry price, limit order lifecycle
  │     │     ├─→ UpbitClient    — pyupbit API wrapper
  │     │     └─→ indicators     — RSI, MACD, BB, EMA, ATR calculations
  │     ├─→ TradeLogger          — CSV trades log + JSON performance metrics
  │     └─→ MarketEnvironment    — Fear/greed index, kimchi premium (scaffolded)
  │
  ├─→ Backtester                 — 기존 백테스터 (단순, look-ahead bias 있음)
  ├─→ EnhancedBacktester         — 강화 백테스터 (다음캔들 체결, 슬리피지, 리스크 지표)
  ├─→ SignalValidator             — 신호별 Precision/Recall/F1/Edge 분석
  └─→ WalkForwardValidator       — IS/OOS 분할 검증 + MIN_SIGNAL_COUNT 민감도 테스트

dashboard.py (Streamlit)
  └─→ Reads trade log CSVs, displays charts & performance metrics
```

### Validation Modules

| 모듈 | 파일 | 주요 기능 |
| --- | --- | --- |
| 강화 백테스터 | `enhanced_backtester.py` | 슬리피지(±0.05%), 다음캔들 시가 체결, Sharpe/Sortino/Calmar, 국면별 성능, Buy&Hold 비교 |
| 신호 검증기 | `signal_validator.py` | 신호 발생 후 N캔들 가격 방향 예측 정확도, 복합신호 F1, 최적 MIN_SIGNAL_COUNT 추천 |
| WF 검증기 | `walk_forward_validator.py` | IS/OOS 성능 저하율, 과적합 등급(✅/⚠️/❌), MIN_SIGNAL_COUNT 민감도 테스트 |

**기존 `backtester.py` vs `enhanced_backtester.py` 핵심 차이:**

- 기존: 신호 발생 캔들 close에서 즉시 체결 (낙관적 가정)
- 강화: 신호 발생 캔들 다음 캔들 시가에 슬리피지 포함 체결 (현실적)

### Key Trading Logic

**Entry Conditions:**
- Only coins with >50B KRW daily volume qualify
- 5 signals scored: RSI, MACD cross, BB position, EMA alignment, volume spike
- Entry requires ≥3 signals aligned (`MIN_SIGNAL_COUNT = 3` in config.py)
- Optimal entry price = min(BB lower band, EMA support, ATR-adjusted discount)

**Exit Strategy:**
- On buy fill: immediately places TWO limit sells — take-profit (+4%) and stop-loss (-1.5%)
- Dynamic adjustment every 5 cycles:
  - If profit > 2%: trailing stop activates (2% below running high)
  - TP adjusts to `BB upper band × 0.998`
- Danger signal triggers instant market sell: MACD dead cross + RSI overbought + volume spike

**Risk Controls:**
- ATR% must be > 0.5% (volatility filter)
- Blacklisted: LUNA, LUNC, LUNA2
- Fixed position size: 100,000 KRW per trade
- Fee: 0.05%

### Configuration (config.py)

All strategy parameters live in `config.py`:
- API keys loaded from `.env` file (UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)
- `PAPER_TRADING = True` by default (no real orders)
- Candle interval: 60 minutes, 200 bars
- Indicator periods: RSI=14, MACD=12/26/9, BB=20, EMA=20/50/200, ATR=14

### Dashboard Architecture

Two separate dashboard components:
- **Streamlit** (`dashboard.py`): Analytics, charts, performance metrics — reads from log files
- **Django** (`dashboard_app/`): Scaffolded REST API + HTML templates — largely unused, no persistent DB (SQLite)

Django port: 8001, Streamlit port: 8501 (auto-increments if occupied). PIDs tracked in `dashboard_pids.json`.

### Logging

- `bot.log` — bot activity log (file + console)
- `trades_YYYYMMDD.csv` — trade history
- `performance.json` — cumulative performance metrics

---

## 실거래 전 체크리스트 (Pre-Live Checklist)

실거래(`--live`) 전 반드시 확인해야 할 사항입니다.

### 필수 준비

- [ ] `.env` 파일에 실제 API 키 입력 (`UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`)
- [ ] 업비트 오픈API에서 출금/주문 권한 활성화 여부 확인
- [ ] `PAPER_TRADING = True` 기본값 유지 — 실거래 시 `--live` 플래그 사용
- [ ] 충분한 KRW 잔고 확인 (`TRADE_AMOUNT_KRW` = 100,000원 기준)
- [ ] 페이퍼 트레이딩으로 최소 1주일 이상 검증 완료

### 알려진 제한사항

| 항목 | 내용 | 조치 |
| --- | --- | --- |
| 단일 포지션 | 한 번에 1개 마켓만 포지션 보유 가능 | 의도된 설계 |
| 지정가 체결 시뮬레이션 | 페이퍼 트레이딩에서 현재가 ≤ 주문가이면 즉시 체결 처리 (호가 깊이 무시) | 실거래 전환 시 체결률 차이 발생 가능 |
| 국면 필터 EMA200 | 60개 미만 캔들 시 실거래 모드에서는 진입 차단(안전), 페이퍼에서는 스킵 | 의도된 비대칭 설계 |
| API 레이트 리밋 | pyupbit 내부에서 초당 10회 제한. 빠른 사이클(< 10초) 설정 시 오류 가능 | `CHECK_INTERVAL` 최소 30초 권장 |
| 거래량 없는 알트코인 | 스캐너 최소 거래량 필터(50B) 있으나 급격한 거래량 감소 시 슬리피지 큼 | 현재 무대응 |

### 운영 주의사항

- **강제청산 타임아웃**: 봇 종료(Ctrl+C) 시 포지션 보유 중이면 30초 내 응답 없으면 자동으로 기존 지정가 주문 유지(n 기본값)
- **중복 주문 방지**: `place_limit_buy()` 시작 시 `active_buy_order` 존재 여부 확인 — 비정상 종료 후 재시작 시 잔여 주문이 업비트에 남아 있을 수 있으므로 업비트 앱에서 미체결 주문 직접 확인 필요
- **동적 TP/SL 조정 race condition**: update_exit_prices() 호출 직전 체결 여부 재확인 로직 존재. 그러나 네트워크 지연 상황에서는 체결 감지 누락 가능 — 업비트 앱 병행 모니터링 권장
- **수수료 계산**: 페이퍼 트레이딩에서 매수 시 `trade_amount + fee` 차감. 실거래에서는 업비트 KRW 잔고에서 실시간 차감되므로 잔고 부족 시 주문 실패
- **전략 한계**: 현재 전략은 단방향(롱 전용). BTC -20% 이상 하락장에서는 국면 필터가 진입을 막아주나, 급격한 플래시 크래시에서는 손절 슬리피지 발생 가능

### config.py 주요 파라미터 (최적화 반영: 2026-03-19)

| 파라미터 | 값 | 설명 |
| --- | --- | --- |
| `RSI_OVERSOLD` | 25 | 과매도 기준 — 이 값 **그대로** RSI 신호 판정 (이전 +5 버그 수정 완료) |
| `STOP_LOSS_PCT` | 0.020 | 손절 2.0% |
| `TAKE_PROFIT_PCT` | 0.030 | 익절 3.0% |
| `TRAILING_STOP_PCT` | 0.010 | 트레일링 스탑 1.0% |
| `VOLUME_THRESHOLD` | **1.5** | 2.0 상향 시 역효과 확인됨 (25→15거래, -0.02%→-0.26%) — 1.5 유지 |
| `USE_TREND_FILTER` | True | EMA200 하락장 필터 활성화 |
| `TREND_FILTER_STRICT` | False | strict=False: close < EMA200 AND ema50 < EMA200 모두 충족 시만 차단 |
| `BREAKEVEN_TRIGGER_PCT` | 0.010 | 1% 수익 도달 시 손절선을 매수가+수수료로 이동 (소손실 방어) |
| `TECHNICAL_EXIT_MIN_PCT` | 0.005 | 0%~0.5% 소수익 구간 기술적 청산 차단 (조기 청산 방지) |
| `MTF_CHECK` | True | 4시간봉 EMA20>EMA50 확인 후 60분봉 진입 허용 |
| `SCALED_ENTRY` | True | 분할투자: 1차 60% → -1.5% 하락 시 2차 40% 매수, 30분 타임아웃 |

### 전략 개선 전후 백테스팅 비교 (90일, KRW-BTC, 2026-03-19 기준)

| 시나리오 | 수익률 | 승률 | 거래수 | 평균수익 | 평균손실 | MDD |
| --- | --- | --- | --- | --- | --- | --- |
| 버그수정 전 | -0.02% | 60.0% | 25회 | +1.03% | -1.57% | -0.89% |
| VOLUME=2.0 적용 | -0.26% | 53.3% | 15회 | — | — | — |
| **최종 (VOLUME=1.5 + exit 개선)** | **+0.00%** | **52.2%** | **23회** | **+1.13%** | **-1.23%** | **-0.89%** |
| Buy&Hold 동일 기간 | -20.76% | — | — | — | — | — |

**개선 포인트**: 평균 손실 -1.57% → -1.23% (본전 보호 스탑 효과), 평균 수익 +1.03% → +1.13% (소수익 조기청산 방지)

### 수정된 버그 목록 (2026-03-19)

**버그 수정:**

1. **`indicators.py`** — `get_signal_score()`: RSI 신호 조건이 `config.RSI_OVERSOLD + 5` 하드코딩 → `config.RSI_OVERSOLD` 직접 사용으로 수정
2. **`order_manager.py`** — `place_limit_buy()`: 활성 매수 주문 존재 시 중복 방지 로직 추가
3. **`order_manager.py`** — `check_sell_orders()`: Exception 무시 → 재시도 1회 + 에러 로깅
4. **`api_client.py`** — `sell_market_order()`: 실거래 API 응답 필드 부재 시 추정값으로 정규화 (price, fee, revenue 보장)
5. **`trader.py`** — `_check_trend_filter()`: 실거래 모드에서 캔들 데이터 부족 시 안전 차단
6. **`trader.py`** — `_on_buy_filled()`: paper_capital 차감 시 수수료 미포함 버그 수정
7. **`trader.py`** — `_dynamic_adjust_exit()`: update_exit_prices() 직전 race condition 방지용 체결 재확인
8. **`trader.py`** — `_shutdown()`: input() 무한 대기 → 30초 타임아웃 (threading 기반)

**전략 개선 (신규 기능):**
9. **`trader.py`** — 하락장 3단계 방어: IDLE 진입차단 / BUY_WAITING 매 3사이클 재확인 후 취소 / POSITION 하락장 전환 시 SL 강화
10. **`trader.py`** — 비정상 종료 복구: atexit/SIGTERM 시 `bot_state.json` 저장 → 재시작 시 y/s/n 선택
11. **`trader.py`** — 연속 오류 10회 초과 시 긴급 상태 저장 후 봇 자동 종료 (`_emergency_shutdown`)
12. **`trader.py`** — 분할투자 DCA: `_on_dca_filled()` — 1차 매수 후 -1.5% 하락 시 2차 매수, 평균단가 재계산
13. **`trader.py`** — MTF 4시간봉 추세 확인: `_check_mtf_trend()` — EMA20>EMA50이어야 진입 허용
14. **`indicators.py`** — `get_sell_signal()`: 본전 보호 스탑 (`BREAKEVEN_TRIGGER_PCT`) + 소수익 청산 차단 (`TECHNICAL_EXIT_MIN_PCT`) 추가
15. **`config.py`** — VOLUME_THRESHOLD 2.0→1.5 복원 (2.0은 거래수 감소로 역효과 확인)
