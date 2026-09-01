"""
GA4 General Analytics Export

Pulls general GA4 site analytics and writes Power BI-friendly tables to
Google Sheets.

Outputs:
- general_analytics_kpis.csv
- general_analytics_users_by_state.csv
- general_analytics_users_by_date.csv
- general_analytics_source_medium.csv
- Google Sheet tabs:
  - General_KPIs
  - General_Users_By_State
  - General_Users_By_Date
  - General_Source_Medium
"""

import re
from datetime import date, datetime
from pathlib import Path

import gspread
import numpy as np
import pandas as pd

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Filter,
    FilterExpression,
    FilterExpressionList,
    Metric,
    OrderBy,
    RunReportRequest,
)

import service_account_auth


# =========================
# CONFIG
# =========================

PROPERTY_ID = "432233519"

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
GENERAL_DATA_DIR = BASE_DIR / "data" / "general"
GENERAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

SPREADSHEET_NAME = "AM_Enriched_Articles"

KPI_OUTPUT_WORKSHEET_NAME = "General_KPIs"
STATE_OUTPUT_WORKSHEET_NAME = "General_Users_By_State"
DATE_OUTPUT_WORKSHEET_NAME = "General_Users_By_Date"
SOURCE_MEDIUM_OUTPUT_WORKSHEET_NAME = "General_Source_Medium"
STATE_DAILY_OUTPUT_WORKSHEET_NAME = "General_Users_By_State_Daily"
SOURCE_MEDIUM_DAILY_OUTPUT_WORKSHEET_NAME = "General_Source_Medium_Daily"

KPI_OUTPUT_FILE = GENERAL_DATA_DIR / "general_analytics_kpis.csv"
STATE_OUTPUT_FILE = GENERAL_DATA_DIR / "general_analytics_users_by_state.csv"
DATE_OUTPUT_FILE = GENERAL_DATA_DIR / "general_analytics_users_by_date.csv"
SOURCE_MEDIUM_OUTPUT_FILE = (
    GENERAL_DATA_DIR / "general_analytics_source_medium.csv"
)
STATE_DAILY_OUTPUT_FILE = GENERAL_DATA_DIR / "general_analytics_users_by_state_daily.csv"
SOURCE_MEDIUM_DAILY_OUTPUT_FILE = (
    GENERAL_DATA_DIR / "general_analytics_source_medium_daily.csv"
)
USER_SPIKE_SOURCE_OUTPUT_FILE = GENERAL_DATA_DIR / "user_spike_source.csv"
USER_SPIKE_REGION_OUTPUT_FILE = GENERAL_DATA_DIR / "user_spike_region.csv"
USER_SPIKE_CAMPAIGN_OUTPUT_FILE = GENERAL_DATA_DIR / "user_spike_campaign.csv"
USER_SPIKE_DEVICE_OUTPUT_FILE = GENERAL_DATA_DIR / "user_spike_device.csv"
USER_SPIKE_LANDING_OUTPUT_FILE = GENERAL_DATA_DIR / "user_spike_landing.csv"
USER_SPIKE_SOURCE_LANDING_OUTPUT_FILE = (
    GENERAL_DATA_DIR / "user_spike_source_landing.csv"
)
USER_SPIKE_METRIC_COLUMNS = [
    "totalUsers",
    "newUsers",
    "returning_users_estimated",
    "sessions",
    "screenPageViews",
]

DEFAULT_START_DATE = "700daysAgo"
DEFAULT_END_DATE = "today"

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# =========================
# AUTH
# =========================

def get_credentials():
    return service_account_auth.get_credentials(SCOPES)


# =========================
# HELPERS
# =========================

def resolve_ga4_date(date_value):
    value = str(date_value).strip()
    today = pd.Timestamp.today().normalize()

    if value == "today":
        return today

    if value == "yesterday":
        return today - pd.Timedelta(days=1)

    match = re.fullmatch(r"(\d+)daysAgo", value)
    if match:
        return today - pd.Timedelta(days=int(match.group(1)))

    return pd.to_datetime(value).normalize()


