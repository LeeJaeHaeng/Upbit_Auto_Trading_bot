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
TRADE_AMOUNT_KRW = 100_000  # 고정 거래금액 (TRADE_AMOUNT_PCT=0 일 때 사용)
TRADE_AMOUNT_PCT = 0.10     # 총 자금 대비 투자 비율 (10%). 0이면 고정금액 사용
TRADE_AMOUNT_MIN = 10_000   # 최소 투자금 (원)
TRADE_AMOUNT_MAX = 500_000  # 최대 투자금 (원) — 잔고가 커도 이 이상 투자 안 함
FEE_RATE = 0.0005           # 업비트 수수료 0.05%

# =====================================================
# 전략 파라미터 (최적화 결과 반영: 2026-03-19)
# =====================================================
# 봉 설정
CANDLE_UNIT = 60            # 분봉 (1, 3, 5, 10, 15, 30, 60, 240)
CANDLE_COUNT = 200          # 가져올 캔들 수

# RSI
RSI_PERIOD = 14
RSI_OVERSOLD = 35           # 과매도 기준 (매수 신호) — 완화: 25→35 (표준 과매도)
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
VOLUME_THRESHOLD = 1.2      # 평균 거래량 대비 배율 — 완화: 1.5→1.2 (진입 빈도 증가)

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

# 최소 리스크/리워드 비율 (Paul Tudor Jones: 최소 1.5:1 원칙)
# 익절폭 / 손절폭 >= 이 값이어야 진입 허용
MIN_RR_RATIO = 1.5

# =====================================================
# 다중 시간대 추세 확인 (Multi-Timeframe)
# =====================================================
# 60분봉 신호 진입 전 4시간봉 EMA 추세가 일치하는지 확인
MTF_CHECK = False           # True: 4h 추세 확인 활성화 / False: 비활성화 (완화)
MTF_CANDLE_UNIT = 240       # 4시간봉
MTF_EMA_SHORT = 20          # 4h 단기 EMA
MTF_EMA_LONG = 50           # 4h 장기 EMA (이 이상 단기 EMA여야 진입 허용)

# =====================================================
# 물타기 — 다단계 DCA (Dollar Cost Averaging)
# =====================================================
# 1차 매수 후 가격이 하락할수록 2차→3차 추가 매수로 평균단가를 낮춤
SCALED_ENTRY = True             # 물타기 활성화
SCALED_ENTRY_1ST_RATIO = 0.5   # 1차 매수 비율 (총 투자금의 50%)
SCALED_ENTRY_TIMEOUT_MIN = 60  # 물타기 대기 최대 시간(분), 초과 시 현 포지션 확정
# 물타기 단계: [(하락률, 추가매수비율), ...]  — 1차 매수가 기준 하락 시 트리거
DCA_LEVELS = [
    (0.015, 0.25),   # 2차: -1.5% 하락 시 투자금 25% 추가
    (0.030, 0.25),   # 3차: -3.0% 하락 시 투자금 25% 추가
]

# =====================================================
# 불타기 — 피라미딩 (Pyramiding)
# =====================================================
# 수익 중일 때 추가 매수하여 모멘텀 극대화
PYRAMID_ENABLED = True          # 불타기 활성화
PYRAMID_TRIGGER_PCT = 0.015     # +1.5% 수익 도달 시 추가 매수
PYRAMID_ADD_RATIO = 0.50        # 원래 투자금의 50% 추가 매수
PYRAMID_MAX_COUNT = 1           # 최대 불타기 횟수 (1회)
PYRAMID_SL_TO_ENTRY = True      # 불타기 후 SL을 평균단가+수수료로 이동 (손실 방지)

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
