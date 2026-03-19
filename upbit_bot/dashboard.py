"""
관리자 대시보드 (Streamlit)

실시간 차트 + 기술적 지표 + 시장 환경 지표 + 거래 내역을 시각화합니다.

실행: streamlit run dashboard.py
"""

import sys
import json
from datetime import datetime
from pathlib import Path

import streamlit as st
import pandas as pd
from env_utils import load_env_file

load_env_file(Path(__file__).resolve().parent)

import pyupbit

# 프로젝트 모듈
sys.path.insert(0, str(Path(__file__).parent))
from indicators import add_all_indicators
from market_indicators import MarketEnvironment

# ── 페이지 설정 ──
st.set_page_config(
    page_title="업비트 자동매매 대시보드",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

import config as cfg
LOG_FILE = cfg.LOG_FILE
PERFORMANCE_FILE = cfg.PERFORMANCE_FILE


# ──────────────────────────────────────
# 데이터 로드
# ──────────────────────────────────────

@st.cache_data(ttl=30)
def load_trade_log() -> pd.DataFrame:
    if not Path(LOG_FILE).exists():
        return pd.DataFrame()
    df = pd.read_csv(LOG_FILE, encoding="utf-8")
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["date"] = df["timestamp"].dt.date
    df["week"] = df["timestamp"].dt.isocalendar().week.astype(int)
    df["year_week"] = df["timestamp"].dt.strftime("%Y-W%U")
    df["month"] = df["timestamp"].dt.strftime("%Y-%m")
    df["hour"] = df["timestamp"].dt.hour
    return df


@st.cache_data(ttl=30)
def load_performance() -> dict:
    if not Path(PERFORMANCE_FILE).exists():
        return {}
    with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_sell_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["action"] == "SELL"].copy()


@st.cache_data(ttl=60)
def load_chart_data(market: str, unit: int = 60, count: int = 200) -> pd.DataFrame:
    """분봉 차트 데이터를 가져옵니다."""
    df = pyupbit.get_ohlcv(market, interval=f"minute{unit}", count=count)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = ["open", "high", "low", "close", "volume", "value"]
    df = add_all_indicators(df, cfg)
    return df.dropna()


@st.cache_data(ttl=300)
def load_market_environment(market: str) -> dict:
    """시장 환경 지표를 가져옵니다."""
    env = MarketEnvironment()
    return env.get_market_score(market)


# ──────────────────────────────────────
# 사이드바
# ──────────────────────────────────────

st.sidebar.title("📊 자동매매 대시보드")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "페이지",
    ["실시간 차트 & 지표", "거래 내역 & 성과"],
)

chart_market = st.sidebar.selectbox(
    "차트 마켓",
    ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-PEPE", "KRW-SUI"],
    index=0,
)

chart_unit = st.sidebar.selectbox(
    "봉 단위",
    [5, 15, 30, 60, 240],
    index=3,
    format_func=lambda x: f"{x}분봉" if x < 240 else "4시간봉",
)

if st.sidebar.button("🔄 새로고침"):
    st.cache_data.clear()
    st.rerun()


# ══════════════════════════════════════════════════════════════
# 페이지 1: 실시간 차트 & 기술적 지표 & 시장 환경
# ══════════════════════════════════════════════════════════════

