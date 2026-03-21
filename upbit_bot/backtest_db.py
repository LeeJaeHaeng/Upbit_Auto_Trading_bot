"""
backtest_db.py — 백테스트 결과 SQLite 영속 저장소

테이블:
  backtest_runs    — 강화 백테스터 실행 메타 + 요약 지표
  backtest_trades  — 백테스트 개별 거래 내역
  signal_runs      — 신호 검증 실행 기록 (JSON 직렬화)
  walkforward_runs — Walk-Forward 실행 기록 (JSON 직렬화)
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
