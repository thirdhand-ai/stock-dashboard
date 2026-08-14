"""Phase 12 Component F (optional): Discord summary payload generator.

Total isolation from the real webhook path: this module never imports
`requests`, `alerts.discord`, or `config.settings`, and never references
DISCORD_WEBHOOK_URL - see tests/test_ops_safety.py's AST-scan test.
`build_daily_summary_payload` is a pure function, no network call anywhere
in this module.
"""
from ops.daily_report import DailyReport

# Hardcoded off, same convention as
# alerts.ops_notifications.OPERATIONAL_ALERTS_ENABLED. Not read by
# ops/generate_daily_summary.py - this flag exists purely as a documented
# "this is what would gate a future real send," and is not wired to
# anything in this phase.
DISCORD_SUMMARY_ENABLED = False


def build_daily_summary_payload(report: DailyReport) -> dict:
    """Pure function: DailyReport -> Discord-embed-shaped dict. No network
    call, no webhook reference of any kind."""
    ph = report.production_health
    pp = report.paper_portfolio
    rj = report.research_job
    mr = report.market_regime

    if pp.ok:
        portfolio_value = (
            f"equity=${pp.equity:,.2f} unrealized_pl=${pp.unrealized_pl:,.2f} "
            f"open_positions={pp.open_position_count}"
        )
    else:
        portfolio_value = f"unavailable: {pp.reason}"

    fields = [
        {"name": "Production run", "value": str(ph.status or "no runs yet"), "inline": True},
        {"name": "Tickers failed", "value": str(ph.tickers_failed), "inline": True},
        {"name": "Paper portfolio", "value": portfolio_value, "inline": False},
        {"name": "Research job", "value": str(rj.latest_status or "no runs yet"), "inline": True},
        {"name": "Market regime", "value": mr.label if mr.ok else (mr.reason or "unavailable"), "inline": True},
    ]

    return {
        "embeds": [
            {
                "title": f"Stock Dashboard — Daily Research Summary ({report.report_date})",
                "description": "Operational/research summary only — not a stock signal, not a trade recommendation.",
                "color": 0x3B82F6,
                "fields": fields,
                "footer": {"text": f"Generated at {report.generated_at}"},
            }
        ]
    }
