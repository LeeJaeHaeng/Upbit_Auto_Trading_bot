"""
업비트 AI 자동매매 봇 - 메인 진입점

사용법:
  python main.py                          → 실시간 자동매매 (페이퍼 트레이딩)
  python main.py --backtest               → 기존 백테스팅 모드
  python main.py --enhanced-backtest      → 강화 백테스팅 (슬리피지·리스크 지표 포함)
  python main.py --validate               → 신호별 정확도 검증 (Precision/Recall/F1)
  python main.py --walk-forward           → Walk-Forward 검증 (IS/OOS 과적합 측정)
  python main.py --optimize               → 파라미터 최적화 (그리드 서치)
  python main.py --optimize --apply       → 최적화 후 config.py 자동 업데이트
  python main.py --scan                   → 마켓 스캔만 수행
  python main.py --live                   → 실거래 모드 (API 키 필요, 주의!)
  python main.py --dashboard              → 관리자 대시보드 실행
"""

import sys
import logging
import argparse
import subprocess
from pathlib import Path

from env_utils import load_env_file

load_env_file(Path(__file__).resolve().parent)

import config as cfg
from backtester import Backtester
from enhanced_backtester import EnhancedBacktester
from signal_validator import SignalValidator
from walk_forward_validator import WalkForwardValidator
from param_optimizer import ParamOptimizer
from trader import Trader
from market_scanner import MarketScanner

# ── 로깅 설정 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(cfg.BOT_LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_backtest(days: int = 30):
    """기존 백테스팅 실행"""
    print("\n" + "="*60)
    print("  🧪 백테스팅 모드")
    print(f"  기간: 최근 {days}일")
    print("="*60)
    bt = Backtester(cfg)
    results = bt.run(days=days, initial_capital=1_000_000)
    return results


def run_enhanced_backtest(days: int = 90):
    """강화 백테스팅 실행 (슬리피지·리스크 지표·국면 분석 포함)"""
    bt = EnhancedBacktester(cfg)
    return bt.run(days=days)


def run_validate(days: int = 180):
    """신호별 예측 정확도 검증 (Precision / Recall / F1 / Edge)"""
    sv = SignalValidator(cfg)
    return sv.validate(days=days)


def run_walk_forward(total_days: int = 180, n_windows: int = 4):
    """Walk-Forward 검증 (IS vs OOS 성능 비교 — 과적합 측정)"""
    wf = WalkForwardValidator(cfg)
    return wf.run(total_days=total_days, n_windows=n_windows)


def run_optimize(days: int = 180, apply: bool = False):
    """
    파라미터 최적화 (그리드 서치).
    apply=True이면 최적 파라미터를 config.py에 자동 반영.
    """
    optimizer = ParamOptimizer(cfg)
    best = optimizer.run(days=days)
    if best and apply:
        from pathlib import Path as _Path
        config_path = _Path(__file__).resolve().parent / "config.py"
        confirm = input("\n  config.py에 최적 파라미터를 적용하시겠습니까? (yes 입력): ").strip()
        if confirm == "yes":
            optimizer.apply_to_config(best, config_path)
        else:
            print("  적용 취소됨.")
    return best


def run_scan():
    """마켓 스캔만 수행"""
    print("\n" + "="*60)
    print("  🔍 마켓 스캔 모드")
    print("="*60)
    scanner = MarketScanner(cfg)
    top_markets = scanner.scan_and_rank(top_n=10)
    print("\n📋 상위 10개 마켓 요약:")
    for i, m in enumerate(top_markets, 1):
        print(
            f"  {i:2}. {m['market']:<12} | "
            f"기회점수={m['opportunity_score']:.2f} | "
            f"신호={m['signal_score']}/5 | "
            f"RSI={m['rsi']:.1f} | "
            f"ATR={m['atr_pct']:.2f}%"
        )


