"""
기술적 지표 계산 모듈
"""

import pandas as pd
import numpy as np


def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Relative Strength Index) 계산"""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD 계산. (macd_line, signal_line, histogram) 반환"""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_bollinger_bands(
    closes: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 계산. (upper, middle, lower) 반환"""
    middle = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def calculate_ema(closes: pd.Series, period: int) -> pd.Series:
    """EMA (Exponential Moving Average) 계산"""
    return closes.ewm(span=period, adjust=False).mean()


def calculate_atr(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> pd.Series:
    """ATR (Average True Range) 계산"""
    prev_close = closes.shift(1)
    tr = pd.concat(
        [
            highs - lows,
            (highs - prev_close).abs(),
            (lows - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def calculate_volume_ma(volumes: pd.Series, period: int = 20) -> pd.Series:
    """거래량 이동평균 계산"""
    return volumes.rolling(period).mean()


def add_all_indicators(df: pd.DataFrame, config) -> pd.DataFrame:
    """DataFrame에 모든 지표를 추가하여 반환"""
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]

    # RSI
    df["rsi"] = calculate_rsi(closes, config.RSI_PERIOD)

    # MACD
    df["macd"], df["macd_signal"], df["macd_hist"] = calculate_macd(
        closes, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL
    )
    df["macd_prev"] = df["macd"].shift(1)
    df["macd_hist_prev"] = df["macd_hist"].shift(1)

    # 볼린저 밴드
    df["bb_upper"], df["bb_middle"], df["bb_lower"] = calculate_bollinger_bands(
        closes, config.BB_PERIOD, config.BB_STD
    )
    df["bb_pct"] = (closes - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])

    # EMA
    df["ema_short"] = calculate_ema(closes, config.EMA_SHORT)
    df["ema_long"] = calculate_ema(closes, config.EMA_LONG)
    df["ema_trend"] = calculate_ema(closes, config.EMA_TREND)

    # ATR (변동성)
    df["atr"] = calculate_atr(highs, lows, closes, config.ATR_PERIOD)
    df["atr_pct"] = df["atr"] / closes * 100  # ATR을 가격 대비 %로

    # 거래량 지표
    df["volume_ma"] = calculate_volume_ma(volumes, config.VOLUME_MA_PERIOD)
    df["volume_ratio"] = volumes / df["volume_ma"]

    return df


def get_signal_score(row: pd.Series, config) -> dict:
    """
    현재 캔들의 매수 신호 점수를 계산합니다.
    각 지표별 신호를 반환하며, True = 매수 신호.

    반환값: {
        'signals': {지표명: True/False},
        'score': 총 True 개수,
        'details': 상세 설명
    }
    """
    signals = {}
    details = {}

    # ── 1. RSI 신호 ──
    # RSI가 과매도(30 이하)에서 반등 중이면 매수 신호
    rsi = row["rsi"]
    signals["rsi"] = rsi < config.RSI_OVERSOLD + 5  # 35 이하면 신호
    details["rsi"] = f"RSI={rsi:.1f} (기준≤{config.RSI_OVERSOLD + 5})"

    # ── 2. MACD 신호 ──
    # MACD 히스토그램이 음수에서 양수로 전환되거나,
    # 히스토그램/라인이 모두 상승 중이면 초기 반전 신호로 판단
    macd_hist = row["macd_hist"]
    macd = row["macd"]
    macd_prev = row.get("macd_prev", np.nan)
    macd_hist_prev = row.get("macd_hist_prev", np.nan)

    hist_cross_up = (
        pd.notna(macd_hist_prev)
        and macd_hist_prev <= 0
        and macd_hist > 0
    )
    momentum_turn_up = (
        pd.notna(macd_prev)
        and pd.notna(macd_hist_prev)
        and macd > macd_prev
        and macd_hist > macd_hist_prev
        and macd_hist > 0
    )

    signals["macd"] = (macd_hist > 0 and macd < 0) or hist_cross_up or momentum_turn_up
    details["macd"] = f"MACD={macd:.2f}, Hist={macd_hist:.2f}"

    # ── 3. 볼린저 밴드 신호 ──
    # 가격이 하단 밴드 근처 (bb_pct < 0.2 = 하단 20%)
    bb_pct = row["bb_pct"]
    signals["bollinger"] = bb_pct < 0.25
    details["bollinger"] = f"BB%={bb_pct:.2f} (기준<0.25)"

    # ── 4. EMA 추세 신호 ──
    # 단기 EMA > 장기 EMA (단기 상승 추세)
    ema_short = row["ema_short"]
    ema_long = row["ema_long"]
    ema_trend = row["ema_trend"]
    close = row["close"]
    # 가격이 EMA200 위에 있거나 EMA200 5% 이내 (과도한 하락 제외)
    near_trend = close >= ema_trend * 0.95
    signals["ema"] = (ema_short > ema_long) and near_trend
    details["ema"] = f"EMA단기={ema_short:.0f}, EMA장기={ema_long:.0f}, 추세필터={'OK' if near_trend else 'X'}"

    # ── 5. 거래량 신호 ──
    # 현재 거래량이 평균보다 높을수록 신호 신뢰도 높음
    volume_ratio = row["volume_ratio"]
    signals["volume"] = volume_ratio >= config.VOLUME_THRESHOLD
    details["volume"] = f"거래량배율={volume_ratio:.2f} (기준≥{config.VOLUME_THRESHOLD})"

    score = sum(signals.values())
    return {"signals": signals, "score": score, "details": details}


def get_sell_signal(row: pd.Series, config, entry_price: float, highest_price: float) -> dict:
    """
    매도 신호를 판단합니다.

    반환값: {
        'should_sell': True/False,
        'reason': 매도 사유
    }
    """
    close = row["close"]
    rsi = row["rsi"]
    bb_pct = row["bb_pct"]

    # 1. 손절 (Stop Loss)
    pnl_pct = (close - entry_price) / entry_price
    if pnl_pct <= -config.STOP_LOSS_PCT:
        return {"should_sell": True, "reason": f"손절 ({pnl_pct*100:.2f}%)"}

    # 2. 익절 (Take Profit)
    if pnl_pct >= config.TAKE_PROFIT_PCT:
        return {"should_sell": True, "reason": f"익절 ({pnl_pct*100:.2f}%)"}

    # 3. 트레일링 스탑 (고점 대비 하락)
    drawdown = (close - highest_price) / highest_price
    if pnl_pct > 0.01 and drawdown <= -config.TRAILING_STOP_PCT:
        return {"should_sell": True, "reason": f"트레일링 스탑 (고점 대비 {drawdown*100:.2f}%)"}

    # 4. 기술적 매도 신호
    sell_signals = 0
    if rsi > config.RSI_OVERBOUGHT:
        sell_signals += 1
    if bb_pct > 0.9:  # 볼린저 상단 근처
        sell_signals += 1
    macd_hist = row["macd_hist"]
    if macd_hist < 0 and row["macd"] > 0:  # MACD 골든크로스 이후 데드크로스 신호
        sell_signals += 1

    if sell_signals >= 2:
        return {"should_sell": True, "reason": f"기술적 매도 신호 {sell_signals}개"}

    return {"should_sell": False, "reason": ""}
