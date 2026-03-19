"""
대시보드 데이터 서비스
CSV/JSON 파일에서 거래 데이터를 로드하고 분석합니다.
"""

import os
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import pyupbit
import pandas as pd

BOT_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = BOT_DIR / "trading_log.csv"
PERFORMANCE_FILE = BOT_DIR / "performance.json"

# 프로젝트 모듈
import sys
sys.path.insert(0, str(BOT_DIR))
from indicators import add_all_indicators, get_signal_score
from market_indicators import MarketEnvironment
import config as cfg


def load_performance() -> dict:
    if not PERFORMANCE_FILE.exists():
        return {"paper_capital": 1_000_000, "paper_current": 1_000_000,
                "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
                "total_pnl_krw": 0, "total_fees_krw": 0,
                "max_profit_pct": 0, "max_loss_pct": 0, "daily_pnl": {}}
    with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_trades() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    trades = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            for key in ["price", "amount_krw", "coin_qty", "fee",
                        "entry_price", "exit_price", "pnl_krw", "pnl_pct"]:
                if row.get(key):
                    try:
                        row[key] = float(row[key])
                    except (ValueError, TypeError):
                        row[key] = 0.0
                else:
                    row[key] = 0.0
            if row.get("signal_score"):
                try:
                    row["signal_score"] = int(row["signal_score"])
                except (ValueError, TypeError):
                    row["signal_score"] = 0
            trades.append(row)
    return trades


def get_sell_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("action") == "SELL"]


