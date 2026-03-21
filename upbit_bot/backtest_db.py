"""
backtest_db.py — 백테스트 + 실거래/모의거래 결과 SQLite 영속 저장소

테이블:
  backtest_runs    — 강화 백테스터 실행 메타 + 요약 지표
  backtest_trades  — 백테스트 개별 거래 내역
  signal_runs      — 신호 검증 실행 기록 (JSON 직렬화)
  walkforward_runs — Walk-Forward 실행 기록 (JSON 직렬화)
  trading_sessions — 봇 실행 세션 (config 스냅샷 + 최종 성과)
  trading_records  — 실거래/모의거래 개별 거래 + 체결 시점 지표값
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "backtest_history.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """테이블 초기화 (존재하면 스킵)"""
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at           TEXT NOT NULL,
            market           TEXT NOT NULL,
            days             INTEGER NOT NULL,
            initial_capital  REAL,
            final_capital    REAL,
            total_return_pct REAL,
            benchmark_pct    REAL,
            alpha_pct        REAL,
            total_trades     INTEGER,
            win_rate_pct     REAL,
            avg_win_pct      REAL,
            avg_loss_pct     REAL,
            profit_factor    REAL,
            max_drawdown_pct REAL,
            sharpe_ratio     REAL,
            sortino_ratio    REAL,
            calmar_ratio     REAL,
            config_json      TEXT
        );

        CREATE TABLE IF NOT EXISTS backtest_trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            entry_time   TEXT,
            exit_time    TEXT,
            entry_price  REAL,
            exit_price   REAL,
            signal_score INTEGER,
            pnl_pct      REAL,
            pnl_krw      REAL,
            reason       TEXT,
            regime       TEXT
        );

        CREATE TABLE IF NOT EXISTS signal_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at      TEXT NOT NULL,
            market      TEXT NOT NULL,
            days        INTEGER NOT NULL,
            results_json TEXT NOT NULL,
            config_json TEXT
        );

        CREATE TABLE IF NOT EXISTS walkforward_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL,
            market       TEXT NOT NULL,
            days         INTEGER NOT NULL,
            n_windows    INTEGER NOT NULL,
            results_json TEXT NOT NULL,
            config_json  TEXT
        );

        -- ── 실거래 / 모의거래 ──────────────────────────────────────
        -- 봇 실행 세션: config 스냅샷 + 세션 종료 시 성과 요약
        CREATE TABLE IF NOT EXISTS trading_sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at       TEXT NOT NULL,
            ended_at         TEXT,
            mode             TEXT NOT NULL,      -- 'paper' | 'live'
            config_json      TEXT NOT NULL,      -- 전체 파라미터 스냅샷
            total_trades     INTEGER DEFAULT 0,
            win_trades       INTEGER DEFAULT 0,
            lose_trades      INTEGER DEFAULT 0,
            win_rate_pct     REAL,
            total_pnl_krw    REAL,
            total_return_pct REAL,
            initial_capital  REAL,
            final_capital    REAL
        );

        -- 개별 거래: 세션 연결 + 체결 시점 지표값 (파라미터 튜닝 분석용)
        CREATE TABLE IF NOT EXISTS trading_records (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER REFERENCES trading_sessions(id),
            timestamp      TEXT NOT NULL,
            action         TEXT NOT NULL,        -- 'BUY' | 'SELL'
            market         TEXT NOT NULL,
            price          REAL,
            amount_krw     REAL,
            coin_qty       REAL,
            fee            REAL,
            entry_price    REAL,
            exit_price     REAL,
            pnl_krw        REAL,
            pnl_pct        REAL,
            reason         TEXT,
            signal_score   INTEGER,
            signals_json   TEXT,                 -- {"rsi": true, "macd": false, ...}
            -- 체결 시점 지표값 (승/패 원인 분석)
            rsi            REAL,
            bb_pct         REAL,
            macd           REAL,
            macd_hist      REAL,
            volume_ratio   REAL,
            atr_pct        REAL,
            ema_short      REAL,
            ema_long       REAL,
            close_price    REAL
        );

        -- 잔고 스냅샷 (거래 체결 + 봇 시작/종료 시 저장)
        CREATE TABLE IF NOT EXISTS balance_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER REFERENCES trading_sessions(id),
            snapshot_at     TEXT NOT NULL,
            trigger         TEXT NOT NULL,   -- startup|buy|sell|shutdown|periodic
            mode            TEXT NOT NULL,   -- paper|live
            krw_balance     REAL,
            coin_market     TEXT,
            coin_qty        REAL,
            coin_value_krw  REAL,
            total_asset_krw REAL,
            unrealized_pct  REAL,
            note            TEXT
        );
        """)


