"""Plotly figure builders for the dashboard.

Kept separate from the view modules so the figure-construction logic can be
exercised without a Streamlit runtime. Multi-panel, shared-x-axis design
throughout - price/volume/RSI/MACD/ADX are never forced onto a single shared
y-axis (no dual-axis charts).
"""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest.config import DEFAULT_RULES
from signals.config import DEFAULT_THRESHOLDS
from dashboard.theme import (
    CANDLE_DOWN,
    CANDLE_UP,
    GRIDLINE,
    SLOT_AQUA,
    SLOT_BLUE,
    SLOT_ORANGE,
    SLOT_VIOLET,
    SLOT_YELLOW,
    STATUS_CRITICAL,
    STATUS_GOOD,
    STATUS_WARNING,
    SURFACE,
    TEXT_MUTED,
    TEXT_SECONDARY,
)


def build_ticker_chart(df: pd.DataFrame, show_ma50: bool = True, show_bbands: bool = True) -> go.Figure:
    """df: an indicator-enriched OHLCV history (see indicators.technical.enrich_with_indicators)."""
    dates = pd.to_datetime(df["date"])

    fig = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.36, 0.12, 0.16, 0.18, 0.18],
        vertical_spacing=0.025,
        subplot_titles=("Price", "Volume", "RSI", "MACD", "ADX"),
    )

    # --- Price panel ---
    fig.add_trace(
        go.Candlestick(
            x=dates, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="Price",
            increasing_line_color=CANDLE_UP, decreasing_line_color=CANDLE_DOWN,
            increasing_fillcolor=CANDLE_UP, decreasing_fillcolor=CANDLE_DOWN,
        ),
        row=1, col=1,
    )
    if show_ma50:
        fig.add_trace(
            go.Scatter(x=dates, y=df["sma_50"], name="50-day MA", line=dict(color=SLOT_VIOLET, width=1.5)),
            row=1, col=1,
        )
    if show_bbands:
        fig.add_trace(
            go.Scatter(x=dates, y=df["bb_upper"], name="BB Upper", line=dict(color=TEXT_MUTED, width=1, dash="dot"), showlegend=False),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=dates, y=df["bb_lower"], name="Bollinger Bands (20, 2σ)",
                line=dict(color=TEXT_MUTED, width=1, dash="dot"),
                fill="tonexty", fillcolor="rgba(137,135,129,0.08)",
            ),
            row=1, col=1,
        )

    # --- Volume panel ---
    vol_colors = [CANDLE_UP if c >= o else CANDLE_DOWN for o, c in zip(df["open"], df["close"])]
    fig.add_trace(go.Bar(x=dates, y=df["volume"], name="Volume", marker_color=vol_colors, opacity=0.7, showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=df["volume_avg_20"], name="20-day avg volume", line=dict(color=SLOT_ORANGE, width=1.5)), row=2, col=1)

    # --- RSI panel ---
    fig.add_trace(go.Scatter(x=dates, y=df["rsi"], name="RSI (14)", line=dict(color=SLOT_BLUE, width=1.5)), row=3, col=1)
    fig.add_hline(y=DEFAULT_THRESHOLDS.rsi_overbought, line=dict(color=STATUS_CRITICAL, width=1, dash="dash"), row=3, col=1)
    fig.add_hline(y=DEFAULT_THRESHOLDS.rsi_bullish_min, line=dict(color=STATUS_GOOD, width=1, dash="dash"), row=3, col=1)
    fig.update_yaxes(range=[0, 100], row=3, col=1)

    # --- MACD panel ---
    fig.add_trace(go.Scatter(x=dates, y=df["macd"], name="MACD", line=dict(color=SLOT_BLUE, width=1.5)), row=4, col=1)
    fig.add_trace(go.Scatter(x=dates, y=df["macd_signal"], name="Signal", line=dict(color=SLOT_ORANGE, width=1.5)), row=4, col=1)
    hist_colors = [STATUS_GOOD if v >= 0 else STATUS_CRITICAL for v in df["macd_hist"].fillna(0)]
    fig.add_trace(go.Bar(x=dates, y=df["macd_hist"], name="Histogram", marker_color=hist_colors, opacity=0.6, showlegend=False), row=4, col=1)

    # --- ADX panel ---
    fig.add_trace(go.Scatter(x=dates, y=df["adx"], name="ADX (14)", line=dict(color=SLOT_BLUE, width=1.5)), row=5, col=1)
    fig.add_hline(
        y=DEFAULT_THRESHOLDS.adx_trend_threshold,
        line=dict(color=STATUS_WARNING, width=1, dash="dash"),
        row=5, col=1,
        annotation_text="trend threshold", annotation_font_color=TEXT_MUTED, annotation_position="top left",
    )

    fig.update_layout(
        height=900,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.02, x=0),
        margin=dict(l=50, r=20, t=40, b=30),
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRIDLINE, rangeslider_visible=False)
    fig.update_yaxes(gridcolor=GRIDLINE)
    for annotation in fig.layout.annotations:
        annotation.font.color = TEXT_SECONDARY

    return fig