if page == "실시간 차트 & 지표":
    st.title(f"📈 {chart_market} 실시간 분석")

    chart_df = load_chart_data(chart_market, chart_unit)
    if chart_df.empty:
        st.error("차트 데이터를 불러올 수 없습니다.")
        st.stop()

    # ── 현재 상태 KPI ──
    last = chart_df.iloc[-1]
    prev = chart_df.iloc[-2]
    change_pct = (last["close"] - prev["close"]) / prev["close"] * 100

    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
    kpi1.metric("현재가", f"{last['close']:,.2f}", delta=f"{change_pct:+.2f}%")
    kpi2.metric("RSI", f"{last['rsi']:.1f}",
                delta="과매도" if last['rsi'] < 30 else ("과매수" if last['rsi'] > 70 else ""))
    kpi3.metric("MACD", f"{last['macd']:.1f}",
                delta="상승" if last['macd_hist'] > 0 else "하락")
    kpi4.metric("BB %", f"{last['bb_pct']:.2f}",
                delta="하단" if last['bb_pct'] < 0.2 else ("상단" if last['bb_pct'] > 0.8 else ""))
    kpi5.metric("ATR %", f"{last['atr_pct']:.2f}%")
    kpi6.metric("거래량 배율", f"{last['volume_ratio']:.2f}x",
                delta="급증" if last['volume_ratio'] > 2 else "")

    st.markdown("---")

    # ══════════════════════════════════════
    # 가격 차트 + 볼린저 밴드 + EMA
    # ══════════════════════════════════════

    st.subheader("가격 차트 (볼린저 밴드 + EMA)")
    price_chart_df = chart_df[["close", "bb_upper", "bb_middle", "bb_lower", "ema_short", "ema_long"]].copy()
    price_chart_df.columns = ["종가", "BB 상단", "BB 중간", "BB 하단", f"EMA {cfg.EMA_SHORT}", f"EMA {cfg.EMA_LONG}"]
    st.line_chart(price_chart_df, use_container_width=True, height=400)

    # ── 보조지표 차트들 ──
    col_ind1, col_ind2 = st.columns(2)

    with col_ind1:
        st.subheader("RSI")
        rsi_df = pd.DataFrame({
            "RSI": chart_df["rsi"],
            "과매도 (30)": 30,
            "과매수 (70)": 70,
        }, index=chart_df.index)
        st.line_chart(rsi_df, use_container_width=True, height=250)

    with col_ind2:
        st.subheader("MACD")
        macd_df = pd.DataFrame({
            "MACD": chart_df["macd"],
            "Signal": chart_df["macd_signal"],
            "Histogram": chart_df["macd_hist"],
        }, index=chart_df.index)
        st.line_chart(macd_df, use_container_width=True, height=250)

    col_ind3, col_ind4 = st.columns(2)

    with col_ind3:
        st.subheader("거래량 & 거래량 MA")
        vol_df = pd.DataFrame({
            "거래량": chart_df["volume"],
            f"MA {cfg.VOLUME_MA_PERIOD}": chart_df["volume_ma"],
        }, index=chart_df.index)
        st.bar_chart(vol_df, use_container_width=True, height=250)

    with col_ind4:
        st.subheader("BB% (볼린저 밴드 위치)")
        bb_df = pd.DataFrame({
            "BB%": chart_df["bb_pct"],
            "하단 기준 (0.2)": 0.2,
            "상단 기준 (0.8)": 0.8,
        }, index=chart_df.index)
        st.line_chart(bb_df, use_container_width=True, height=250)

    st.markdown("---")

    # ══════════════════════════════════════
    # 시장 환경 지표 (기술적 지표 외 매개변수)
    # ══════════════════════════════════════

    st.subheader("📡 시장 환경 지표 (기술적 지표 외)")

    env_data = load_market_environment(chart_market)
    d = env_data["details"]

    env_col1, env_col2 = st.columns(2)

    with env_col1:
        # 공포탐욕 + 종합 점수
        fgi = d["fear_greed"]
        score = env_data["score"]

        st.markdown("**공포탐욕 지수 (Fear & Greed)**")
        fgi_col1, fgi_col2 = st.columns([1, 2])
        with fgi_col1:
            fgi_val = fgi["value"]
            fgi_color = "🟢" if fgi_val <= 30 else ("🟡" if fgi_val <= 60 else "🔴")
            st.metric("지수", f"{fgi_color} {fgi_val}/100")
        with fgi_col2:
            st.write(f"**상태:** {fgi['classification']}")
            st.write(f"**신호:** {fgi['signal']}")

        st.markdown("---")
        st.markdown("**종합 시장 점수**")
        score_color = "🟢" if score >= 10 else ("🟡" if score >= -10 else "🔴")
        st.metric("시장 점수", f"{score_color} {score:+d}/100")
        st.write(f"**판단:** {env_data['recommendation']}")

    with env_col2:
        # 김치 프리미엄 + 호가 + 거래량
        kimchi = d["kimchi_premium"]
        ob = d["orderbook_pressure"]
        vol = d["volume_trend"]

        st.markdown("**김치 프리미엄**")
        prem = kimchi["premium_pct"]
        prem_color = "🔴" if prem > 3 else ("🟡" if prem > 1 else "🟢")
        st.write(f"{prem_color} **{prem:+.2f}%** — {kimchi['signal']}")
        if kimchi.get("upbit_price"):
            st.caption(f"업비트: {kimchi['upbit_price']:,.0f} | 바이낸스(환산): {kimchi['binance_price_krw']:,.0f}")

        st.markdown("**호가창 매수/매도 압력**")
        bid_r = ob["bid_ratio"]
        ob_color = "🟢" if bid_r > 0.55 else ("🔴" if bid_r < 0.45 else "🟡")
        st.write(f"{ob_color} **매수 {bid_r:.1%}** / 매도 {1-bid_r:.1%} — {ob['signal']}")

        st.markdown("**거래량 추세 (3일 평균 대비)**")
        vr = vol["ratio"]
        vol_color = "🟢" if vr > 1.2 else ("🔴" if vr < 0.6 else "🟡")
        st.write(f"{vol_color} **{vr:.2f}x** — {vol['signal']}")

    st.markdown("---")

    # ══════════════════════════════════════
    # 신호 요약
    # ══════════════════════════════════════

    st.subheader("🎯 현재 매수 신호 요약")

    from indicators import get_signal_score
    signal_result = get_signal_score(last, cfg)
    signals = signal_result["signals"]
    details = signal_result["details"]

    sig_cols = st.columns(5)
    signal_names = {"rsi": "RSI", "macd": "MACD", "bollinger": "볼린저", "ema": "EMA", "volume": "거래량"}

    for i, (key, label) in enumerate(signal_names.items()):
        active = signals.get(key, False)
        with sig_cols[i]:
            icon = "✅" if active else "❌"
            st.metric(f"{icon} {label}", "ON" if active else "OFF")
            st.caption(details.get(key, ""))

    total_signals = signal_result["score"]
    min_required = cfg.MIN_SIGNAL_COUNT
    if total_signals >= min_required:
        st.success(f"매수 신호 {total_signals}/5 — 진입 조건 충족! (최소 {min_required}개 필요)")
    else:
        st.warning(f"매수 신호 {total_signals}/5 — 진입 조건 미달 (최소 {min_required}개 필요)")


