"""Tests for alerts/price_config.py's resolve_percent_band() - the pure
function that converts a symmetric percent band around a baseline price
into concrete (above, below) dollar levels. This is the only place percent
mode does any percent-specific math; everything downstream (crossing
detection, delivery, dashboard display of above/below) just sees the
resolved dollar values, same as fixed mode always has.
"""
import pytest

from alerts.price_config import MODE_FIXED, MODE_PERCENT, PriceThreshold, resolve_percent_band


def test_resolve_percent_band_computes_symmetric_above_below():
    above, below = resolve_percent_band(baseline_price=150.0, percent=6.0)
    assert above == pytest.approx(159.0)
    assert below == pytest.approx(141.0)


def test_resolve_percent_band_scales_with_baseline_price():
    above, below = resolve_percent_band(baseline_price=1000.0, percent=6.0)
    assert above == pytest.approx(1060.0)
    assert below == pytest.approx(940.0)


def test_resolve_percent_band_small_percent():
    above, below = resolve_percent_band(baseline_price=200.0, percent=0.5)
    assert above == pytest.approx(201.0)
    assert below == pytest.approx(199.0)


def test_resolve_percent_band_rejects_zero_or_negative_baseline_price():
    with pytest.raises(ValueError):
        resolve_percent_band(baseline_price=0.0, percent=6.0)
    with pytest.raises(ValueError):
        resolve_percent_band(baseline_price=-10.0, percent=6.0)


def test_resolve_percent_band_rejects_zero_or_negative_percent():
    with pytest.raises(ValueError):
        resolve_percent_band(baseline_price=150.0, percent=0.0)
    with pytest.raises(ValueError):
        resolve_percent_band(baseline_price=150.0, percent=-6.0)


def test_price_threshold_defaults_to_fixed_mode_with_no_percent_fields():
    """Backward compatibility: constructing PriceThreshold the old way (no
    mode/percent/baseline_price) still produces a fixed-mode threshold
    indistinguishable from before these fields existed."""
    t = PriceThreshold(ticker="AAPL", above=320.0, below=290.0)
    assert t.mode == MODE_FIXED
    assert t.percent is None
    assert t.baseline_price is None


def test_price_threshold_percent_mode_carries_metadata():
    t = PriceThreshold(ticker="AAPL", above=159.0, below=141.0, mode=MODE_PERCENT, percent=6.0, baseline_price=150.0)
    assert t.mode == MODE_PERCENT
    assert t.percent == pytest.approx(6.0)
    assert t.baseline_price == pytest.approx(150.0)