def get_overview_data() -> dict:
    perf = load_performance()
    trades = load_trades()
    sells = get_sell_trades(trades)

    total = len(sells)
    wins = [t for t in sells if t["pnl_krw"] >= 0]
    losses = [t for t in sells if t["pnl_krw"] < 0]
    win_rate = len(wins) / total * 100 if total > 0 else 0
    total_pnl = sum(t["pnl_krw"] for t in sells)
    total_fees = sum(t["fee"] for t in trades)

    initial = perf.get("paper_capital", 1_000_000)
    current = perf.get("paper_current", initial)
    total_return = (current - initial) / initial * 100

    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    max_win = max((t["pnl_pct"] for t in wins), default=0)
    max_loss = min((t["pnl_pct"] for t in losses), default=0)

    # 프로핏 팩터
    total_win_krw = sum(t["pnl_krw"] for t in wins)
    total_loss_krw = abs(sum(t["pnl_krw"] for t in losses))
    profit_factor = total_win_krw / total_loss_krw if total_loss_krw > 0 else 0

    # MDD
    cumulative = 0
    peak = 0
    mdd = 0
    for t in sells:
        cumulative += t["pnl_krw"]
        if cumulative > peak:
            peak = cumulative
        dd = cumulative - peak
        if dd < mdd:
            mdd = dd

    return {
        "total_trades": total,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl),
        "total_fees": round(total_fees),
        "initial_capital": initial,
        "current_capital": round(current),
        "total_return": round(total_return, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_win": round(max_win, 2),
        "max_loss": round(max_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "mdd": round(mdd),
    }


def get_daily_data() -> list[dict]:
    sells = get_sell_trades(load_trades())
    by_date = defaultdict(list)
    for t in sells:
        date = t["timestamp"][:10]
        by_date[date].append(t)

    result = []
    for date in sorted(by_date):
        items = by_date[date]
        pnl_list = [t["pnl_krw"] for t in items]
        wins = sum(1 for p in pnl_list if p >= 0)
        result.append({
            "date": date,
            "trades": len(items),
            "pnl": round(sum(pnl_list)),
            "avg_pnl": round(sum(pnl_list) / len(pnl_list)),
            "win_rate": round(wins / len(items) * 100, 1),
        })
    return result


def get_weekly_data() -> list[dict]:
    sells = get_sell_trades(load_trades())
    by_week = defaultdict(list)
    for t in sells:
        dt = datetime.strptime(t["timestamp"][:10], "%Y-%m-%d")
        week_key = dt.strftime("%Y-W%U")
        by_week[week_key].append(t)

    result = []
    for week in sorted(by_week):
        items = by_week[week]
        pnl_list = [t["pnl_krw"] for t in items]
        wins = sum(1 for p in pnl_list if p >= 0)
        result.append({
            "week": week,
            "trades": len(items),
            "pnl": round(sum(pnl_list)),
            "win_rate": round(wins / len(items) * 100, 1),
        })
    return result


def get_monthly_data() -> list[dict]:
    sells = get_sell_trades(load_trades())
    by_month = defaultdict(list)
    for t in sells:
        month_key = t["timestamp"][:7]
        by_month[month_key].append(t)

    result = []
    for month in sorted(by_month):
        items = by_month[month]
        pnl_list = [t["pnl_krw"] for t in items]
        wins = sum(1 for p in pnl_list if p >= 0)
        result.append({
            "month": month,
            "trades": len(items),
            "pnl": round(sum(pnl_list)),
            "win_rate": round(wins / len(items) * 100, 1),
        })
    return result


def get_market_performance() -> list[dict]:
    sells = get_sell_trades(load_trades())
    by_market = defaultdict(list)
    for t in sells:
        by_market[t.get("market", "")].append(t)

    result = []
    for market in sorted(by_market):
        items = by_market[market]
        pnl_list = [t["pnl_krw"] for t in items]
        wins = sum(1 for p in pnl_list if p >= 0)
        result.append({
            "market": market,
            "trades": len(items),
            "pnl": round(sum(pnl_list)),
            "avg_pnl_pct": round(sum(t["pnl_pct"] for t in items) / len(items), 2),
            "win_rate": round(wins / len(items) * 100, 1),
        })
    result.sort(key=lambda x: x["pnl"], reverse=True)
    return result


def get_cumulative_pnl() -> list[dict]:
    sells = get_sell_trades(load_trades())
    cumulative = 0
    data = []
    for t in sells:
        cumulative += t["pnl_krw"]
        data.append({
            "timestamp": t["timestamp"],
            "cumulative": round(cumulative),
            "pnl": round(t["pnl_krw"]),
        })
    return data


def get_chart_data(market: str = "KRW-BTC", unit: int = 60) -> dict:
    """실시간 차트 데이터 + 지표"""
    df = pyupbit.get_ohlcv(market, interval=f"minute{unit}", count=200)
    if df is None or df.empty:
        return {}
    df.columns = ["open", "high", "low", "close", "volume", "value"]
    df = add_all_indicators(df, cfg)
    df = df.dropna()

    labels = [dt.strftime("%m/%d %H:%M") for dt in df.index]
    last = df.iloc[-1]

    candles = []
    volume_bars = []
    ema_short_line = []
    ema_long_line = []
    bb_upper_line = []
    bb_lower_line = []

    for dt, row in df.iterrows():
        ts = int(pd.Timestamp(dt).timestamp())
        open_price = float(row["open"])
        close_price = float(row["close"])

        candles.append({
            "time": ts,
            "open": open_price,
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": close_price,
        })
        # 업비트 스타일: 상승(종가>=시가) 빨강, 하락 파랑
        volume_bars.append({
            "time": ts,
            "value": float(row["volume"]),
            "color": "#ef4444" if close_price >= open_price else "#3b82f6",
        })
        ema_short_line.append({"time": ts, "value": float(row["ema_short"])})
        ema_long_line.append({"time": ts, "value": float(row["ema_long"])})
        bb_upper_line.append({"time": ts, "value": float(row["bb_upper"])})
        bb_lower_line.append({"time": ts, "value": float(row["bb_lower"])})

    signal_result = get_signal_score(last, cfg)
    json_safe_signals = {
        key: bool(value) for key, value in signal_result["signals"].items()
    }

    return {
        "labels": labels,
        "candles": candles,
        "volume_bars": volume_bars,
        "ema_short_line": ema_short_line,
        "ema_long_line": ema_long_line,
        "bb_upper_line": bb_upper_line,
        "bb_lower_line": bb_lower_line,
        "close": df["close"].tolist(),
        "bb_upper": df["bb_upper"].tolist(),
        "bb_middle": df["bb_middle"].tolist(),
        "bb_lower": df["bb_lower"].tolist(),
        "ema_short": df["ema_short"].tolist(),
        "ema_long": df["ema_long"].tolist(),
        "rsi": df["rsi"].tolist(),
        "macd": df["macd"].tolist(),
        "macd_signal": df["macd_signal"].tolist(),
        "macd_hist": df["macd_hist"].tolist(),
        "volume": df["volume"].tolist(),
        "volume_ma": df["volume_ma"].tolist(),
        "current": {
            "price": float(last["close"]),
            "rsi": round(float(last["rsi"]), 1),
            "macd": round(float(last["macd"]), 1),
            "macd_hist": round(float(last["macd_hist"]), 1),
            "bb_pct": round(float(last["bb_pct"]), 2),
            "atr_pct": round(float(last["atr_pct"]), 2),
            "volume_ratio": round(float(last["volume_ratio"]), 2),
        },
        "signals": json_safe_signals,
        "signal_score": int(signal_result["score"]),
        "signal_details": {k: str(v) for k, v in signal_result["details"].items()},
    }


def get_market_env(market: str = "KRW-BTC") -> dict:
    env = MarketEnvironment()
    return env.get_market_score(market)