def ga4_date_string(timestamp):
    return pd.to_datetime(timestamp).strftime("%Y-%m-%d")


def previous_period(start_date, end_date):
    start_dt = resolve_ga4_date(start_date)
    end_dt = resolve_ga4_date(end_date)
    period_days = (end_dt - start_dt).days + 1
    previous_end = start_dt - pd.Timedelta(days=1)
    previous_start = previous_end - pd.Timedelta(days=period_days - 1)
    return ga4_date_string(previous_start), ga4_date_string(previous_end)


def coerce_numeric(df, columns):
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def percent_change(current_value, previous_value):
    if previous_value in (0, None) or pd.isna(previous_value):
        return np.nan
    return (current_value - previous_value) / previous_value


def us_country_filter():
    return FilterExpression(
        filter=Filter(
            field_name="country",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT,
                value="United States",
                case_sensitive=False,
            ),
        )
    )


def run_report(
    credentials,
    start_date,
    end_date,
    dimensions,
    metrics,
    order_metric=None,
    desc=True,
    dimension_filter=None,
    limit=100000,
):
    client = BetaAnalyticsDataClient(credentials=credentials)
    rows = []
    offset = 0

    while True:
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[Dimension(name=dimension) for dimension in dimensions],
            metrics=[Metric(name=metric) for metric in metrics],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            dimension_filter=dimension_filter,
            order_bys=[
                OrderBy(
                    metric=OrderBy.MetricOrderBy(metric_name=order_metric),
                    desc=desc,
                )
            ] if order_metric else [],
            limit=limit,
            offset=offset,
        )

        response = client.run_report(request)
        if not response.rows:
            break

        for row in response.rows:
            row_dict = {}
            for index, dimension in enumerate(dimensions):
                row_dict[dimension] = row.dimension_values[index].value
            for index, metric in enumerate(metrics):
                row_dict[metric] = row.metric_values[index].value
            rows.append(row_dict)

        offset += len(response.rows)
        if len(response.rows) < limit:
            break

    df = pd.DataFrame(rows)
    return coerce_numeric(df, metrics)


def combine_dimension_filters(*filters):
    active_filters = [item for item in filters if item is not None]
    if not active_filters:
        return None
    if len(active_filters) == 1:
        return active_filters[0]
    return FilterExpression(
        and_group=FilterExpressionList(expressions=active_filters)
    )


def returning_user_filter():
    return FilterExpression(
        filter=Filter(
            field_name="newVsReturning",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT,
                value="returning",
                case_sensitive=False,
            ),
        )
    )


def run_returning_users_report(
    credentials,
    start_date,
    end_date,
    dimensions,
    dimension_filter=None,
):
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=dimensions,
        metrics=["totalUsers"],
        dimension_filter=combine_dimension_filters(
            dimension_filter,
            returning_user_filter(),
        ),
    )
    return df.rename(columns={"totalUsers": "returning_users_estimated"})


def parse_ga4_date_column(df):
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    return df


# =========================
# ANALYSIS TABLES
# =========================

