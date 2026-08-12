"""Streamlit dashboard entry point.

Run with: streamlit run dashboard/app.py

Reuses the Phase 1-3 ingestion/indicator/signal/backtest modules as the
source of truth - this file and its views never read API credentials or
call external APIs directly.
"""
import sys
from pathlib import Path

# streamlit run only adds this file's own directory to sys.path, not the
# project root - add it explicitly so `dashboard.*` and the Phase 1-3
# packages (config, db, indicators, signals, backtest) are importable
# regardless of the working directory the command was launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from dashboard.pages_registry import NAV_STRUCTURE
from dashboard.theme import CUSTOM_CSS

st.set_page_config(
    page_title="Stock Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

nav = st.navigation(NAV_STRUCTURE)
nav.run()
