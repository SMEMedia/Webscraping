"""Operator interface for the SME dashboard data refresh."""
import io
import json
import os
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def configure_credentials():
    if "google_service_account" in st.secrets:
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = json.dumps(
            dict(st.secrets["google_service_account"])
        )


def output_zip():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(DATA_DIR.rglob("*.csv")):
            if "cache" not in path.parts:
                bundle.write(path, path.relative_to(BASE_DIR))
    return archive.getvalue()


st.set_page_config(page_title="Dashboard Data Refresh", page_icon="🔄")
configure_credentials()
st.title("Dashboard Data Refresh")
st.write("Refresh the data used by the Webscraping and Company Mentions dashboards.")
with st.expander("What happens when I run this?"):
    st.write("The app reads the approved article list, checks Google Analytics, collects missing article details, and rebuilds the dashboard data files. Keep this page open until it finishes.")

today = date.today()
start_date = st.date_input("Start date", today - timedelta(days=700), max_value=today)
end_date = st.date_input("End date", today, max_value=today)
scrape = st.checkbox("Collect details for new articles", True, help="Leave selected for a normal refresh.")
if start_date > end_date:
    st.error("The start date must be on or before the end date.")

credentials_ready = any(os.environ.get(name) for name in (
    "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS"
))
if not credentials_ready:
    st.warning("This app has not been connected to the SME Google account. An administrator must complete the one-time setup in the README.")

if st.button("Run dashboard refresh", type="primary", disabled=start_date > end_date or not credentials_ready):
    command = [sys.executable, str(BASE_DIR / "scripts" / "run_all_ga4_analyses.py"),
               "--start-date", start_date.isoformat(), "--end-date", end_date.isoformat(),
               "--scrape" if scrape else "--no-scrape"]
    with st.status("Refreshing dashboard data…", expanded=True) as status:
        result = subprocess.run(command, cwd=BASE_DIR, capture_output=True, text=True, env=os.environ.copy())
        if result.returncode == 0:
            status.update(label="Refresh complete", state="complete")
            st.success("Refresh complete. The Google Sheet used by the dashboards has been updated.")
            st.download_button("Download optional backup files", output_zip(),
                               f"dashboard-data-{today.isoformat()}.zip", "application/zip")
        else:
            status.update(label="Refresh did not finish", state="error")
            st.error("The refresh stopped. Send the technical details below to the dashboard administrator.")
        with st.expander("Technical details"):
            st.code((result.stdout + "\n" + result.stderr).strip() or "No details returned.")
