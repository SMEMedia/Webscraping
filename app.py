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
earliest_date = date(2020, 1, 1)
selected_dates = st.date_input(
    "Dates to include",
    value=(today - timedelta(days=700), today),
    min_value=earliest_date,
    max_value=today,
    format="MM/DD/YYYY",
    help="Select the first and last day to include in the dashboard refresh.",
)

# Streamlit returns one date while the operator is still choosing a range.
if len(selected_dates) == 2:
    start_date, end_date = selected_dates
    dates_ready = True
else:
    start_date = end_date = selected_dates[0]
    dates_ready = False
    st.info("Select an end date to complete the date range.")
scrape = st.checkbox("Collect details for new articles", True, help="Leave selected for a normal refresh.")

credentials_ready = any(os.environ.get(name) for name in (
    "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_APPLICATION_CREDENTIALS"
))
if not credentials_ready:
    st.warning("This app has not been connected to the SME Google account. An administrator must complete the one-time setup in the README.")

if st.button("Run dashboard refresh", type="primary", disabled=not dates_ready or not credentials_ready):
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
