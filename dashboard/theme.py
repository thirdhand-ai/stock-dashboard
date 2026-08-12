"""Color palette and CSS for the dashboard.

Dark-mode-first: this dashboard deliberately commits to one polished look
rather than supporting a light/dark toggle. All hex values are the
dark-surface steps from the project's validated reference palette (see the
dataviz skill's references/palette.md) - not invented ad hoc. Categorical
identity uses the fixed slot order; score/stage use the reserved status and
ordinal ramps respectively, never a cycled/rainbow hue.
"""
from backtest.config import DEFAULT_RULES
from signals.engine import STAGE_MOMENTUM, STAGE_NONE, STAGE_TREND, STAGE_VOLUME

# --- Chart chrome & ink (dark surface) ---
SURFACE = "#1a1a19"
PAGE_PLANE = "#0d0d0d"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#c3c2b7"
TEXT_MUTED = "#898781"
GRIDLINE = "#2c2c2a"
AXIS = "#383835"

# --- Categorical slots (fixed order, never cycled) ---
SLOT_BLUE = "#3987e5"      # 1 - primary series / strategy equity
SLOT_ORANGE = "#d95926"    # 2 - benchmark / buy & hold
SLOT_AQUA = "#199e70"
SLOT_YELLOW = "#c98500"
SLOT_VIOLET = "#9085e9"

# --- Status palette (reserved - never reused for a plain series) ---
STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#e66767"

# --- Ordinal ramp for highest_confirmed_stage (funnel-stage pattern) ---
STAGE_COLORS = {
    STAGE_NONE: TEXT_MUTED,
    STAGE_TREND: "#5598e7",
    STAGE_MOMENTUM: "#2a78d6",
    STAGE_VOLUME: "#184f95",
}
STAGE_LABELS = {
    STAGE_NONE: "None",
    STAGE_TREND: "Trend",
    STAGE_MOMENTUM: "Momentum",
    STAGE_VOLUME: "Volume",
}

# Candlestick up/down follows the domain convention, tied to the status palette.
CANDLE_UP = STATUS_GOOD
CANDLE_DOWN = "#d03b3b"


def score_status(score: float):
    """Map a raw 0-100 score to a (label, color) status pair, using the
    project's own configured entry/exit thresholds as the band edges rather
    than inventing separate cutoffs for display purposes."""
    if score is None:
        return "n/a", TEXT_MUTED
    if score >= DEFAULT_RULES.entry_min_score:
        return "Strong", STATUS_GOOD
    if score <= DEFAULT_RULES.exit_max_score:
        return "Weak", STATUS_CRITICAL
    return "Building", STATUS_WARNING


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, str(stage))


def stage_color(stage: str) -> str:
    return STAGE_COLORS.get(stage, TEXT_MUTED)


PLOTLY_LAYOUT_DEFAULTS = dict(
    paper_bgcolor=SURFACE,
    plot_bgcolor=SURFACE,
    font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
    xaxis=dict(gridcolor=GRIDLINE, linecolor=AXIS, zerolinecolor=AXIS),
    yaxis=dict(gridcolor=GRIDLINE, linecolor=AXIS, zerolinecolor=AXIS),
    legend=dict(bgcolor="rgba(0,0,0,0)"),
    margin=dict(l=40, r=20, t=40, b=30),
)


def apply_layout_defaults(fig):
    """Apply the shared dark-theme layout to a Plotly figure, without
    clobbering axis config the caller already set on multi-panel figures."""
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(bgcolor="rgba(0,0,0,0)"),
        margin=dict(l=40, r=20, t=40, b=30),
    )
    fig.update_xaxes(gridcolor=GRIDLINE, linecolor=AXIS, zerolinecolor=AXIS)
    fig.update_yaxes(gridcolor=GRIDLINE, linecolor=AXIS, zerolinecolor=AXIS)
    return fig


CUSTOM_CSS = f"""
<style>
    .stApp {{
        background-color: {PAGE_PLANE};
    }}
    section[data-testid="stSidebar"] {{
        background-color: {SURFACE};
        border-right: 1px solid {GRIDLINE};
    }}
    div[data-testid="stMetric"] {{
        background-color: {SURFACE};
        border: 1px solid {GRIDLINE};
        border-radius: 10px;
        padding: 14px 16px;
    }}
    div[data-testid="stMetricLabel"] {{
        color: {TEXT_MUTED};
    }}
    .dash-card {{
        background-color: {SURFACE};
        border: 1px solid {GRIDLINE};
        border-radius: 10px;
        padding: 16px 18px;
        margin-bottom: 10px;
    }}
    .dash-badge {{
        display: inline-block;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.02em;
    }}
    .dash-muted {{
        color: {TEXT_MUTED};
        font-size: 0.85rem;
    }}
    .dash-disclaimer {{
        background-color: {SURFACE};
        border-left: 3px solid {STATUS_WARNING};
        border-radius: 4px;
        padding: 10px 14px;
        color: {TEXT_SECONDARY};
        font-size: 0.85rem;
    }}
    h1, h2, h3 {{
        color: {TEXT_PRIMARY};
    }}
    hr {{
        border-color: {GRIDLINE};
    }}
</style>
"""


def badge_html(text: str, color: str) -> str:
    return f'<span class="dash-badge" style="background-color:{color}22; color:{color}; border:1px solid {color}55;">{text}</span>'