def build_kpi_summary(credentials, start_date, end_date):
    previous_start, previous_end = previous_period(start_date, end_date)

    current_df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=[],
        metrics=["screenPageViews", "totalUsers", "newUsers", "sessions"],
    )
    previous_df = run_report(
        credentials,
        previous_start,
        previous_end,
        dimensions=[],
        metrics=["screenPageViews", "totalUsers", "newUsers", "sessions"],
    )
    current_returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=[],
    )
    previous_returning_df = run_returning_users_report(
        credentials,
        previous_start,
        previous_end,
        dimensions=[],
    )

    current_values = current_df.iloc[0].to_dict() if not current_df.empty else {}
    previous_values = previous_df.iloc[0].to_dict() if not previous_df.empty else {}
    current_returning_values = (
        current_returning_df.iloc[0].to_dict()
        if not current_returning_df.empty
        else {}
    )
    previous_returning_values = (
        previous_returning_df.iloc[0].to_dict()
        if not previous_returning_df.empty
        else {}
    )

    metric_labels = {
        "screenPageViews": "total_views",
        "totalUsers": "total_users",
        "newUsers": "new_users",
        "sessions": "total_sessions",
    }

    rows = []
    for metric, label in metric_labels.items():
        current_value = float(current_values.get(metric, 0))
        previous_value = float(previous_values.get(metric, 0))
        rows.append({
            "metric": label,
            "current_period_start": start_date,
            "current_period_end": end_date,
            "previous_period_start": previous_start,
            "previous_period_end": previous_end,
            "current_value": current_value,
            "previous_value": previous_value,
            "change": current_value - previous_value,
            "percent_change": percent_change(current_value, previous_value),
        })

    kpi_df = pd.DataFrame(rows)

    current_returning = float(
        current_returning_values.get("returning_users_estimated", 0)
    )
    previous_returning = float(
        previous_returning_values.get("returning_users_estimated", 0)
    )

    returning_row = {
        "metric": "returning_users_estimated",
        "current_period_start": start_date,
        "current_period_end": end_date,
        "previous_period_start": previous_start,
        "previous_period_end": previous_end,
        "current_value": current_returning,
        "previous_value": previous_returning,
        "change": current_returning - previous_returning,
        "percent_change": percent_change(current_returning, previous_returning),
    }

    return pd.concat([kpi_df, pd.DataFrame([returning_row])], ignore_index=True)


def build_users_by_state(credentials, start_date, end_date):
    country_filter = us_country_filter()
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=["country", "region"],
        metrics=["totalUsers", "newUsers", "sessions", "screenPageViews"],
        order_metric="totalUsers",
        dimension_filter=country_filter,
    )
    if df.empty:
        return df

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=["country", "region"],
        dimension_filter=country_filter,
    )
    df = df.merge(returning_df, on=["country", "region"], how="left")
    df = df.rename(columns={
        "region": "state",
        "totalUsers": "users",
        "newUsers": "new_users",
        "screenPageViews": "views",
    })
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    df["period_start"] = start_date
    df["period_end"] = end_date
    return df[
        [
            "period_start",
            "period_end",
            "country",
            "state",
            "users",
            "new_users",
            "returning_users_estimated",
            "sessions",
            "views",
        ]
    ].sort_values("users", ascending=False)


def build_users_by_state_daily(credentials, start_date, end_date):
    country_filter = us_country_filter()
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date", "country", "region"],
        metrics=["totalUsers", "newUsers", "sessions", "screenPageViews"],
        order_metric=None,
        dimension_filter=country_filter,
    )
    if df.empty:
        return df

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date", "country", "region"],
        dimension_filter=country_filter,
    )
    if returning_df.empty:
        returning_df = pd.DataFrame(
            columns=["date", "country", "region", "returning_users_estimated"]
        )
    df = df.merge(returning_df, on=["date", "country", "region"], how="left")
    df = parse_ga4_date_column(df)
    df = df.rename(columns={
        "region": "state",
        "totalUsers": "users",
        "newUsers": "new_users",
        "screenPageViews": "views",
    })
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    return df[
        [
            "date",
            "country",
            "state",
            "users",
            "new_users",
            "returning_users_estimated",
            "sessions",
            "views",
        ]
    ].sort_values(["date", "users"], ascending=[True, False])


def build_users_by_date(credentials, start_date, end_date):
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date"],
        metrics=["totalUsers", "newUsers", "sessions", "screenPageViews"],
        order_metric=None,
    )
    if df.empty:
        return df

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date"],
    )
    df = df.merge(returning_df, on=["date"], how="left")
    df = parse_ga4_date_column(df)
    df = df.rename(columns={
        "totalUsers": "users",
        "newUsers": "new_users",
        "screenPageViews": "views",
    })
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    df["weekday"] = df["date"].dt.day_name()
    return df[
        [
            "date",
            "weekday",
            "users",
            "new_users",
            "returning_users_estimated",
            "sessions",
            "views",
        ]
    ].sort_values("date")