def save_backtest(results: dict, config) -> int:
    """
    강화 백테스터 결과 저장.
    반환: backtest_runs.id
    """
    init_db()
    cfg_json = json.dumps({
        "RSI_OVERSOLD":        config.RSI_OVERSOLD,
        "MIN_SIGNAL_COUNT":    config.MIN_SIGNAL_COUNT,
        "VOLUME_THRESHOLD":    config.VOLUME_THRESHOLD,
        "STOP_LOSS_PCT":       config.STOP_LOSS_PCT,
        "TAKE_PROFIT_PCT":     config.TAKE_PROFIT_PCT,
        "TRAILING_STOP_PCT":   config.TRAILING_STOP_PCT,
        "MTF_CHECK":           config.MTF_CHECK,
        "USE_TREND_FILTER":    config.USE_TREND_FILTER,
        "CANDLE_UNIT":         config.CANDLE_UNIT,
    })

    with _connect() as conn:
        cur = conn.execute("""
            INSERT INTO backtest_runs (
                run_at, market, days,
                initial_capital, final_capital,
                total_return_pct, benchmark_pct, alpha_pct,
                total_trades, win_rate_pct,
                avg_win_pct, avg_loss_pct, profit_factor,
                max_drawdown_pct, sharpe_ratio, sortino_ratio, calmar_ratio,
                config_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            results["market"],
            results["days"],
            results.get("initial_capital", 0),
            results.get("final_capital", 0),
            results.get("total_return_pct", 0),
            results.get("benchmark_return_pct", 0),
            results.get("alpha_pct", 0),
            results.get("total_trades", 0),
            results.get("win_rate_pct", 0),
            results.get("avg_win_pct", 0),
            results.get("avg_loss_pct", 0),
            results.get("profit_factor", 0),
            results.get("max_drawdown_pct", 0),
            results.get("sharpe_ratio", 0),
            results.get("sortino_ratio", 0),
            results.get("calmar_ratio", 0),
            cfg_json,
        ))
        run_id = cur.lastrowid

        trades = results.get("trades", [])
        if trades:
            conn.executemany("""
                INSERT INTO backtest_trades
                    (run_id, entry_time, exit_time, entry_price, exit_price,
                     signal_score, pnl_pct, pnl_krw, reason, regime)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, [
                (
                    run_id,
                    t.get("entry_time", ""),
                    t.get("exit_time", ""),
                    t.get("entry_price", 0),
                    t.get("exit_price", 0),
                    t.get("signal_score", 0),
                    t.get("pnl_pct", 0),
                    t.get("pnl_krw", 0),
                    t.get("reason", ""),
                    t.get("regime", ""),
                )
                for t in trades
            ])

    print(f"[DB] 백테스트 결과 저장 완료 → backtest_history.db (run_id={run_id})")
    return run_id


def save_signal_validation(results: dict, market: str, days: int, config) -> int:
    """신호 검증 결과 저장"""
    init_db()
    cfg_json = json.dumps({
        "MIN_SIGNAL_COUNT": config.MIN_SIGNAL_COUNT,
        "RSI_OVERSOLD":     config.RSI_OVERSOLD,
        "VOLUME_THRESHOLD": config.VOLUME_THRESHOLD,
    })
    with _connect() as conn:
        cur = conn.execute("""
            INSERT INTO signal_runs (run_at, market, days, results_json, config_json)
            VALUES (?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            market, days,
            json.dumps(results, ensure_ascii=False),
            cfg_json,
        ))
        run_id = cur.lastrowid
    print(f"[DB] 신호 검증 결과 저장 완료 → backtest_history.db (run_id={run_id})")
    return run_id


def save_walkforward(results: dict, market: str, days: int, n_windows: int, config) -> int:
    """Walk-Forward 검증 결과 저장"""
    init_db()
    cfg_json = json.dumps({
        "MIN_SIGNAL_COUNT": config.MIN_SIGNAL_COUNT,
        "RSI_OVERSOLD":     config.RSI_OVERSOLD,
        "VOLUME_THRESHOLD": config.VOLUME_THRESHOLD,
    })
    with _connect() as conn:
        cur = conn.execute("""
            INSERT INTO walkforward_runs
                (run_at, market, days, n_windows, results_json, config_json)
            VALUES (?,?,?,?,?,?)
        """, (
            datetime.now().isoformat(),
            market, days, n_windows,
            json.dumps(results, ensure_ascii=False, default=str),
            cfg_json,
        ))
        run_id = cur.lastrowid
    print(f"[DB] Walk-Forward 결과 저장 완료 → backtest_history.db (run_id={run_id})")
    return run_id


# ── 조회 헬퍼 ────────────────────────────────────────────────

def list_backtest_runs(limit: int = 50) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, run_at, market, days,
                   total_return_pct, total_trades, win_rate_pct,
                   sharpe_ratio, max_drawdown_pct
            FROM backtest_runs
            ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_backtest_trades(run_id: int) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM backtest_trades WHERE run_id=? ORDER BY id
        """, (run_id,)).fetchall()
    return [dict(r) for r in rows]