# ══════════════════════════════════════════════════════════════
# 페이지 2: 거래 내역 & 성과
# ══════════════════════════════════════════════════════════════

elif page == "거래 내역 & 성과":
    st.title("📊 거래 내역 & 성과 분석")

    trade_df = load_trade_log()
    perf = load_performance()

    if trade_df.empty:
        st.warning("아직 거래 기록이 없습니다. 봇을 실행하면 여기에 거래 내역이 표시됩니다.")
        st.stop()

    sell_df = get_sell_trades(trade_df)

    view_mode = st.radio("조회 기간", ["전체", "일별", "주별", "월별"], horizontal=True)

    st.markdown("---")

    # ── KPI ──
    total_trades = len(sell_df)
    winning = sell_df[sell_df["pnl_krw"] >= 0] if not sell_df.empty else pd.DataFrame()
    losing = sell_df[sell_df["pnl_krw"] < 0] if not sell_df.empty else pd.DataFrame()
    win_rate = len(winning) / total_trades * 100 if total_trades > 0 else 0
    total_pnl = sell_df["pnl_krw"].sum() if not sell_df.empty else 0
    total_fees = trade_df["fee"].sum() if not trade_df.empty else 0

    initial_capital = perf.get("paper_capital", 1_000_000)
    current_capital = perf.get("paper_current", initial_capital)
    total_return = (current_capital - initial_capital) / initial_capital * 100

    kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
    kpi1.metric("총 거래", f"{total_trades}회")
    kpi2.metric("승률", f"{win_rate:.1f}%", delta=f"{len(winning)}승 {len(losing)}패")
    kpi3.metric("순손익", f"{total_pnl:+,.0f}원", delta=f"{total_return:+.2f}%")
    kpi4.metric("총 수수료", f"{total_fees:,.0f}원")
    kpi5.metric("현재 자금", f"{current_capital:,.0f}원",
                delta=f"{current_capital - initial_capital:+,.0f}원")

    st.markdown("---")

    # ── 수익 차트 ──
    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        st.subheader("📈 누적 수익 곡선")
        if not sell_df.empty:
            cumulative = sell_df.sort_values("timestamp").copy()
            cumulative["cumulative_pnl"] = cumulative["pnl_krw"].cumsum()
            st.line_chart(cumulative.set_index("timestamp")["cumulative_pnl"], use_container_width=True)

    with col_chart2:
        st.subheader("📊 거래별 손익")
        if not sell_df.empty:
            st.bar_chart(sell_df.sort_values("timestamp").set_index("timestamp")["pnl_krw"],
                         use_container_width=True)

    st.markdown("---")

    # ── 기간별 분석 ──
    if view_mode in ("일별", "전체") and not sell_df.empty:
        st.subheader("📅 일별 수익")
        daily = sell_df.groupby("date").agg(
            거래수=("pnl_krw", "count"),
            총손익=("pnl_krw", "sum"),
            평균손익=("pnl_krw", "mean"),
            승률=("pnl_krw", lambda x: (x >= 0).sum() / len(x) * 100),
        ).reset_index()
        daily.columns = ["날짜", "거래수", "총손익", "평균손익", "승률(%)"]
        st.bar_chart(daily.set_index("날짜")["총손익"], use_container_width=True)
        st.dataframe(daily, use_container_width=True)

    if view_mode in ("주별", "전체") and not sell_df.empty:
        st.subheader("📆 주별 수익")
        weekly = sell_df.groupby("year_week").agg(
            거래수=("pnl_krw", "count"),
            총손익=("pnl_krw", "sum"),
            승률=("pnl_krw", lambda x: (x >= 0).sum() / len(x) * 100),
        ).reset_index()
        weekly.columns = ["주차", "거래수", "총손익", "승률(%)"]
        st.bar_chart(weekly.set_index("주차")["총손익"], use_container_width=True)
        st.dataframe(weekly, use_container_width=True)

    if view_mode in ("월별", "전체") and not sell_df.empty:
        st.subheader("📅 월별 수익")
        monthly = sell_df.groupby("month").agg(
            거래수=("pnl_krw", "count"),
            총손익=("pnl_krw", "sum"),
            승률=("pnl_krw", lambda x: (x >= 0).sum() / len(x) * 100),
        ).reset_index()
        monthly.columns = ["월", "거래수", "총손익", "승률(%)"]
        st.bar_chart(monthly.set_index("월")["총손익"], use_container_width=True)
        st.dataframe(monthly, use_container_width=True)

    st.markdown("---")

    # ── 코인별 / 시간대별 ──
    col_m, col_h = st.columns(2)

    with col_m:
        st.subheader("🪙 코인별 성과")
        if not sell_df.empty:
            market_perf = sell_df.groupby("market").agg(
                거래수=("pnl_krw", "count"),
                총손익=("pnl_krw", "sum"),
                평균손익=("pnl_pct", "mean"),
                승률=("pnl_krw", lambda x: (x >= 0).sum() / len(x) * 100),
            ).sort_values("총손익", ascending=False).reset_index()
            market_perf.columns = ["마켓", "거래수", "총손익", "평균손익(%)", "승률(%)"]
            st.dataframe(market_perf, use_container_width=True)

    with col_h:
        st.subheader("🕐 시간대별 성과")
        if not sell_df.empty:
            hourly = sell_df.groupby("hour").agg(
                거래수=("pnl_krw", "count"),
                평균손익=("pnl_krw", "mean"),
            ).reset_index()
            hourly.columns = ["시간", "거래수", "평균손익"]
            st.bar_chart(hourly.set_index("시간")["평균손익"], use_container_width=True)

    st.markdown("---")

    # ── 전체 거래 내역 ──
    st.subheader("📋 전체 거래 내역")
    sort_order = st.radio("정렬", ["최신순", "오래된순"], horizontal=True, key="sort_trade")
    display_df = trade_df.sort_values("timestamp", ascending=(sort_order == "오래된순")).copy()
    display_df["timestamp"] = display_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    default_cols = [c for c in ["timestamp", "action", "market", "price", "pnl_krw", "pnl_pct", "reason"]
                    if c in display_df.columns]
    st.dataframe(display_df[default_cols], use_container_width=True, height=500)

    # ── 리스크 지표 ──
    if not sell_df.empty:
        st.markdown("---")
        st.subheader("📈 리스크 지표")
        col_s1, col_s2, col_s3 = st.columns(3)

        with col_s1:
            st.markdown("**수익 거래**")
            if not winning.empty:
                st.write(f"- 건수: {len(winning)}회")
                st.write(f"- 평균: {winning['pnl_pct'].mean():+.2f}%")
                st.write(f"- 최대: {winning['pnl_pct'].max():+.2f}%")

        with col_s2:
            st.markdown("**손실 거래**")
            if not losing.empty:
                st.write(f"- 건수: {len(losing)}회")
                st.write(f"- 평균: {losing['pnl_pct'].mean():+.2f}%")
                st.write(f"- 최대: {losing['pnl_pct'].min():+.2f}%")

        with col_s3:
            avg_win = winning["pnl_pct"].mean() if not winning.empty else 0
            avg_loss = abs(losing["pnl_pct"].mean()) if not losing.empty else 1
            pf = (winning["pnl_krw"].sum() / abs(losing["pnl_krw"].sum())
                  if not losing.empty and losing["pnl_krw"].sum() != 0 else float("inf"))
            st.markdown("**비율**")
            st.write(f"- 프로핏 팩터: {pf:.2f}")
            st.write(f"- R:R 비율: {avg_win/avg_loss:.2f}" if avg_loss > 0 else "- R:R: N/A")

            cumulative_pnl = sell_df.sort_values("timestamp")["pnl_krw"].cumsum()
            peak = cumulative_pnl.cummax()
            mdd = (cumulative_pnl - peak).min()
            st.write(f"- MDD: {mdd:+,.0f}원")

# ── 푸터 ──
st.markdown("---")
st.caption(f"마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
