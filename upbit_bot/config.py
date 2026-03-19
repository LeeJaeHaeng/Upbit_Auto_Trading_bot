"""
업비트 자동매매 봇 설정 파일
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# =====================================================
# 업비트 API 키
# .env 파일에서 자동 로드합니다. 코드에 직접 넣지 마세요!
# 발급: https://upbit.com/service_center/open_api_guide
# =====================================================
ACCESS_KEY = os.environ.get("UPBIT_ACCESS_KEY", "YOUR_ACCESS_KEY")
SECRET_KEY = os.environ.get("UPBIT_SECRET_KEY", "YOUR_SECRET_KEY")

# =====================================================
# 거래 설정
# =====================================================
MARKET = "KRW-BTC"          # 거래 마켓 (비트코인/원화)
TRADE_AMOUNT_KRW = 100_000  # 1회 거래 금액 (원), 최소 5,000원
FEE_RATE = 0.0005           # 업비트 수수료 0.05%

# =====================================================
# 전략 파라미터 (최적화 결과 반영: 2026-03-19)
# =====================================================
# 봉 설정
CANDLE_UNIT = 60            # 분봉 (1, 3, 5, 10, 15, 30, 60, 240)
CANDLE_COUNT = 200          # 가져올 캔들 수

# RSI
RSI_PERIOD = 14
RSI_OVERSOLD = 25           # 과매도 기준 (매수 신호) — 최적화: 30→25
RSI_OVERBOUGHT = 70         # 과매수 기준 (매도 신호)

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# 볼린저 밴드
BB_PERIOD = 20
BB_STD = 2.0

# EMA (추세 필터)
EMA_SHORT = 20
EMA_LONG = 50
EMA_TREND = 200             # 장기 추세 필터

# 거래량 필터
VOLUME_MA_PERIOD = 20       # 거래량 이동평균 기간
VOLUME_THRESHOLD = 1.5      # 평균 거래량 대비 배율 (백테스트 결과 2.0은 역효과 — 1.5 최적)

# =====================================================
# 리스크 관리 (최적화 결과 반영)
# =====================================================
STOP_LOSS_PCT = 0.020       # 손절 비율 — 최적화: 1.5%→2.0% (변동성 대비 여유)
TAKE_PROFIT_PCT = 0.030     # 익절 비율 — 최적화: 4.0%→3.0% (빠른 수익 실현)
TRAILING_STOP_PCT = 0.010   # 트레일링 스탑 — 최적화: 2.0%→1.0% (수익 보호 강화)

# 본전 보호 (Breakeven Stop)
# 수익률이 이 값 이상 도달 시 손절선을 매수가+수수료로 자동 이동
BREAKEVEN_TRIGGER_PCT = 0.010   # 1.0% 수익 도달 시 본전 스탑 활성화

# 기술적 매도 신호 최소 수익 임계값
# 소수익 구간(0%~이 값)에서는 기술적 신호로 청산하지 않음 (소수익 조기 청산 방지)
# 손실 중이거나 이 값 이상 수익 시에는 정상 동작
TECHNICAL_EXIT_MIN_PCT = 0.005  # 0.5% 미만 수익 구간에서 기술적 청산 차단

# 진입 신호 최소 개수 (5개 지표 중 몇 개 이상 일치해야 진입할지)
MIN_SIGNAL_COUNT = 3

# =====================================================
# 다중 시간대 추세 확인 (Multi-Timeframe)
# =====================================================
# 60분봉 신호 진입 전 4시간봉 EMA 추세가 일치하는지 확인
MTF_CHECK = True            # True: 4h 추세 확인 활성화
MTF_CANDLE_UNIT = 240       # 4시간봉
MTF_EMA_SHORT = 20          # 4h 단기 EMA
MTF_EMA_LONG = 50           # 4h 장기 EMA (이 이상 단기 EMA여야 진입 허용)

# =====================================================
# 분할 투자 (DCA — Dollar Cost Averaging)
# =====================================================
# 1차 매수 후 가격이 추가 하락 시 2차 매수로 평균단가를 낮춤
SCALED_ENTRY = True             # 분할 매수 활성화
SCALED_ENTRY_1ST_RATIO = 0.6   # 1차 매수 비율 (60%), 2차는 나머지 40%
SCALED_ENTRY_DIP_PCT = 0.015   # 1차 매수가 대비 -1.5% 하락 시 2차 매수 트리거
SCALED_ENTRY_TIMEOUT_MIN = 30  # 2차 매수 대기 최대 시간(분), 초과 시 1차만으로 포지션 확정

# =====================================================
# 시장 상황 필터
# =====================================================
MIN_VOLATILITY_PCT = 0.5    # 최소 변동성 (1시간 기준 %)
ATR_PERIOD = 14             # ATR 기간

# 시장 국면 필터 — 하락장 진입 차단
# True: EMA200 기준 하락 추세 감지 시 진입 보류
# False: 국면 무관하게 항상 진입 시도
USE_TREND_FILTER = True

# 하락장 판단 기준
# 현재가 < EMA200 AND EMA50 < EMA200 → 하락장으로 분류 → 진입 차단
# 현재가 < EMA200 이지만 EMA50 >= EMA200 → 횡보장 → 진입 허용
TREND_FILTER_STRICT = False  # True: EMA50 < EMA200만으로도 차단 (더 보수적)

# =====================================================
# 봇 동작 설정
# =====================================================
PAPER_TRADING = True        # True: 페이퍼 트레이딩 (실제 주문X), False: 실거래
CHECK_INTERVAL = 60         # 신호 체크 주기 (초)
LOG_FILE = str(BASE_DIR / "trading_log.csv")
PERFORMANCE_FILE = str(BASE_DIR / "performance.json")
BOT_LOG_FILE = str(BASE_DIR / "bot.log")
