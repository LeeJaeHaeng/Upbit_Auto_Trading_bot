import json
from django.shortcuts import render
from django.http import JsonResponse
from . import services


def _to_json_safe(value):
    """numpy 스칼라 등 비표준 타입을 JsonResponse 직렬화 가능한 타입으로 변환."""
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def overview(request):
    ctx = {
        "overview": services.get_overview_data(),
        "daily": services.get_daily_data(),
        "weekly": services.get_weekly_data(),
        "monthly": services.get_monthly_data(),
        "market_perf": services.get_market_performance(),
        "cumulative": json.dumps(services.get_cumulative_pnl()),
    }
    return render(request, "dashboard/overview.html", ctx)


def trades(request):
    trades_list = services.load_trades()
    trades_list.reverse()  # 최신순
    market_filter = request.GET.get("market", "")

    if market_filter:
        trades_list = [t for t in trades_list if t.get("market") == market_filter]

    all_trades = services.load_trades()
    markets = sorted(set(t.get("market", "") for t in all_trades if t.get("market")))

    ctx = {
        "trades": trades_list,
        "markets": markets,
        "current_market": market_filter,
    }
    return render(request, "dashboard/trades.html", ctx)


def live_chart(request):
    market = request.GET.get("market", "KRW-BTC")
    unit = int(request.GET.get("unit", "60"))
    markets = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE",
               "KRW-PEPE", "KRW-SUI", "KRW-ADA", "KRW-ONDO"]
    ctx = {
        "market": market,
        "unit": unit,
        "markets": markets,
        "units": [5, 15, 30, 60, 240],
    }
    return render(request, "dashboard/chart.html", ctx)


def api_overview(request):
    return JsonResponse(_to_json_safe(services.get_overview_data()))


def api_chart(request, market, unit):
    data = services.get_chart_data(market, unit)
    return JsonResponse(_to_json_safe(data), safe=False)


def api_market_env(request, market):
    data = services.get_market_env(market)
    return JsonResponse(_to_json_safe(data), safe=False)


def api_cumulative(request):
    return JsonResponse(_to_json_safe(services.get_cumulative_pnl()), safe=False)
