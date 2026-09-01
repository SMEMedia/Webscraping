"""SME dashboard refresh and reporting application."""
import importlib
import sys

import streamlit as st

from dashboard import render_dashboard

st.set_page_config(page_title="SME Web Analytics", page_icon="📊", layout="wide")

refresh_tab, dashboard_tab = st.tabs(["Run Refresh", "Dashboard"])

with refresh_tab:
    # Reload so Streamlit executes the refresh page on every interaction.
    if "refresh_page" in sys.modules:
        importlib.reload(sys.modules["refresh_page"])
    else:
        importlib.import_module("refresh_page")

with dashboard_tab:
    render_dashboard()