def build_source_medium(credentials, start_date, end_date):
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=["sessionSourceMedium"],
        metrics=["screenPageViews", "sessions", "totalUsers", "newUsers"],
        order_metric="screenPageViews",
    )
    if df.empty:
        return df

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=["sessionSourceMedium"],
    )
    df = df.merge(returning_df, on=["sessionSourceMedium"], how="left")
    df = df.rename(columns={
        "sessionSourceMedium": "session_source_medium",
        "screenPageViews": "views",
        "totalUsers": "users",
        "newUsers": "new_users",
    })
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    df["period_start"] = start_date
    df["period_end"] = end_date
    return df[
        [
            "period_start",
            "period_end",
            "session_source_medium",
            "views",
            "sessions",
            "users",
            "new_users",
            "returning_users_estimated",
        ]
    ].sort_values("views", ascending=False)


def build_source_medium_daily(credentials, start_date, end_date):
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date", "sessionSourceMedium"],
        metrics=["screenPageViews", "sessions", "totalUsers", "newUsers"],
        order_metric=None,
    )
    if df.empty:
        return df

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=["date", "sessionSourceMedium"],
    )
    if returning_df.empty:
        returning_df = pd.DataFrame(
            columns=[
                "date",
                "sessionSourceMedium",
                "returning_users_estimated",
            ]
        )
    df = df.merge(returning_df, on=["date", "sessionSourceMedium"], how="left")
    df = parse_ga4_date_column(df)
    df = df.rename(columns={
        "sessionSourceMedium": "session_source_medium",
        "screenPageViews": "views",
        "totalUsers": "users",
        "newUsers": "new_users",
    })
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    return df[
        [
            "date",
            "session_source_medium",
            "views",
            "sessions",
            "users",
            "new_users",
            "returning_users_estimated",
        ]
    ].sort_values(["date", "views"], ascending=[True, False])


def build_user_spike_table(credentials, start_date, end_date, dimensions):
    output_columns = [*dimensions, *USER_SPIKE_METRIC_COLUMNS]
    df = run_report(
        credentials,
        start_date,
        end_date,
        dimensions=dimensions,
        metrics=["totalUsers", "newUsers", "sessions", "screenPageViews"],
        order_metric="screenPageViews",
    )
    if df.empty:
        return pd.DataFrame(columns=output_columns)

    returning_df = run_returning_users_report(
        credentials,
        start_date,
        end_date,
        dimensions=dimensions,
    )
    if returning_df.empty:
        returning_df = pd.DataFrame(
            columns=[*dimensions, "returning_users_estimated"]
        )

    df = df.merge(returning_df, on=dimensions, how="left")
    df["returning_users_estimated"] = (
        df["returning_users_estimated"].fillna(0)
    )
    for column in output_columns:
        if column not in df.columns:
            df[column] = np.nan
    return df[output_columns]


def build_user_spike_tables(credentials, start_date, end_date):
    return {
        USER_SPIKE_SOURCE_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            ["date", "sessionSourceMedium"],
        ),
        USER_SPIKE_REGION_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            ["date", "region"],
        ),
        USER_SPIKE_CAMPAIGN_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            ["date", "sessionCampaignName"],
        ),
        USER_SPIKE_DEVICE_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            ["date", "deviceCategory"],
        ),
        USER_SPIKE_LANDING_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            ["date", "landingPagePlusQueryString"],
        ),
        USER_SPIKE_SOURCE_LANDING_OUTPUT_FILE: build_user_spike_table(
            credentials,
            start_date,
            end_date,
            [
                "date",
                "sessionSourceMedium",
                "sessionCampaignName",
                "landingPagePlusQueryString",
            ],
        ),
    }


