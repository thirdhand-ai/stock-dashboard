"""Tests for strategy_lab/production_guard.py's file-hashing logic - in
particular the Phase 12 config-drift fix: config/settings.py's guarded
hash covers only the WATCHLIST assignment, not the whole file, since that
file also holds unrelated dashboard/display/SMTP settings that should
never count as a frozen-config change. Synthetic tmp_path fixtures only -
never touches the real, git-tracked config/settings.py.
"""
from strategy_lab import production_guard

WATCHLIST_BLOCK = 'WATCHLIST = [\n    "AAPL",\n    "MSFT",\n]\n'


def _write_settings(base_dir, watchlist_block=WATCHLIST_BLOCK, extra=""):
    config_dir = base_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.py").write_text(
        f'"""settings"""\n{extra}\n{watchlist_block}\n'
    )


def test_hash_file_unaffected_by_unrelated_settings_addition(tmp_path, monkeypatch):
    """Reproduces the real Phase 12 false positive: appending SMTP settings
    and EXPLORATORY_WATCHLIST to config/settings.py (neither of which is
    WATCHLIST or any structured_values field) must not change the guarded
    fingerprint for that file."""
    monkeypatch.setattr(production_guard, "BASE_DIR", tmp_path)
    _write_settings(tmp_path)
    before = production_guard._hash_file("config/settings.py")

    _write_settings(
        tmp_path,
        extra=(
            'SMTP_HOST = "smtp.example.com"\n'
            'SMTP_PORT = 587\n'
            'EXPLORATORY_WATCHLIST = ["AVGO", "TSM", "AMD"]\n'
            'SIGNAL_COVERAGE_TICKERS = WATCHLIST + EXPLORATORY_WATCHLIST\n'
        ),
    )
    after = production_guard._hash_file("config/settings.py")

    assert before == after


def test_hash_file_changes_when_watchlist_itself_changes(tmp_path, monkeypatch):
    """The guard must still catch a real change to WATCHLIST."""
    monkeypatch.setattr(production_guard, "BASE_DIR", tmp_path)
    _write_settings(tmp_path)
    before = production_guard._hash_file("config/settings.py")

    changed_block = 'WATCHLIST = [\n    "AAPL",\n    "MSFT",\n    "NVDA",\n]\n'
    _write_settings(tmp_path, watchlist_block=changed_block)
    after = production_guard._hash_file("config/settings.py")

    assert before != after


def test_hash_file_missing_watchlist_assignment_is_a_distinct_value(tmp_path, monkeypatch):
    """If WATCHLIST is ever removed/renamed entirely, that's a real config
    change and must produce a fingerprint different from any normal hash -
    never silently match nothing."""
    monkeypatch.setattr(production_guard, "BASE_DIR", tmp_path)
    _write_settings(tmp_path)
    with_watchlist = production_guard._hash_file("config/settings.py")

    config_dir = tmp_path / "config"
    (config_dir / "settings.py").write_text('"""settings"""\nOTHER = 1\n')
    without_watchlist = production_guard._hash_file("config/settings.py")

    assert without_watchlist != with_watchlist
    assert without_watchlist == "MISSING:WATCHLIST"


def test_hash_file_other_guarded_files_are_still_whole_file_hashed(tmp_path, monkeypatch):
    """signals/config.py and friends have no GUARDED_SECTIONS entry - any
    change anywhere in them must still flip their hash, unchanged behavior
    from before this fix."""
    monkeypatch.setattr(production_guard, "BASE_DIR", tmp_path)
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir()
    (signals_dir / "config.py").write_text("X = 1\n")
    before = production_guard._hash_file("signals/config.py")

    (signals_dir / "config.py").write_text("X = 1\nY = 2\n")
    after = production_guard._hash_file("signals/config.py")

    assert before != after


def test_extract_assignment_source_ignores_surrounding_content():
    source = (
        '"""module docstring"""\n'
        'CREDENTIAL = "secret"\n'
        '\n'
        '# a comment above WATCHLIST\n'
        'WATCHLIST = ["AAPL", "MSFT"]\n'
        '\n'
        'OTHER_LIST = ["XLV"]\n'
    )
    segment = production_guard._extract_assignment_source(source, "WATCHLIST")
    assert segment == 'WATCHLIST = ["AAPL", "MSFT"]'


def test_extract_assignment_source_returns_none_when_absent():
    source = 'OTHER = 1\n'
    assert production_guard._extract_assignment_source(source, "WATCHLIST") is None
