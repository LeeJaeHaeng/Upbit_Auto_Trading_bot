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
