# 업비트 자동매매 봇

업비트(Upbit) 한국 암호화폐 거래소용 자동매매 봇입니다.
KRW 마켓 코인을 실시간 스캔하여 기술적 지표 기반으로 **지정가 주문** 매수/매도를 자동 실행합니다.

## 주요 기능

- **멀티 코인 스캔** — 전체 KRW 마켓을 스캔하여 최적 종목 자동 선택
- **5개 기술적 지표** — RSI(Stoch RSI), MACD(제로선 크로스), 볼린저 밴드(스퀴즈감지), EMA, OBV(스마트머니) 복합 신호
- **지정가 주문** — 최적 진입가 계산 후 지정가 매수/매도 (시장가 대비 유리한 체결)
- **동적 손익 관리** — 익절/손절 자동 조정 + 트레일링 스탑 + R/R 비율 필터(1.5x 이상만 진입)
- **시장 국면 필터** — EMA200 기반 하락장 자동 감지 및 진입 차단
- **페이퍼 트레이딩** — 실제 자금 없이 모의 투자 (기본값)
- **강화 백테스팅** — 슬리피지·수수료 반영, Sharpe/Sortino/Calmar 리스크 지표
- **신호 정확도 검증** — Precision/Recall/F1 기반 신호 품질 측정
- **Walk-Forward 검증** — IS/OOS 분리로 과적합 측정
- **파라미터 최적화** — 그리드 서치로 최적 파라미터 자동 탐색
- **실시간 대시보드** — Streamlit + Django 기반 성과 모니터링

## 프로젝트 구조

```
upbit_bot/
├── main.py                  # CLI 진입점
├── config.py                # 전략 파라미터 설정
├── trader.py                # 메인 상태 머신 (IDLE→BUY_WAIT→POSITION)
├── api_client.py            # Upbit API 래퍼 (pyupbit)
├── order_manager.py         # 지정가 주문 라이프사이클
├── market_scanner.py        # KRW 전 종목 스캔 및 랭킹
├── indicators.py            # RSI, MACD, 볼린저, EMA, ATR 계산
├── market_indicators.py     # 공포/탐욕 지수, 김치 프리미엄
├── trade_logger.py          # 거래 CSV 로그 + 성과 JSON
│
├── backtester.py            # 기존 백테스터
├── enhanced_backtester.py   # 강화 백테스터 (슬리피지·리스크 지표·국면 분석)
├── signal_validator.py      # 신호별 Precision/Recall/F1/Edge 검증
├── walk_forward_validator.py# Walk-Forward IS/OOS 과적합 검증
├── param_optimizer.py       # 그리드 서치 파라미터 최적화
│
├── dashboard.py             # Streamlit 대시보드
├── dashboard_app/           # Django 앱 (REST API)
├── dashboard_web/           # Django 프로젝트 설정
│
├── start_dashboards.bat     # 대시보드 실행 (Windows)
├── stop_dashboards.ps1      # 대시보드 종료
└── requirements.txt
```

## 설치

```bash
pip install -r upbit_bot/requirements.txt
```

## 환경 변수 설정

`upbit_bot/.env` 파일 생성:

```env
UPBIT_ACCESS_KEY=your_access_key_here
UPBIT_SECRET_KEY=your_secret_key_here
```

API 키 발급: https://upbit.com/service_center/open_api_guide

## 사용법

모든 명령어는 `upbit_bot/` 디렉토리에서 실행합니다.

자동매매 대시보드: http://localhost:8501

### 트레이딩

```bash
# 페이퍼 트레이딩 (기본값 — 실제 주문 없음)
python main.py

# 실거래 (API 키 필요, 주의!)
python main.py --live
```

### 검증 및 최적화

```bash
# 강화 백테스팅 (슬리피지·리스크 지표·시장 국면 분석)
python main.py --enhanced-backtest
python main.py --enhanced-backtest --days 90

# 신호별 정확도 검증 (Precision/Recall/F1)
python main.py --validate
python main.py --validate --days 180

# Walk-Forward 검증 (IS vs OOS 과적합 측정)
python main.py --walk-forward
python main.py --walk-forward --days 180 --windows 4

# 파라미터 최적화 (그리드 서치)
python main.py --optimize
python main.py --optimize --days 180
python main.py --optimize --apply        # 최적값 config.py 자동 반영
```