def list_signal_runs(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, run_at, market, days FROM signal_runs ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_signal_run(run_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM signal_runs WHERE id=?", (run_id,)
        ).fetchone()
    if row:
        d = dict(row)
        d["results"] = json.loads(d["results_json"])
        return d
    return None


def list_walkforward_runs(limit: int = 20) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("""
            SELECT id, run_at, market, days, n_windows
            FROM walkforward_runs ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_walkforward_run(run_id: int) -> dict | None:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM walkforward_runs WHERE id=?", (run_id,)
        ).fetchone()
    if row:
        d = dict(row)
        d["results"] = json.loads(d["results_json"])
        return d
    return None


# ── 실거래 / 모의거래 세션 & 거래 기록 ─────────────────────────────

def start_trading_session(mode: str, config) -> int:
    init_db()
    cfg_json = json.dumps({
        "RSI_OVERSOLD":           config.RSI_OVERSOLD,
        "MIN_SIGNAL_COUNT":       config.MIN_SIGNAL_COUNT,
        "VOLUME_THRESHOLD":       config.VOLUME_THRESHOLD,
        "STOP_LOSS_PCT":          config.STOP_LOSS_PCT,
        "TAKE_PROFIT_PCT":        config.TAKE_PROFIT_PCT,
        "TRAILING_STOP_PCT":      config.TRAILING_STOP_PCT,
        "BREAKEVEN_TRIGGER_PCT":  config.BREAKEVEN_TRIGGER_PCT,
        "TECHNICAL_EXIT_MIN_PCT": config.TECHNICAL_EXIT_MIN_PCT,
        "MTF_CHECK":              config.MTF_CHECK,
        "USE_TREND_FILTER":       config.USE_TREND_FILTER,
        "TREND_FILTER_STRICT":    config.TREND_FILTER_STRICT,
        "SCALED_ENTRY":           config.SCALED_ENTRY,
        "CANDLE_UNIT":            config.CANDLE_UNIT,
        "TRADE_AMOUNT_KRW":       config.TRADE_AMOUNT_KRW,
    }, ensure_ascii=False)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO trading_sessions (started_at, mode, config_json) VALUES (?,?,?)",
            (datetime.now().isoformat(), mode, cfg_json),
        )
    return cur.lastrowid


def end_trading_session(session_id: int, perf: dict, final_capital: float):
    init_db()
    total = perf.get("total_trades", 0)
    wins  = perf.get("winning_trades", 0)
    loses = perf.get("losing_trades", 0)
    pnl   = perf.get("total_pnl_krw", 0.0)
    init  = perf.get("paper_capital", final_capital)
    ret   = (final_capital - init) / init * 100 if init else 0.0
    wr    = wins / total * 100 if total else 0.0
    with _connect() as conn:
        conn.execute(
            """UPDATE trading_sessions
            SET ended_at=?, total_trades=?, win_trades=?, lose_trades=?,
                win_rate_pct=?, total_pnl_krw=?, total_return_pct=?,
                initial_capital=?, final_capital=?
            WHERE id=?""",
            (datetime.now().isoformat(), total, wins, loses,
             wr, pnl, ret, init, final_capital, session_id),
        )


def record_buy(session_id: int, market: str, price: float,
               amount_krw: float, coin_qty: float, fee: float,
               signal_score: int, signals: dict, indicators: dict = None):
    init_db()
    ind = indicators or {}
    with _connect() as conn:
        conn.execute(
            """INSERT INTO trading_records
                (session_id, timestamp, action, market, price,
                 amount_krw, coin_qty, fee, entry_price,
                 signal_score, signals_json,
                 rsi, bb_pct, macd, macd_hist, volume_ratio, atr_pct,
                 ema_short, ema_long, close_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, datetime.now().isoformat(), "BUY", market, price,
                amount_krw, coin_qty, fee, price,
                signal_score, json.dumps(signals, ensure_ascii=False),
                ind.get("rsi"), ind.get("bb_pct"), ind.get("macd"), ind.get("macd_hist"),
                ind.get("volume_ratio"), ind.get("atr_pct"),
                ind.get("ema_short"), ind.get("ema_long"), ind.get("close"),
            ),
        )


def record_sell(session_id: int, market: str, entry_price: float,
                exit_price: float, coin_qty: float, fee: float,
                pnl_krw: float, pnl_pct: float, reason: str):
    init_db()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO trading_records
                (session_id, timestamp, action, market, price,
                 coin_qty, fee, entry_price, exit_price,
                 pnl_krw, pnl_pct, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, datetime.now().isoformat(), "SELL", market, exit_price,
                coin_qty, fee, entry_price, exit_price, pnl_krw, pnl_pct, reason,
            ),
        )


