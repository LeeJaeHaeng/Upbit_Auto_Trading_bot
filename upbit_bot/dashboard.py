"""
관리자 대시보드 (Streamlit)

실시간 차트 + 기술적 지표 + 시장 환경 지표 + 거래 내역 + 봇 제어 + 뉴스

실행: streamlit run dashboard.py
"""

import os
import sys
import re
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import psutil
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
from env_utils import load_env_file

load_env_file(Path(__file__).resolve().parent)

import pyupbit

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
BOT_PID_FILE = Path(__file__).parent / "bot_pid.json"
LIVE_STATUS_FILE = Path(__file__).parent / "live_status.json"
BOT_DIR = Path(__file__).parent


# ──────────────────────────────────────
# 봇 프로세스 제어
# ──────────────────────────────────────

def _is_pid_running(pid: int) -> bool:
    """PID로 프로세스 실행 여부 확인 (psutil 사용)"""
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def get_bot_status() -> dict:
    """봇 실행 상태 반환"""
    if not BOT_PID_FILE.exists():
        return {"running": False, "pid": None, "mode": None, "started_at": None}
    try:
        with open(BOT_PID_FILE, encoding="utf-8") as f:
            info = json.load(f)
        pid = info.get("pid")
        if pid and _is_pid_running(pid):
            return {"running": True, **info}
    except Exception:
        pass
    BOT_PID_FILE.unlink(missing_ok=True)
    return {"running": False, "pid": None, "mode": None, "started_at": None}