def run_dashboard():
    """Streamlit 관리자 대시보드 실행"""
    print("\n" + "="*60)
    print("  📊 관리자 대시보드 실행")
    print("  브라우저에서 자동으로 열립니다.")
    print("  종료: Ctrl+C")
    print("="*60)
    dashboard_path = Path(__file__).resolve().parent / "dashboard.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(dashboard_path)])


def run_live_trader(paper: bool = True):
    """실시간 트레이더 실행"""
    cfg.PAPER_TRADING = paper
    if not paper:
        print("\n⚠️  경고: 실거래 모드입니다. 실제 자금이 사용됩니다!")
        if cfg.ACCESS_KEY == "YOUR_ACCESS_KEY":
            print("❌ API 키가 설정되지 않았습니다. config.py에서 설정하세요.")
            sys.exit(1)
        confirm = input("계속하시겠습니까? (yes 입력): ").strip()
        if confirm != "yes":
            print("취소됨.")
            sys.exit(0)

    trader = Trader(cfg)
    trader.run()


def main():
    parser = argparse.ArgumentParser(
        description="업비트 AI 자동매매 봇",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python main.py                            # 페이퍼 트레이딩 (기본)
  python main.py --backtest                 # 30일 백테스팅 (기존)
  python main.py --backtest --days 60       # 60일 백테스팅
  python main.py --enhanced-backtest        # 강화 백테스팅 (슬리피지·리스크 지표)
  python main.py --enhanced-backtest --days 90
  python main.py --validate                 # 신호별 정확도 검증
  python main.py --validate --days 180      # 검증 기간 180일
  python main.py --walk-forward             # Walk-Forward 검증 (과적합 측정)
  python main.py --walk-forward --days 180 --windows 4
  python main.py --optimize                 # 파라미터 최적화 (그리드 서치)
  python main.py --optimize --apply         # 최적화 후 config.py 자동 업데이트
  python main.py --optimize --days 180      # 180일 데이터로 최적화
  python main.py --scan                     # 마켓 스캔만
  python main.py --dashboard                # 관리자 대시보드
  python main.py --live                     # 실거래 (주의!)
        """
    )
    parser.add_argument("--backtest",          action="store_true", help="기존 백테스팅 모드 실행")
    parser.add_argument("--enhanced-backtest", action="store_true", help="강화 백테스팅 (슬리피지·리스크 지표·국면 분석)")
    parser.add_argument("--validate",          action="store_true", help="신호별 예측 정확도 검증 (Precision/Recall/F1)")
    parser.add_argument("--walk-forward",      action="store_true", help="Walk-Forward 검증 (IS vs OOS 과적합 측정)")
    parser.add_argument("--optimize",          action="store_true", help="파라미터 그리드 서치 최적화")
    parser.add_argument("--apply",             action="store_true", help="--optimize 결과를 config.py에 자동 반영")
    parser.add_argument("--days",    type=int, default=None, help="기간 (일) — 미지정 시 각 모드 기본값 사용")
    parser.add_argument("--windows", type=int, default=4,   help="Walk-Forward 윈도우 수 (기본: 4)")
    parser.add_argument("--scan",      action="store_true", help="마켓 스캔만 실행")
    parser.add_argument("--live",      action="store_true", help="실거래 모드 (주의: 실제 자금 사용)")
    parser.add_argument("--dashboard", action="store_true", help="관리자 대시보드 실행")
    args = parser.parse_args()

    if args.dashboard:
        run_dashboard()
    elif args.backtest:
        run_backtest(days=args.days or 30)
    elif args.enhanced_backtest:
        run_enhanced_backtest(days=args.days or 90)
    elif args.validate:
        run_validate(days=args.days or 180)
    elif args.walk_forward:
        run_walk_forward(total_days=args.days or 180, n_windows=args.windows)
    elif args.optimize:
        run_optimize(days=args.days or 180, apply=args.apply)
    elif args.scan:
        run_scan()
    elif args.live:
        run_live_trader(paper=False)
    else:
        run_live_trader(paper=True)


if __name__ == "__main__":
    main()