### 분석

```bash
# 마켓 스캔 (상위 10개 종목)
python main.py --scan

# 기존 백테스팅
python main.py --backtest --days 60
```

### 대시보드

```bash
# Streamlit 단독 실행
python main.py --dashboard

# Django + Streamlit 동시 실행 (Windows)
./upbit_bot/start_dashboards.bat

# 대시보드 종료
powershell -File upbit_bot/stop_dashboards.ps1
```

## 전략 구조

### 상태 머신 (trader.py)

```
STATE_IDLE
  ↓ 마켓 스캔 → 국면 필터(EMA200) → 진입가 분석
STATE_BUY_WAITING
  ↓ 지정가 매수 체결 대기 (30분 타임아웃)
STATE_POSITION
  ↓ 지정가 익절/손절 감시 + 동적 TP/SL 조정
STATE_IDLE
```

### 진입 조건

| 지표 | 신호 조건 |
| --- | --- |
| RSI | RSI < 25 (과매도) |
| MACD | 히스토그램 양전환 또는 상승 모멘텀 |
| 볼린저 밴드 | BB% < 0.25 (하단 25% 이내) |
| EMA | 단기 EMA > 장기 EMA + 추세 필터 |
| 거래량 | 현재 거래량 > 평균 × 1.5 |

5개 중 **3개 이상** 일치 시 진입

### 리스크 관리

| 항목 | 값 |
| --- | --- |
| 손절 | -2.0% |
| 익절 | +3.0% |
| 트레일링 스탑 | 고점 대비 -1.0% |
| 1회 거래 금액 | 100,000원 |
| 수수료 | 0.05% |

### 시장 국면 필터

하락장 진입을 자동 차단합니다:
- **하락장**: `현재가 < EMA200` AND `EMA50 < EMA200` → 진입 차단
- **횡보장**: `현재가 < EMA200` but `EMA50 >= EMA200` → 진입 허용
- **상승장**: `현재가 > EMA200` → 진입 허용

## 백테스팅 결과 (KRW-BTC, 90일)

| 지표 | 기존 전략 | 국면 필터 적용 후 |
| --- | --- | --- |
| 전략 수익률 | -3.11% | **-0.24%** |
| Buy&Hold | -21.08% | -21.08% |
| 알파 | +17.97% | **+20.85%** |
| 승률 | 35.8% | **57.7%** |
| MDD | -4.20% | **-0.81%** |
| 거래수 | 53회 | 26회 |

> 측정 기간(2025-12-19 ~ 2026-03-19)은 BTC 가격이 약 -21% 하락한 강한 하락장.
> 국면 필터로 하락장 진입을 차단하여 손실을 크게 줄였습니다.

## 파라미터 최적화

`param_optimizer.py`가 180일 실제 BTC 데이터로 그리드 서치를 수행해
OOS Sharpe Ratio 기준 최적 파라미터를 찾습니다.

탐색 대상:
- `MIN_SIGNAL_COUNT` (진입 신호 임계값)
- `STOP_LOSS_PCT` / `TAKE_PROFIT_PCT` (손익 비율)
- `TRAILING_STOP_PCT` (트레일링 스탑)
- `RSI_OVERSOLD` (RSI 과매도 기준)
- `VOLUME_THRESHOLD` (거래량 배율)

```bash
python main.py --optimize --days 180 --apply
```

## 주의사항

- `PAPER_TRADING = True` (기본값) — 실거래 시 반드시 `--live` 플래그 사용
- 실거래 전 반드시 페이퍼 트레이딩으로 충분히 검증하세요
- 암호화폐 투자는 원금 손실 위험이 있습니다
- 이 봇은 교육/연구 목적으로 제공되며, 투자 손실에 대한 책임을 지지 않습니다

## License

MIT