def build_equity_curve_chart(strategy_equity: pd.Series, buy_hold_equity: pd.Series) -> go.Figure:
    """Both series indexed to a common base (100 at t0) on a single axis -
    never a dual-axis comparison."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=strategy_equity.index, y=strategy_equity.values, name="Strategy",
        line=dict(color=SLOT_BLUE, width=2),
    ))
    fig.add_trace(go.Scatter(
        x=buy_hold_equity.index, y=buy_hold_equity.values, name="Buy & Hold",
        line=dict(color=SLOT_ORANGE, width=2),
    ))
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.05, x=0),
        margin=dict(l=50, r=20, t=30, b=30),
        yaxis_title="Equity (indexed to 100 at start)",
        hovermode="x unified",
        height=420,
    )
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE)
    return fig


def build_portfolio_equity_chart(equity_series: pd.Series, benchmark_series: pd.Series = None, benchmark_label: str = "SPY") -> go.Figure:
    """Locally captured portfolio_snapshots equity, optionally indexed
    alongside a benchmark on the same axis (see dashboard.charts.index_to_100)."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=equity_series.index, y=equity_series.values, name="Portfolio equity",
        line=dict(color=SLOT_BLUE, width=2),
    ))
    if benchmark_series is not None:
        fig.add_trace(go.Scatter(
            x=benchmark_series.index, y=benchmark_series.values, name=benchmark_label,
            line=dict(color=SLOT_ORANGE, width=2),
        ))
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.05, x=0),
        margin=dict(l=50, r=20, t=30, b=30),
        yaxis_title="Equity (indexed to 100 at first snapshot)" if benchmark_series is not None else "Equity ($)",
        hovermode="x unified",
        height=380,
    )
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE)
    return fig


def build_drawdown_chart(equity_series: pd.Series) -> go.Figure:
    """Drawdown from the running peak of a locally captured equity series."""
    running_max = equity_series.cummax()
    drawdown_pct = (equity_series / running_max - 1.0) * 100

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=drawdown_pct.index, y=drawdown_pct.values, name="Drawdown",
        line=dict(color=STATUS_CRITICAL, width=1.5),
        fill="tozeroy", fillcolor="rgba(230,103,103,0.15)",
    ))
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        margin=dict(l=50, r=20, t=20, b=30),
        yaxis_title="Drawdown (%)",
        hovermode="x unified",
        height=220,
        showlegend=False,
    )
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE)
    return fig


def index_to_100(series: pd.Series) -> pd.Series:
    """Rebase a series to start at 100 - the standard way to compare two
    differently-scaled series on one axis instead of a dual-axis chart."""
    first = series.iloc[0]
    return series / first * 100.0


_GENERIC_SLOTS = [SLOT_BLUE, SLOT_ORANGE, SLOT_AQUA, SLOT_YELLOW, SLOT_VIOLET]


def build_bar_chart(labels, values, y_title: str = "", color=SLOT_BLUE, height: int = 320) -> go.Figure:
    """Generic single-series bar chart with the shared dark theme applied -
    used by the Strategy Lab research page for bucket/stage/ticker/year
    breakdowns rather than inventing a new chart style per breakdown."""
    colors = [STATUS_GOOD if v >= 0 else STATUS_CRITICAL for v in values] if color == "pnl" else color
    fig = go.Figure(go.Bar(x=list(labels), y=list(values), marker_color=colors))
    fig.update_layout(
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        margin=dict(l=50, r=20, t=20, b=30), yaxis_title=y_title, height=height, showlegend=False,
    )
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE, zeroline=True, zerolinecolor=GRIDLINE)
    return fig


def build_multi_line_chart(series_dict, y_title: str = "", height: int = 380) -> go.Figure:
    """Generic multi-series line chart (2+ named pd.Series on one shared
    axis) - used for strategy-vs-benchmark and friction comparisons."""
    fig = go.Figure()
    for i, (name, series) in enumerate(series_dict.items()):
        color = _GENERIC_SLOTS[i % len(_GENERIC_SLOTS)]
        fig.add_trace(go.Scatter(x=series.index, y=series.values, name=name, line=dict(color=color, width=2)))
    fig.update_layout(
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(color=TEXT_SECONDARY, family="system-ui, -apple-system, 'Segoe UI', sans-serif"),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.05, x=0),
        margin=dict(l=50, r=20, t=30, b=30), yaxis_title=y_title, hovermode="x unified", height=height,
    )
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE)
    return fig