def import_csv_to_db(csv_path: str, session_id: int = None) -> int:
    import csv as _csv
    init_db()
    if session_id is None:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO trading_sessions (started_at, mode, config_json) VALUES (?,?,?)",
                (datetime.now().isoformat(), "imported", json.dumps({"note": "CSV import"})),
            )
            session_id = cur.lastrowid

    count = 0
    with open(csv_path, encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        rows_to_insert = []
        for row in reader:
            def _f(k, default=None, _row=row):
                v = _row.get(k, "")
                try:
                    return float(v) if v not in ("", None) else default
                except Exception:
                    return default
            rows_to_insert.append((
                session_id, row.get("timestamp", ""), row.get("action", ""),
                row.get("market", ""), _f("price"), _f("amount_krw"),
                _f("coin_qty"), _f("fee"), _f("entry_price"), _f("exit_price"),
                _f("pnl_krw"), _f("pnl_pct"), row.get("reason", ""), _f("signal_score"),
            ))
        with _connect() as conn:
            conn.executemany(
                """INSERT INTO trading_records
                    (session_id, timestamp, action, market, price,
                     amount_krw, coin_qty, fee, entry_price, exit_price,
                     pnl_krw, pnl_pct, reason, signal_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows_to_insert,
            )
            count = len(rows_to_insert)
    print(f"[DB] CSV 임포트 완료: {count}건 -> session_id={session_id}")
    return count


# ── 분석용 조회 ──────────────────────────────────────────────

def list_trading_sessions(limit: int = 30) -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, started_at, ended_at, mode,
                   total_trades, win_rate_pct, total_pnl_krw, total_return_pct,
                   initial_capital, final_capital
            FROM trading_sessions ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_trading_records(session_id: int = None, limit: int = 500) -> list[dict]:
    init_db()
    with _connect() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM trading_records WHERE session_id=? ORDER BY timestamp DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trading_records ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_param_performance_summary() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, started_at, mode, config_json,
                   total_trades, win_rate_pct, total_pnl_krw, total_return_pct,
                   initial_capital, final_capital
            FROM trading_sessions WHERE total_trades > 0 ORDER BY id DESC""",
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = json.loads(d["config_json"])
        except Exception:
            d["config"] = {}
        result.append(d)
    return result


def get_indicator_win_analysis() -> list[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT r.*, s.config_json
            FROM trading_records r
            LEFT JOIN trading_sessions s ON r.session_id = s.id
            WHERE r.action = 'SELL'
            ORDER BY r.timestamp DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def record_balance(
    session_id: int,
    trigger: str,
    mode: str,
    krw_balance: float,
    coin_market: str = None,
    coin_qty: float = None,
    coin_value_krw: float = None,
    unrealized_pct: float = None,
    note: str = None,
):
    """잔고 스냅샷 저장.

    trigger: 'startup' | 'buy' | 'sell' | 'shutdown' | 'periodic'
    """
    init_db()
    total = (krw_balance or 0) + (coin_value_krw or 0)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO balance_snapshots
                (session_id, snapshot_at, trigger, mode,
                 krw_balance, coin_market, coin_qty, coin_value_krw,
                 total_asset_krw, unrealized_pct, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, datetime.now().isoformat(), trigger, mode,
                krw_balance, coin_market, coin_qty, coin_value_krw,
                total, unrealized_pct, note,
            ),
        )


def list_balance_snapshots(session_id: int = None, limit: int = 100) -> list[dict]:
    init_db()
    with _connect() as conn:
        if session_id:
            rows = conn.execute(
                """SELECT * FROM balance_snapshots WHERE session_id=?
                ORDER BY snapshot_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM balance_snapshots ORDER BY snapshot_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]