def start_bot(live: bool = False) -> int:
    """봇 프로세스 시작 — 독립 프로세스 그룹으로 실행"""
    args = [sys.executable, "main.py"]
    if live:
        args.append("--live")
    proc = subprocess.Popen(
        args,
        cwd=str(BOT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    info = {
        "pid": proc.pid,
        "mode": "live" if live else "paper",
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(BOT_PID_FILE, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    return proc.pid


def stop_bot(pid: int) -> bool:
    """봇 프로세스 강제 종료 (Windows taskkill)"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        )
    except Exception:
        pass
    BOT_PID_FILE.unlink(missing_ok=True)
    return True


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
    df = pyupbit.get_ohlcv(market, interval=f"minute{unit}", count=count)
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = ["open", "high", "low", "close", "volume", "value"]
    df = add_all_indicators(df, cfg)
    return df.dropna()


@st.cache_data(ttl=300)
def load_market_environment(market: str) -> dict:
    env = MarketEnvironment()
    return env.get_market_score(market)


# ──────────────────────────────────────
# 뉴스 크롤링
# ──────────────────────────────────────

RSS_FEEDS = [
    {"name": "CoinDesk",          "emoji": "🇺🇸", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"},
    {"name": "CoinTelegraph",     "emoji": "📰", "url": "https://cointelegraph.com/rss"},
    {"name": "Bitcoin Magazine",  "emoji": "₿",  "url": "https://bitcoinmagazine.com/feed"},
    {"name": "Decrypt",           "emoji": "🔓", "url": "https://decrypt.co/feed"},
    {"name": "BlockMedia (KR)",   "emoji": "🇰🇷", "url": "https://www.blockmedia.co.kr/feed"},
    {"name": "코인리더스 (KR)",   "emoji": "🇰🇷", "url": "https://www.coinreaders.com/feed"},
]


@st.cache_data(ttl=300)
def fetch_crypto_news() -> list:
    """여러 RSS 피드에서 암호화폐 뉴스를 수집 후 날짜순 정렬"""
    news = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    for feed in RSS_FEEDS:
        try:
            resp = requests.get(feed["url"], headers=headers, timeout=10)
            if resp.status_code != 200:
                continue

            # XML 파싱 (RSS 2.0 / Atom 공통 처리)
            root = ET.fromstring(resp.content)
            channel = root.find("channel")
            items = channel.findall("item") if channel is not None else []

            # Atom 피드 fallback
            if not items:
                ns_atom = "http://www.w3.org/2005/Atom"
                items = root.findall(f"{{{ns_atom}}}entry")

            for item in items[:6]:
                title = (
                    item.findtext("title")
                    or item.findtext("{http://www.w3.org/2005/Atom}title")
                    or ""
                ).strip()

                link_el = item.find("link")
                if link_el is not None and link_el.text:
                    link = link_el.text.strip()
                elif link_el is not None:
                    link = link_el.get("href", "").strip()
                else:
                    link = (item.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()

                pub = (
                    item.findtext("pubDate")
                    or item.findtext("{http://www.w3.org/2005/Atom}published")
                    or item.findtext("{http://www.w3.org/2005/Atom}updated")
                    or ""
                ).strip()

                desc = (
                    item.findtext("description")
                    or item.findtext("{http://www.w3.org/2005/Atom}summary")
                    or item.findtext("{http://www.w3.org/2005/Atom}content")
                    or ""
                ).strip()

                # HTML 태그 제거 및 길이 제한
                desc = re.sub(r"<[^>]+>", "", desc)
                desc = re.sub(r"\s+", " ", desc).strip()[:280]

                # 날짜 파싱
                try:
                    dt = parsedate_to_datetime(pub)
                    date_str = dt.strftime("%Y-%m-%d %H:%M")
                    sort_key = dt.timestamp()
                except Exception:
                    date_str = pub[:16] if len(pub) >= 10 else pub
                    sort_key = 0

                if title and link:
                    news.append({
                        "source": f"{feed['emoji']} {feed['name']}",
                        "title": title,
                        "link": link,
                        "date": date_str,
                        "summary": desc,
                        "_sort_key": sort_key,
                    })
        except Exception:
            continue

    news.sort(key=lambda x: x["_sort_key"], reverse=True)
    return news


# ──────────────────────────────────────
# 사이드바
# ──────────────────────────────────────

st.sidebar.title("📊 자동매매 대시보드")
st.sidebar.markdown("---")

# ── 봇 제어 패널 ──
st.sidebar.subheader("🤖 봇 제어")

bot_status = get_bot_status()

if bot_status["running"]:
    mode_label = "🔴 실거래" if bot_status.get("mode") == "live" else "🟡 페이퍼"
    st.sidebar.success(f"**실행 중** {mode_label}")
    st.sidebar.caption(
        f"PID: {bot_status['pid']} | 시작: {bot_status.get('started_at', '-')}"
    )
    if st.sidebar.button("⏹ 봇 종료", type="primary", use_container_width=True):
        stop_bot(bot_status["pid"])
        st.sidebar.warning("봇이 종료되었습니다.")
        st.rerun()
else:
    st.sidebar.error("**정지 중**")
    trading_mode = st.sidebar.radio(
        "모드 선택",
        ["페이퍼 트레이딩 (모의)", "실거래 (실제 자금)"],
        index=0,
    )
    is_live = trading_mode == "실거래 (실제 자금)"

    if is_live:
        st.sidebar.warning("⚠️ 실거래 모드: 실제 자금이 사용됩니다!")
        live_confirm = st.sidebar.checkbox("실거래 시작에 동의합니다")
    else:
        live_confirm = True

    if st.sidebar.button(
        "▶ 봇 시작",
        type="primary",
        use_container_width=True,
        disabled=(is_live and not live_confirm),
    ):
        pid = start_bot(live=is_live)
        st.sidebar.success(f"봇이 시작되었습니다! (PID: {pid})")
        st.rerun()

st.sidebar.markdown("---")

# ── 페이지 선택 ──
page = st.sidebar.radio(
    "페이지",
    [
        "🔴 실시간 현황",
        "실시간 차트 & 지표",
        "거래 내역 & 성과",
        "💰 내 업비트 지갑",
        "📰 비트코인 뉴스",
    ],
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
# 페이지 1: 실시간 현황 (봇 상태 · 모의 포지션 · 미실현 손익)
# ══════════════════════════════════════════════════════════════

def _load_live_status() -> dict:
    """live_status.json 읽기 (캐시 없이 매번 읽어 최신 값 보장)"""
    try:
        if LIVE_STATUS_FILE.exists():
            with open(LIVE_STATUS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


STATE_KO = {
    "idle":        ("🔍 IDLE",       "대기 중 — 매수 기회 탐색"),
    "buy_waiting": ("⏳ BUY_WAITING", "지정가 매수 주문 체결 대기"),
    "position":    ("📦 POSITION",   "포지션 보유 중 — 익절/손절 대기"),
    "stopped":     ("⏹ 정지",        "봇이 실행 중이지 않습니다"),
}


if page == "🔴 실시간 현황":
    # 자동 새로고침: 10초마다 (봇 CHECK_INTERVAL 과 동기화)
    st_autorefresh(interval=10_000, key="live_refresh")

    st.title("🔴 실시간 현황")
    st.caption("봇 상태·모의 포지션·미실현 손익을 10초마다 자동 갱신합니다.")

    live = _load_live_status()

    if not live:
        st.warning("봇이 실행 중이지 않거나 아직 첫 사이클이 완료되지 않았습니다.\n\n"
                   "사이드바에서 **봇 시작** 버튼을 누른 후 10~30초를 기다리세요.")
        st.stop()

    raw_state = live.get("state", "stopped")
    state_label, state_desc = STATE_KO.get(raw_state, (raw_state, ""))
    mode_str = "💰 실거래" if live.get("mode") == "live" else "📄 페이퍼 트레이딩"
    updated = live.get("last_updated", "")

    # ── 상단 상태 배너 ──
    if raw_state == "position":
        st.success(f"**{state_label}** — {state_desc}  |  {mode_str}")
    elif raw_state == "buy_waiting":
        st.warning(f"**{state_label}** — {state_desc}  |  {mode_str}")
    elif raw_state == "idle":
        st.info(f"**{state_label}** — {state_desc}  |  {mode_str}")
    else:
        st.error(f"**{state_label}**  |  {mode_str}")

    st.caption(f"마지막 봇 갱신: {updated}")
    st.markdown("---")

    # ── KPI 행 ──
    paper_capital = live.get("paper_capital", 0)
    kpi_cols = st.columns(4)

    kpi_cols[0].metric(
        "모의 잔고",
        f"{paper_capital:,.0f} 원",
        help="현재 페이퍼 트레이딩 잔고 (매수 후에는 잔고가 줄고 매도 후 복구됩니다)",
    )

    if raw_state == "position" and live.get("entry_price"):
        upnl_pct = live.get("unrealized_pct", 0)
        upnl_krw = live.get("unrealized_krw", 0)
        pos_val  = live.get("position_value_krw", 0)
        cur_price = live.get("current_price", 0)

        kpi_cols[1].metric(
            "미실현 손익",
            f"{upnl_krw:+,.0f} 원",
            delta=f"{upnl_pct:+.2f}%",
            delta_color="normal",
        )
        kpi_cols[2].metric("포지션 평가액", f"{pos_val:,.0f} 원")
        kpi_cols[3].metric("현재가", f"{cur_price:,.0f} 원")

        st.markdown("---")

        # ── 포지션 상세 ──
        st.subheader(f"📦 포지션 상세 — {live.get('market', '')}")

        avg_price = live.get("avg_entry_price") or live.get("entry_price", 0)
        entry_price = live.get("entry_price", 0)
        coin_qty = live.get("coin_qty", 0)
        highest_price = live.get("highest_price", 0)
        tp_price = live.get("tp_price", 0)
        sl_price = live.get("sl_price", 0)
        dca_done = live.get("dca_done", False)
        be_activated = live.get("breakeven_activated", False)

        col_d1, col_d2, col_d3 = st.columns(3)
        with col_d1:
            st.markdown("**진입 정보**")
            st.write(f"- 1차 매수가: **{entry_price:,.0f}** 원")
            if dca_done:
                st.write(f"- 평균단가 (DCA): **{avg_price:,.0f}** 원")
            st.write(f"- 보유 수량: `{coin_qty:.8f}`")
            st.write(f"- 고점: {highest_price:,.0f} 원")

        with col_d2:
            st.markdown("**TP / SL**")
            if tp_price:
                tp_dist = (tp_price - cur_price) / cur_price * 100
                st.write(f"- 익절가 (TP): **{tp_price:,.0f}** 원  `{tp_dist:+.2f}%`")
            if sl_price:
                sl_dist = (sl_price - cur_price) / cur_price * 100
                st.write(f"- 손절가 (SL): **{sl_price:,.0f}** 원  `{sl_dist:+.2f}%`")

        with col_d3:
            st.markdown("**기능 활성 여부**")
            st.write(f"- DCA 2차 매수: {'✅ 완료' if dca_done else '⏳ 대기 중'}")
            st.write(f"- 본전 보호 스탑: {'✅ 활성' if be_activated else '❌ 미활성'}")

        # ── 진입가 / TP / SL / 현재가 시각화 ──
        if all([avg_price, cur_price, tp_price, sl_price]):
            st.markdown("**가격 레벨 시각화**")
            level_data = pd.DataFrame({
                "가격": [sl_price, avg_price, cur_price, tp_price],
                "레이블": ["손절(SL)", "평균진입가", "현재가", "익절(TP)"],
            }).set_index("레이블")
            st.bar_chart(level_data, use_container_width=True, height=200)

    elif raw_state == "buy_waiting":
        market = live.get("market", "")
        buy_price = live.get("pending_buy_price", 0)
        buy_amount = live.get("pending_buy_amount", 0)
        cur_price = pyupbit.get_current_price(market) if market else 0

        kpi_cols[1].metric("대기 매수가", f"{buy_price:,.0f} 원")
        kpi_cols[2].metric("주문 금액", f"{buy_amount:,.0f} 원")
        kpi_cols[3].metric("현재가", f"{cur_price:,.0f} 원" if cur_price else "—")

        st.markdown("---")
        st.info(
            f"**{market}** 지정가 매수 주문 대기 중\n\n"
            f"주문가: **{buy_price:,.0f}원** | 현재가: {cur_price:,.0f}원 | "
            f"금액: {buy_amount:,.0f}원\n\n"
            "현재가가 주문가 이하로 내려오면 체결됩니다. (최대 30분 대기)"
        )

    else:
        kpi_cols[1].metric("포지션", "없음")
        kpi_cols[2].metric("미실현 손익", "—")
        kpi_cols[3].metric("현재가", "—")

    # ── 최근 거래 내역 (실시간 현황 하단) ──
    st.markdown("---")
    st.subheader("📋 최근 거래 (최근 10건)")
    trade_df_live = load_trade_log()
    if not trade_df_live.empty:
        recent = trade_df_live.sort_values("timestamp", ascending=False).head(10).copy()
        recent["timestamp"] = recent["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        show_cols = [c for c in ["timestamp", "action", "market", "price", "pnl_krw", "pnl_pct", "reason"]
                     if c in recent.columns]
        st.dataframe(recent[show_cols], use_container_width=True, hide_index=True)
    else:
        st.info("아직 거래 기록이 없습니다.")


# ══════════════════════════════════════════════════════════════
# 페이지 2: 실시간 차트 & 기술적 지표 & 시장 환경
# ══════════════════════════════════════════════════════════════

elif page == "실시간 차트 & 지표":
    st.title(f"📈 {chart_market} 실시간 분석")

    chart_df = load_chart_data(chart_market, chart_unit)
    if chart_df.empty:
        st.error("차트 데이터를 불러올 수 없습니다.")
        st.stop()

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

    st.subheader("가격 차트 (볼린저 밴드 + EMA)")
    price_chart_df = chart_df[["close", "bb_upper", "bb_middle", "bb_lower", "ema_short", "ema_long"]].copy()
    price_chart_df.columns = ["종가", "BB 상단", "BB 중간", "BB 하단", f"EMA {cfg.EMA_SHORT}", f"EMA {cfg.EMA_LONG}"]
    st.line_chart(price_chart_df, use_container_width=True, height=400)

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

    st.subheader("📡 시장 환경 지표 (기술적 지표 외)")

    env_data = load_market_environment(chart_market)
    d = env_data["details"]

    env_col1, env_col2 = st.columns(2)

    with env_col1:
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

    st.subheader("📋 전체 거래 내역")
    sort_order = st.radio("정렬", ["최신순", "오래된순"], horizontal=True, key="sort_trade")
    display_df = trade_df.sort_values("timestamp", ascending=(sort_order == "오래된순")).copy()
    display_df["timestamp"] = display_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")

    default_cols = [c for c in ["timestamp", "action", "market", "price", "pnl_krw", "pnl_pct", "reason"]
                    if c in display_df.columns]
    st.dataframe(display_df[default_cols], use_container_width=True, height=500)

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


# ══════════════════════════════════════════════════════════════
# 페이지 4: 내 업비트 지갑 현황
# ══════════════════════════════════════════════════════════════

elif page == "💰 내 업비트 지갑":
    st_autorefresh(interval=30_000, key="wallet_refresh")

    st.title("💰 내 업비트 지갑 현황")
    st.caption("업비트 API 키로 실시간 잔고를 조회합니다. 30초마다 자동 갱신.")

    access_key = getattr(cfg, "ACCESS_KEY", "YOUR_ACCESS_KEY")
    secret_key = getattr(cfg, "SECRET_KEY", "YOUR_SECRET_KEY")

    if access_key in ("YOUR_ACCESS_KEY", "", None):
        st.error(
            "API 키가 설정되어 있지 않습니다.\n\n"
            "`upbit_bot/.env` 파일에 `UPBIT_ACCESS_KEY` 와 `UPBIT_SECRET_KEY` 를 입력하세요."
        )
        st.code(
            "UPBIT_ACCESS_KEY=your_access_key_here\n"
            "UPBIT_SECRET_KEY=your_secret_key_here",
            language="bash",
        )
        st.stop()

    try:
        upbit_obj = pyupbit.Upbit(access_key, secret_key)
        balances_raw = upbit_obj.get_balances()
    except Exception as e:
        st.error(f"업비트 API 연결 실패: {e}\n\nAPI 키 및 IP 허용 설정을 확인하세요.")
        st.stop()

    if not balances_raw:
        st.warning("잔고 정보를 가져오지 못했습니다. API 권한을 확인하세요.")
        st.stop()

    # ── 잔고 파싱 ──
    rows = []
    total_krw_equiv = 0.0

    for b in balances_raw:
        currency = b.get("currency", "")
        balance = float(b.get("balance") or 0)
        locked = float(b.get("locked") or 0)
        avg_buy_price = float(b.get("avg_buy_price") or 0)
        total_qty = balance + locked

        if total_qty <= 0:
            continue

        if currency == "KRW":
            current_price = 1.0
            eval_krw = total_qty
            pnl_pct = 0.0
            pnl_krw = 0.0
        else:
            market = f"KRW-{currency}"
            current_price = pyupbit.get_current_price(market) or 0.0
            eval_krw = total_qty * current_price
            if avg_buy_price > 0 and current_price > 0:
                pnl_pct = (current_price - avg_buy_price) / avg_buy_price * 100
                pnl_krw = (current_price - avg_buy_price) * total_qty
            else:
                pnl_pct = 0.0
                pnl_krw = 0.0

        total_krw_equiv += eval_krw
        rows.append({
            "코인": currency,
            "보유 수량": total_qty,
            "잠금 수량": locked,
            "평균 매수가": avg_buy_price,
            "현재가": current_price,
            "평가금액 (KRW)": eval_krw,
            "평가손익 (KRW)": pnl_krw,
            "수익률 (%)": pnl_pct,
        })

    wallet_df = pd.DataFrame(rows)

    # ── 총 자산 KPI ──
    krw_row = wallet_df[wallet_df["코인"] == "KRW"]
    krw_balance = krw_row["평가금액 (KRW)"].values[0] if not krw_row.empty else 0.0
    coin_eval = total_krw_equiv - krw_balance

    w1, w2, w3 = st.columns(3)
    w1.metric("총 평가금액", f"{total_krw_equiv:,.0f} 원")
    w2.metric("KRW 잔고", f"{krw_balance:,.0f} 원")
    w3.metric("코인 평가액", f"{coin_eval:,.0f} 원")

    st.markdown("---")

    # ── 코인별 보유 현황 테이블 ──
    st.subheader("보유 자산 현황")
    coin_df = wallet_df[wallet_df["코인"] != "KRW"].copy() if len(wallet_df) > 1 else pd.DataFrame()

    if not coin_df.empty:
        display_wallet = coin_df[[
            "코인", "보유 수량", "평균 매수가", "현재가",
            "평가금액 (KRW)", "평가손익 (KRW)", "수익률 (%)"
        ]].copy()
        display_wallet["평균 매수가"]     = display_wallet["평균 매수가"].map(lambda x: f"{x:,.0f}")
        display_wallet["현재가"]          = display_wallet["현재가"].map(lambda x: f"{x:,.0f}")
        display_wallet["평가금액 (KRW)"]  = display_wallet["평가금액 (KRW)"].map(lambda x: f"{x:,.0f}")
        display_wallet["평가손익 (KRW)"]  = display_wallet["평가손익 (KRW)"].map(lambda x: f"{x:+,.0f}")
        display_wallet["수익률 (%)"]      = display_wallet["수익률 (%)"].map(lambda x: f"{x:+.2f}%")
        display_wallet["보유 수량"]       = display_wallet["보유 수량"].map(lambda x: f"{x:.8f}".rstrip("0").rstrip("."))

        st.dataframe(display_wallet, use_container_width=True, hide_index=True)

        # ── 코인 비중 차트 ──
        st.subheader("자산 구성 비중")
        pie_df = pd.DataFrame({
            "자산": ["KRW"] + coin_df["코인"].tolist(),
            "평가액": [krw_balance] + coin_df["평가금액 (KRW)"].tolist(),
        }).set_index("자산")
        st.bar_chart(pie_df, use_container_width=True, height=300)
    else:
        st.info("보유 중인 코인이 없습니다. KRW만 보유 중입니다.")

    # ── KRW 잔고 별도 표시 ──
    st.markdown("---")
    st.caption(f"원화(KRW) 잔고: **{krw_balance:,.0f}원** | 조회 시각: {datetime.now().strftime('%H:%M:%S')}")


# ══════════════════════════════════════════════════════════════
# 페이지 5: 비트코인 뉴스
# ══════════════════════════════════════════════════════════════

elif page == "📰 비트코인 뉴스":
    st.title("📰 최신 암호화폐 뉴스")
    st.caption("주요 미디어 RSS 피드에서 실시간 수집 · 5분 자동 캐시 갱신")

    col_top1, col_top2 = st.columns([3, 1])
    with col_top2:
        if st.button("🔄 뉴스 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with st.spinner("뉴스를 불러오는 중..."):
        news_items = fetch_crypto_news()

    if not news_items:
        st.error(
            "뉴스를 불러오지 못했습니다. 네트워크 연결 또는 피드 URL을 확인하세요.\n\n"
            "일부 피드는 한국 IP에서 접근이 제한될 수 있습니다."
        )
        st.stop()

    # 소스 필터
    sources = sorted({n["source"] for n in news_items})
    selected_sources = st.multiselect(
        "소스 필터 (미선택 시 전체)",
        sources,
        default=[],
        placeholder="소스를 선택하세요...",
    )
    if selected_sources:
        news_items = [n for n in news_items if n["source"] in selected_sources]

    st.markdown(f"**총 {len(news_items)}개** 뉴스 수집됨")
    st.markdown("---")

    # 뉴스 카드 출력 (2열)
    for i in range(0, len(news_items), 2):
        cols = st.columns(2)
        for j, col in enumerate(cols):
            idx = i + j
            if idx >= len(news_items):
                break
            item = news_items[idx]
            with col:
                with st.container(border=True):
                    st.markdown(
                        f"**[{item['title']}]({item['link']})**"
                    )
                    meta_col1, meta_col2 = st.columns([1, 1])
                    meta_col1.caption(f"📌 {item['source']}")
                    meta_col2.caption(f"🕐 {item['date']}")
                    if item["summary"]:
                        st.markdown(
                            f"<div style='font-size:0.88rem; color:#888; line-height:1.5;'>"
                            f"{item['summary']}{'...' if len(item['summary']) >= 280 else ''}"
                            f"</div>",
                            unsafe_allow_html=True,
                        )

    st.markdown("---")
    st.caption(
        "출처: CoinDesk · CoinTelegraph · Bitcoin Magazine · Decrypt · BlockMedia · 코인리더스 | "
        f"마지막 수집: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ── 공통 푸터 ──
st.markdown("---")
st.caption(f"마지막 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
