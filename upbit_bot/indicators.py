"""
기술적 지표 계산 모듈
"""

import pandas as pd
import numpy as np


def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Relative Strength Index) 계산 — Wilder 스무딩 방식"""
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


def calculate_obv(closes: pd.Series, volumes: pd.Series) -> pd.Series:
    """
    OBV (On-Balance Volume) 계산 — 스마트머니 매집/분산 감지
    상승 시 거래량 누적, 하락 시 차감
    """
    direction = np.sign(closes.diff().fillna(0))
    return (direction * volumes).cumsum()


def calculate_stoch_rsi(rsi: pd.Series, period: int = 14) -> pd.Series:
    """
    Stochastic RSI 계산 (0~100 범위)
    RSI의 RSI — 일반 RSI보다 빠르게 과매도/과매수 감지
    """
    rsi_min = rsi.rolling(period).min()
    rsi_max = rsi.rolling(period).max()
    denom = rsi_max - rsi_min
    stoch = (rsi - rsi_min) / denom.replace(0, np.nan)
    return (stoch * 100).fillna(50)


def calculate_bb_width(upper: pd.Series, lower: pd.Series, middle: pd.Series) -> pd.Series:
    """
    볼린저 밴드 폭 = (upper - lower) / middle
    값이 낮을수록 스퀴즈(수축) 상태 → 폭발적 움직임 예고
    """
    return (upper - lower) / middle.replace(0, np.nan)


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

    # ── 추가 지표 ──
    # OBV: 스마트머니 방향성 감지
    df["obv"] = calculate_obv(closes, volumes)
    df["obv_ma"] = df["obv"].rolling(10).mean()

    # Stochastic RSI: 빠른 과매도/과매수 감지
    df["stoch_rsi"] = calculate_stoch_rsi(df["rsi"], period=14)

    # BB Width: 스퀴즈(밴드 수축) 감지 → 돌파 예고 신호
    df["bb_width"] = calculate_bb_width(df["bb_upper"], df["bb_lower"], df["bb_middle"])
    df["bb_width_ma"] = df["bb_width"].rolling(20).mean()

    # RSI 기울기 (이전 대비 방향)
    df["rsi_prev"] = df["rsi"].shift(2)

    return df


def get_signal_score(row: pd.Series, config) -> dict:
    """
    현재 캔들의 매수 신호 점수를 계산합니다.
    세계 최고 트레이더 전략 종합 반영:
    - RSI 기울기 반전 확인 (Stan Weinstein: 과매도 후 반등 확인)
    - MACD 제로선 하방 골든크로스 (Larry Williams: 최강 반전 신호)
    - BB 스퀴즈 + 하단 반등 (볼린저밴드 발명자 John Bollinger)
    - EMA 골든크로스 + 기울기 (Mark Minervini: 모든 EMA 우상향)
    - OBV + 거래량 급등 (스마트머니 축적 확인)

    반환값: {
        'signals': {지표명: True/False},
        'score': 총 True 개수,
        'details': 상세 설명,
        'strength': 신호 강도 (0.0~5.0, 소수점)
    }
    """
    signals = {}
    details = {}
    strength_scores = {}  # 각 신호의 강도 (0.0~1.0)

    # ── 1. RSI 신호 ──
    # 기본: RSI < 과매도 기준
    # 강화: RSI가 2캔들 전보다 상승 중이면 반전 확인 (더 강한 신호)
    rsi = row["rsi"]
    rsi_prev2 = row.get("rsi_prev", np.nan)
    rsi_oversold = rsi < config.RSI_OVERSOLD
    rsi_rising = pd.notna(rsi_prev2) and rsi > rsi_prev2  # RSI 반등 중
    stoch_rsi = row.get("stoch_rsi", 50.0)

    # RSI 과매도 + 상승 반전 = 강한 신호 / 과매도만 = 일반 신호
    if rsi_oversold and rsi_rising:
        signals["rsi"] = True
        strength_scores["rsi"] = 1.0  # 강한 반전 신호
    elif rsi_oversold:
        signals["rsi"] = True
        strength_scores["rsi"] = 0.7
    elif stoch_rsi < 20 and rsi_rising:  # Stoch RSI 극과매도 + 상승
        signals["rsi"] = True
        strength_scores["rsi"] = 0.6
    else:
        signals["rsi"] = False
        strength_scores["rsi"] = 0.0
    details["rsi"] = f"RSI={rsi:.1f}(기준≤{config.RSI_OVERSOLD}) StochRSI={stoch_rsi:.0f} {'↑반전' if rsi_rising else ''}"

    # ── 2. MACD 신호 ──
    # 최강: 제로선 하방에서 히스토그램 반전 (MACD < 0 이면서 hist 전환)
    # 강: 히스토그램 음→양 전환 (골든크로스)
    # 보통: 히스토그램 + MACD 동시 상승 중
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
    # 제로선 하방 골든크로스 = 가장 신뢰도 높은 신호
    zero_line_cross = hist_cross_up and macd < 0

    if zero_line_cross:
        signals["macd"] = True
        strength_scores["macd"] = 1.0  # 최강 신호
    elif hist_cross_up:
        signals["macd"] = True
        strength_scores["macd"] = 0.8
    elif (macd_hist > 0 and macd < 0) or momentum_turn_up:
        signals["macd"] = True
        strength_scores["macd"] = 0.5
    else:
        signals["macd"] = False
        strength_scores["macd"] = 0.0
    details["macd"] = f"MACD={macd:.4f}, Hist={macd_hist:.4f} {'제로선하방크로스!' if zero_line_cross else ('골든크로스' if hist_cross_up else '')}"

    # ── 3. 볼린저 밴드 신호 ──
    # 스퀴즈(밴드 수축) 후 하단에서 반등 = 폭발적 상승 전조
    # 기본: bb_pct < 0.25 (하단 25% 구간)
    bb_pct = row["bb_pct"]
    bb_width = row.get("bb_width", np.nan)
    bb_width_ma = row.get("bb_width_ma", np.nan)
    close = row["close"]
    open_price = row.get("open", close)

    # BB 스퀴즈 감지 (밴드 폭이 평균보다 좁으면 돌파 임박)
    bb_squeeze = pd.notna(bb_width) and pd.notna(bb_width_ma) and bb_width < bb_width_ma * 0.9
    # 양봉 확인 (하단에서 실제로 반등 중)
    bullish_candle = close > open_price

    if bb_pct < 0.15 and bullish_candle:  # 하단 15% + 양봉
        signals["bollinger"] = True
        strength_scores["bollinger"] = 1.0
    elif bb_pct < 0.25 and bb_squeeze:  # 하단 25% + 스퀴즈
        signals["bollinger"] = True
        strength_scores["bollinger"] = 0.8
    elif bb_pct < 0.25:  # 하단 25%
        signals["bollinger"] = True
        strength_scores["bollinger"] = 0.6
    else:
        signals["bollinger"] = False
        strength_scores["bollinger"] = 0.0
    details["bollinger"] = f"BB%={bb_pct:.2f} {'스퀴즈!' if bb_squeeze else ''} {'양봉' if bullish_candle else '음봉'}"

    # ── 4. EMA 추세 신호 ──
    # Minervini SEPA: 단기>장기 EMA + EMA200 위에 있거나 5% 이내 + EMA 자체가 우상향
    ema_short = row["ema_short"]
    ema_long = row["ema_long"]
    ema_trend = row["ema_trend"]
    near_trend = close >= ema_trend * 0.95

    # EMA 정배열: 단기 > 장기 EMA
    ema_aligned = ema_short > ema_long
    # 이상적 조건: close > ema_short > ema_long (가격이 모든 EMA 위)
    full_alignment = close > ema_short > ema_long

    if full_alignment and near_trend:
        signals["ema"] = True
        strength_scores["ema"] = 1.0  # 완전 정배열
    elif ema_aligned and near_trend:
        signals["ema"] = True
        strength_scores["ema"] = 0.7
    else:
        signals["ema"] = False
        strength_scores["ema"] = 0.0
    _ema_fmt = ".0f" if ema_short >= 100 else (".2f" if ema_short >= 1 else ".4f")
    details["ema"] = (
        f"EMA단기={ema_short:{_ema_fmt}}, EMA장기={ema_long:{_ema_fmt}}, "
        f"추세={'완전정배열' if full_alignment else ('정배열' if ema_aligned else 'X')}"
    )

    # ── 5. 거래량 + OBV 신호 ──
    # 기본: 거래량 평균 대비 급증
    # 강화: OBV가 이동평균 위에 있으면 스마트머니 매집 확인
    volume_ratio = row["volume_ratio"]
    obv = row.get("obv", np.nan)
    obv_ma = row.get("obv_ma", np.nan)
    obv_rising = pd.notna(obv) and pd.notna(obv_ma) and obv > obv_ma  # OBV > OBV MA = 매집

    if volume_ratio >= config.VOLUME_THRESHOLD and obv_rising:
        signals["volume"] = True
        strength_scores["volume"] = 1.0  # 거래량 + OBV 동시 확인
    elif volume_ratio >= config.VOLUME_THRESHOLD:
        signals["volume"] = True
        strength_scores["volume"] = 0.7
    elif obv_rising and volume_ratio >= config.VOLUME_THRESHOLD * 0.8:  # OBV는 좋지만 거래량 약간 부족
        signals["volume"] = True
        strength_scores["volume"] = 0.5
    else:
        signals["volume"] = False
        strength_scores["volume"] = 0.0
    details["volume"] = f"거래량배율={volume_ratio:.2f}(기준≥{config.VOLUME_THRESHOLD}) OBV={'매집' if obv_rising else '분산'}"

    score = sum(signals.values())
    strength = sum(strength_scores[k] for k in signals if signals[k])
    return {"signals": signals, "score": score, "details": details, "strength": round(strength, 2)}


def get_sell_signal(
    row: pd.Series,
    config,
    entry_price: float,
    highest_price: float,
    current_pnl_pct: float = None,
) -> dict:
    """
    매도 신호를 판단합니다.

    Args:
        current_pnl_pct: 현재 손익률(%). None이면 내부 계산.
                         TECHNICAL_EXIT_MIN_PCT 체크에 사용됩니다.

    반환값: {
        'should_sell': True/False,
        'reason': 매도 사유
    }
    """
    close = row["close"]
    rsi = row["rsi"]
    bb_pct = row["bb_pct"]

    pnl_pct = (close - entry_price) / entry_price
    if current_pnl_pct is None:
        current_pnl_pct = pnl_pct * 100

    # 1. 본전 보호 스탑 (Breakeven Stop)
    breakeven_trigger = getattr(config, "BREAKEVEN_TRIGGER_PCT", 0.0)
    if breakeven_trigger > 0:
        fee_buffer = getattr(config, "FEE_RATE", 0.0005) * 2
        breakeven_sl = entry_price * (1 + fee_buffer)
        # 한 번이라도 BREAKEVEN_TRIGGER_PCT 이상 수익을 달성했다면 본전 스탑 적용
        peak_pnl = (highest_price - entry_price) / entry_price
        if peak_pnl >= breakeven_trigger and close <= breakeven_sl:
            return {"should_sell": True, "reason": f"본전 보호 스탑 ({current_pnl_pct:.2f}%)"}

    # 2. 손절 (Stop Loss)
    if pnl_pct <= -config.STOP_LOSS_PCT:
        return {"should_sell": True, "reason": f"손절 ({pnl_pct*100:.2f}%)"}

    # 3. 익절 (Take Profit)
    if pnl_pct >= config.TAKE_PROFIT_PCT:
        return {"should_sell": True, "reason": f"익절 ({pnl_pct*100:.2f}%)"}

    # 4. 트레일링 스탑 (고점 대비 하락)
    drawdown = (close - highest_price) / highest_price
    if pnl_pct > 0.01 and drawdown <= -config.TRAILING_STOP_PCT:
        return {"should_sell": True, "reason": f"트레일링 스탑 (고점 대비 {drawdown*100:.2f}%)"}

    # 5. 기술적 매도 신호 (다중 확인)
    sell_signals = 0
    sell_reasons = []

    if rsi > config.RSI_OVERBOUGHT:
        sell_signals += 1
        sell_reasons.append(f"RSI과매수({rsi:.0f})")

    if bb_pct > 0.9:  # 볼린저 상단 근처
        sell_signals += 1
        sell_reasons.append("BB상단")

    macd_hist = row["macd_hist"]
    if macd_hist < 0 and row["macd"] > 0:  # MACD 골든크로스 이후 데드크로스 신호
        sell_signals += 1
        sell_reasons.append("MACD데드크로스")

    # 거래량 급증 + 음봉 = 분배 신호
    volume_ratio = row.get("volume_ratio", 1.0)
    open_price = row.get("open", close)
    if volume_ratio > 2.5 and close < open_price:
        sell_signals += 1
        sell_reasons.append(f"거래량급증+음봉({volume_ratio:.1f}x)")

    if sell_signals >= 2:
        # 소수익 구간(0% ~ TECHNICAL_EXIT_MIN_PCT)에서는 기술적 신호 청산 차단
        # → 조기 청산 방지, 손실 중이거나 충분한 수익이면 정상 청산
        min_exit_pct = getattr(config, "TECHNICAL_EXIT_MIN_PCT", 0.0) * 100
        if current_pnl_pct < 0 or current_pnl_pct >= min_exit_pct:
            return {"should_sell": True, "reason": f"기술적 매도 신호: {'+'.join(sell_reasons)}"}

    return {"should_sell": False, "reason": ""}