# =========================
# GOOGLE SHEETS WRITE
# =========================

def sheet_value(value):
    if pd.isna(value):
        return ""

    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    if isinstance(value, np.generic):
        return value.item()

    return value


def sheet_values(df):
    return [
        [sheet_value(value) for value in row]
        for row in df.replace([np.inf, -np.inf], np.nan).itertuples(
            index=False,
            name=None,
        )
    ]


def update_or_create_worksheet(spreadsheet, worksheet_name, df):
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=max(len(df) + 10, 100),
            cols=max(len(df.columns) + 2, 10),
        )

    worksheet.clear()

    if df.empty:
        if len(df.columns) > 0:
            worksheet.update([df.columns.tolist()])
            return
        worksheet.update([["No rows"]])
        return

    worksheet.update([df.columns.tolist()] + sheet_values(df))


def write_analysis_to_google_sheet(
    credentials,
    kpi_df,
    state_df,
    date_df,
    source_medium_df,
    state_daily_df,
    source_medium_daily_df,
):
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open(SPREADSHEET_NAME)

    update_or_create_worksheet(spreadsheet, KPI_OUTPUT_WORKSHEET_NAME, kpi_df)
    update_or_create_worksheet(spreadsheet, STATE_OUTPUT_WORKSHEET_NAME, state_df)
    update_or_create_worksheet(spreadsheet, DATE_OUTPUT_WORKSHEET_NAME, date_df)
    update_or_create_worksheet(
        spreadsheet,
        SOURCE_MEDIUM_OUTPUT_WORKSHEET_NAME,
        source_medium_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        STATE_DAILY_OUTPUT_WORKSHEET_NAME,
        state_daily_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SOURCE_MEDIUM_DAILY_OUTPUT_WORKSHEET_NAME,
        source_medium_daily_df,
    )


# =========================
# MAIN
# =========================

def main(start_date=None, end_date=None):
    if start_date is None:
        start_date = DEFAULT_START_DATE

    if end_date is None:
        end_date = DEFAULT_END_DATE

    credentials = get_credentials()
    print(f"Export window: {start_date} to {end_date}")

    print("Building KPI comparison...")
    kpi_df = build_kpi_summary(credentials, start_date, end_date)

    print("Pulling users by state...")
    state_df = build_users_by_state(credentials, start_date, end_date)

    print("Pulling users by date...")
    date_df = build_users_by_date(credentials, start_date, end_date)

    print("Pulling source/medium by views...")
    source_medium_df = build_source_medium(credentials, start_date, end_date)

    print("Pulling daily users by state...")
    state_daily_df = build_users_by_state_daily(credentials, start_date, end_date)

    print("Pulling daily source/medium by views...")
    source_medium_daily_df = build_source_medium_daily(
        credentials,
        start_date,
        end_date,
    )

    print("Pulling user spike tables...")
    user_spike_tables = build_user_spike_tables(credentials, start_date, end_date)

    kpi_df.to_csv(KPI_OUTPUT_FILE, index=False)
    state_df.to_csv(STATE_OUTPUT_FILE, index=False)
    date_df.to_csv(DATE_OUTPUT_FILE, index=False)
    source_medium_df.to_csv(SOURCE_MEDIUM_OUTPUT_FILE, index=False)
    state_daily_df.to_csv(STATE_DAILY_OUTPUT_FILE, index=False)
    source_medium_daily_df.to_csv(SOURCE_MEDIUM_DAILY_OUTPUT_FILE, index=False)
    for output_file, spike_df in user_spike_tables.items():
        spike_df.to_csv(output_file, index=False)

    print("Writing general analytics tabs to Google Sheets...")
    write_analysis_to_google_sheet(
        credentials,
        kpi_df,
        state_df,
        date_df,
        source_medium_df,
        state_daily_df,
        source_medium_daily_df,
    )

    print("\nKPI summary:")
    print(kpi_df.to_string(index=False))
    print("\nDone.")


if __name__ == "__main__":
    main()
