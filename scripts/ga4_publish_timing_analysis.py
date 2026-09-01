"""
GA4 Publish Timing Analysis

Pulls article performance from GA4, scrapes article publish datetimes, and
analyzes which publish days and hours perform best.

Outputs:
- publish_timing_article_analysis.csv
- publish_timing_weekday_summary.csv
- publish_timing_hour_summary.csv
- publish_timing_weekday_hour_summary.csv
- publish_timing_date_summary.csv
- publish_timing_top_slots.csv
- Google Sheet tabs:
  - PublishTiming_Articles
  - PublishTiming_Weekdays
  - PublishTiming_Hours
  - PublishTiming_WeekdayHours
  - PublishTiming_Dates
  - PublishTiming_TopSlots
"""

import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import numpy as np
import pandas as pd
import requests

from bs4 import BeautifulSoup

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
DATA_DIR = BASE_DIR / "data"
TIMING_DATA_DIR = DATA_DIR / "timing"
CACHE_DATA_DIR = DATA_DIR / "cache"
for data_dir in [TIMING_DATA_DIR, CACHE_DATA_DIR]:
    data_dir.mkdir(parents=True, exist_ok=True)

SPREADSHEET_NAME = "AM_Enriched_Articles"
SOURCE_WORKSHEET_NAME = "Keyword_Articles"
FALLBACK_SOURCE_WORKSHEET_NAME = "Article_List"

ARTICLE_OUTPUT_WORKSHEET_NAME = "PublishTiming_Articles"
WEEKDAY_OUTPUT_WORKSHEET_NAME = "PublishTiming_Weekdays"
HOUR_OUTPUT_WORKSHEET_NAME = "PublishTiming_Hours"
WEEKDAY_HOUR_OUTPUT_WORKSHEET_NAME = "PublishTiming_WeekdayHours"
DATE_OUTPUT_WORKSHEET_NAME = "PublishTiming_Dates"
TOP_SLOTS_OUTPUT_WORKSHEET_NAME = "PublishTiming_TopSlots"

PUBLISHED_DATETIME_CACHE_FILE = CACHE_DATA_DIR / "article_published_datetime_cache.csv"
PUBLISH_TIMING_ARTICLE_OUTPUT_FILE = (
    TIMING_DATA_DIR / "publish_timing_article_analysis.csv"
)
PUBLISH_TIMING_WEEKDAY_OUTPUT_FILE = (
    TIMING_DATA_DIR / "publish_timing_weekday_summary.csv"
)
PUBLISH_TIMING_HOUR_OUTPUT_FILE = TIMING_DATA_DIR / "publish_timing_hour_summary.csv"
PUBLISH_TIMING_WEEKDAY_HOUR_OUTPUT_FILE = (
    TIMING_DATA_DIR / "publish_timing_weekday_hour_summary.csv"
)
PUBLISH_TIMING_DATE_OUTPUT_FILE = TIMING_DATA_DIR / "publish_timing_date_summary.csv"
PUBLISH_TIMING_TOP_SLOTS_OUTPUT_FILE = (
    TIMING_DATA_DIR / "publish_timing_top_slots.csv"
)
PUBLISH_TIMEZONE = "America/New_York"
ALL_TIME_START_DATE = "2020-01-01"
MIN_ARTICLES_PER_SLOT = 3
DELAY_SECONDS = 3
MAX_SCRAPE_RETRIES = 1

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# =========================
# AUTH
# =========================

def get_credentials():
    return service_account_auth.get_credentials(SCOPES)


# =========================
# HELPERS
# =========================

def clean_url(url):
    if not url:
        return None

    url = str(url).strip()
    url = url.split("?")[0].split("#")[0]
    url = url.rstrip("/")

    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url


def split_grouped_urls(value):
    urls = []
    for raw_url in str(value or "").split(" | "):
        url = clean_url(raw_url)
        if url:
            urls.append(url)
    return urls


def clean_text(text):
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def clean_article_title(title):
    title = clean_text(title)
    if "|" in title:
        title = title.split("|", 1)[0]
    return clean_text(title)


def coerce_numeric(series):
    return pd.to_numeric(series, errors="coerce")


def normalize_bounce_rate(series):
    values = coerce_numeric(series)

    if values.dropna().empty:
        return values

    # Keep bounce rate as a decimal for Looker Studio percentage formatting.
    if values.max(skipna=True) > 1:
        values = values / 100

    return values


def weighted_average(values, weights):
    valid = values.notna() & weights.notna() & (weights > 0)
    if not valid.any():
        return np.nan
    return np.average(values[valid], weights=weights[valid])


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

    return pd.to_datetime(value)


def value_has_time(value):
    text = str(value or "").strip()
    return bool(re.search(r"T\d{1,2}:\d{2}| \d{1,2}:\d{2}", text))


def parse_published_datetime(value):
    if not value:
        return pd.NaT

    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT

    local_tz = ZoneInfo(PUBLISH_TIMEZONE)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(local_tz)
    else:
        parsed = parsed.tz_convert(local_tz)

    return parsed.tz_localize(None)


def first_jsonld_value(data, keys):
    if isinstance(data, list):
        for item in data:
            value = first_jsonld_value(item, keys)
            if value:
                return value

    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)
        if value:
            return value[0] if isinstance(value, list) else value

    for nested_key in ("@graph", "mainEntity", "mainEntityOfPage"):
        value = first_jsonld_value(data.get(nested_key), keys)
        if value:
            return value

    return None


def extract_published_datetime_from_soup(soup):
    selectors = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"name": "article:published_time"}, "content"),
        ("meta", {"name": "pubdate"}, "content"),
        ("meta", {"name": "publish_date"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"itemprop": "datePublished"}, "content"),
        ("time", {"datetime": True}, "datetime"),
    ]

    for tag_name, attrs, value_attr in selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get(value_attr):
            parsed = parse_published_datetime(tag.get(value_attr))
            if not pd.isna(parsed):
                return parsed, value_has_time(tag.get(value_attr)), f"{tag_name}:{value_attr}"

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue

        value = first_jsonld_value(data, ["datePublished", "dateCreated"])
        parsed = parse_published_datetime(value)
        if not pd.isna(parsed):
            return parsed, value_has_time(value), "jsonld"

    return pd.NaT, False, ""


def get_soup(url, max_retries=MAX_SCRAPE_RETRIES):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except Exception as error:
            print(f"Request error on attempt {attempt + 1} for {url}: {error}")
            if attempt + 1 < max_retries:
                time.sleep(DELAY_SECONDS)

    return None


def extract_published_datetime(url):
    soup = get_soup(url)
    if soup is None:
        return pd.NaT, False, ""
    return extract_published_datetime_from_soup(soup)


def load_published_datetime_cache():
    if not os.path.exists(PUBLISHED_DATETIME_CACHE_FILE):
        return pd.DataFrame(
            columns=[
                "url",
                "published_datetime",
                "published_time_available",
                "published_datetime_source",
            ]
        )

    cache_df = pd.read_csv(PUBLISHED_DATETIME_CACHE_FILE)
    if "url" not in cache_df.columns:
        return pd.DataFrame(
            columns=[
                "url",
                "published_datetime",
                "published_time_available",
                "published_datetime_source",
            ]
        )

    cache_df["url"] = cache_df["url"].map(clean_url)

    if "published_datetime" not in cache_df.columns:
        cache_df["published_datetime"] = pd.NaT
    else:
        cache_df["published_datetime"] = pd.to_datetime(
            cache_df["published_datetime"],
            errors="coerce",
        )

    if "published_datetime_source" not in cache_df.columns:
        cache_df["published_datetime_source"] = ""

    if "published_time_available" not in cache_df.columns:
        cache_df["published_time_available"] = cache_df["published_datetime"].notna()
    else:
        cache_df["published_time_available"] = (
            cache_df["published_time_available"]
            .fillna(False)
            .astype(str)
            .str.lower()
            .isin(["true", "1", "yes"])
        )

    return cache_df.dropna(subset=["url"]).drop_duplicates("url", keep="last")


def merge_published_datetime_rows(*dataframes):
    frames = [
        df.copy()
        for df in dataframes
        if df is not None and not df.empty
    ]
    if not frames:
        return pd.DataFrame(
            columns=[
                "url",
                "published_datetime",
                "published_time_available",
                "published_datetime_source",
            ]
        )

    combined_df = pd.concat(frames, ignore_index=True)
    combined_df["url"] = combined_df["url"].map(clean_url)
    combined_df["published_datetime"] = pd.to_datetime(
        combined_df["published_datetime"],
        errors="coerce",
    )
    combined_df["published_time_available"] = (
        combined_df["published_time_available"]
        .fillna(False)
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes"])
    )
    combined_df["published_datetime_source"] = combined_df[
        "published_datetime_source"
    ].fillna("")

    # Prefer a real publish time, then a date-only value, then an empty result.
    combined_df["_quality"] = (
        combined_df["published_datetime"].notna().astype(int)
        + combined_df["published_time_available"].astype(int) * 2
    )
    combined_df["_row_order"] = range(len(combined_df))
    combined_df = combined_df.sort_values(
        ["url", "_quality", "_row_order"],
        ascending=[True, True, True],
    )

    return (
        combined_df.dropna(subset=["url"])
        .drop_duplicates("url", keep="last")
        .drop(columns=["_quality", "_row_order"])
    )


def urls_missing_publish_time(article_urls, published_df):
    time_available_by_url = (
        published_df.groupby("url")["published_time_available"].any()
        if not published_df.empty
        else pd.Series(dtype=bool)
    )
    return [
        url
        for url in article_urls
        if url and not bool(time_available_by_url.get(url, False))
    ]


def collect_published_datetimes(article_urls, scrape_missing=True):
    cache_df = load_published_datetime_cache()
    urls_to_scrape = urls_missing_publish_time(article_urls, cache_df)

    print(f"Published datetime cache rows: {len(cache_df)}")
    print(f"New publish datetimes to scrape: {len(urls_to_scrape)}")

    if not scrape_missing:
        print("Skipping publish datetime scraping; using existing cache/sheet data only.")
        return cache_df

    new_rows = []
    for index, url in enumerate(urls_to_scrape, start=1):
        print(f"Scraping publish datetime {index}/{len(urls_to_scrape)}: {url}")
        published_datetime, time_available, source = extract_published_datetime(url)
        new_rows.append({
            "url": url,
            "published_datetime": published_datetime,
            "published_time_available": time_available,
            "published_datetime_source": source,
        })

    if new_rows:
        cache_df = merge_published_datetime_rows(
            cache_df,
            pd.DataFrame(new_rows),
        )
        cache_df.to_csv(PUBLISHED_DATETIME_CACHE_FILE, index=False)

    return cache_df


def first_existing_column(df, columns):
    for column in columns:
        if column in df.columns:
            return column
    return None


def build_known_published_datetimes(enriched_df):
    if enriched_df.empty:
        return pd.DataFrame(
            columns=[
                "url",
                "published_datetime",
                "published_time_available",
                "published_datetime_source",
            ]
        )

    published_column = first_existing_column(
        enriched_df,
        [
            "published_datetime",
            "published_time",
            "published_time_x",
            "published_time_y",
            "published_date",
        ],
    )
    if not published_column:
        return pd.DataFrame(
            columns=[
                "url",
                "published_datetime",
                "published_time_available",
                "published_datetime_source",
            ]
        )

    known_df = enriched_df[["url", published_column]].copy()
    known_df = known_df.rename(columns={published_column: "published_datetime"})
    raw_values = known_df["published_datetime"].copy()
    known_df["published_datetime"] = pd.to_datetime(
        known_df["published_datetime"],
        errors="coerce",
    )
    if published_column == "published_date":
        known_df["published_time_available"] = raw_values.map(value_has_time)
    else:
        known_df["published_time_available"] = known_df["published_datetime"].notna()
    known_df["published_datetime_source"] = f"{SOURCE_WORKSHEET_NAME}:{published_column}"

    return (
        known_df.dropna(subset=["url", "published_datetime"])
        .drop_duplicates("url", keep="last")
    )


def collect_published_datetimes_with_known(article_urls, known_df, scrape_missing=True):
    cache_df = load_published_datetime_cache()
    if known_df is not None and not known_df.empty:
        cache_df = merge_published_datetime_rows(cache_df, known_df)
        cache_df.to_csv(PUBLISHED_DATETIME_CACHE_FILE, index=False)

    urls_to_scrape = urls_missing_publish_time(article_urls, cache_df)

    print(f"Published datetime cache rows: {len(cache_df)}")
    print(f"Known publish datetimes from {SOURCE_WORKSHEET_NAME}: {len(known_df)}")
    print(f"New publish datetimes to scrape: {len(urls_to_scrape)}")

    if not scrape_missing:
        print("Skipping publish datetime scraping; using existing cache/sheet data only.")
        return cache_df

    new_rows = []
    for index, url in enumerate(urls_to_scrape, start=1):
        print(f"Scraping publish datetime {index}/{len(urls_to_scrape)}: {url}")
        published_datetime, time_available, source = extract_published_datetime(url)
        new_rows.append({
            "url": url,
            "published_datetime": published_datetime,
            "published_time_available": time_available,
            "published_datetime_source": source,
        })

    if new_rows:
        cache_df = merge_published_datetime_rows(
            cache_df,
            pd.DataFrame(new_rows),
        )
        cache_df.to_csv(PUBLISHED_DATETIME_CACHE_FILE, index=False)

    return cache_df


def should_scrape_missing_content():
    choice = input(
        "Scrape missing publish datetimes before pulling GA4? "
        "[Y = scrape + GA4, N = GA4/cache only] (default Y): "
    ).strip().lower()
    return choice not in {"n", "no", "g", "ga4", "ga4 only", "cache"}


# =========================
# INPUT DATA
# =========================

def read_enriched_articles(credentials):
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open(SPREADSHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(SOURCE_WORKSHEET_NAME)
        source_name = SOURCE_WORKSHEET_NAME
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.worksheet(FALLBACK_SOURCE_WORKSHEET_NAME)
        source_name = FALLBACK_SOURCE_WORKSHEET_NAME

    df = pd.DataFrame(worksheet.get_all_records())
    if df.empty and source_name != FALLBACK_SOURCE_WORKSHEET_NAME:
        worksheet = spreadsheet.worksheet(FALLBACK_SOURCE_WORKSHEET_NAME)
        source_name = FALLBACK_SOURCE_WORKSHEET_NAME
        df = pd.DataFrame(worksheet.get_all_records())

    if df.empty:
        return pd.DataFrame(columns=["url"])

    if "url" not in df.columns:
        raise ValueError("Source sheet is missing required column: url")

    rows = []
    for _, row in df.iterrows():
        urls = split_grouped_urls(row.get("url"))
        primary_url = clean_url(row.get("primary_url"))
        if primary_url and primary_url not in urls:
            urls.insert(0, primary_url)

        for url in urls:
            row_dict = row.to_dict()
            row_dict["url"] = url
            if "article_title" in row_dict and "title" not in row_dict:
                row_dict["title"] = clean_article_title(row_dict["article_title"])
            rows.append(row_dict)

    df = pd.DataFrame(rows)

    keep_columns = ["url"]
    for column in [
        "title",
        "article_title",
        "word_count",
        "character_count",
        "scrape_status",
        "published_date",
        "published_time",
        "published_datetime",
    ]:
        if column in df.columns:
            keep_columns.append(column)

    print(f"Using source worksheet: {source_name}")
    return df[keep_columns].dropna(subset=["url"]).drop_duplicates("url")


def get_ga4_article_metrics(credentials, start_date, end_date):
    client = BetaAnalyticsDataClient(credentials=credentials)

    article_filter = FilterExpression(
        filter=Filter(
            field_name="fullPageUrl",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS,
                value="article_",
                case_sensitive=False,
            ),
        )
    )

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="pageTitle"),
            Dimension(name="fullPageUrl"),
        ],
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="totalUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="bounceRate"),
            Metric(name="userEngagementDuration"),
            Metric(name="averageSessionDuration"),
        ],
        date_ranges=[
            DateRange(start_date=start_date, end_date=end_date)
        ],
        dimension_filter=article_filter,
        order_bys=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"),
                desc=True,
            )
        ],
        limit=100000,
    )

    response = client.run_report(request)

    returning_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="fullPageUrl")],
        metrics=[Metric(name="totalUsers")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=FilterExpression(
            and_group=FilterExpressionList(
                expressions=[
                    article_filter,
                    FilterExpression(
                        filter=Filter(
                            field_name="newVsReturning",
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.EXACT,
                                value="returning",
                                case_sensitive=False,
                            ),
                        )
                    ),
                ]
            )
        ),
        limit=100000,
    )
    returning_response = client.run_report(returning_request)
    returning_users_by_url = {}
    for row in returning_response.rows:
        url = clean_url(row.dimension_values[0].value)
        returning_users_by_url[url] = (
            returning_users_by_url.get(url, 0)
            + int(row.metric_values[0].value)
        )

    rows = []
    for row in response.rows:
        full_page_url = row.dimension_values[1].value
        views = int(row.metric_values[0].value)
        users = int(row.metric_values[1].value)
        new_users = int(row.metric_values[2].value)
        sessions = int(row.metric_values[3].value)
        engagement_seconds = float(row.metric_values[5].value)

        rows.append({
            "ga4_title": row.dimension_values[0].value,
            "full_page_url": full_page_url,
            "url": clean_url(full_page_url),
            "views": views,
            "users": users,
            "new_users": new_users,
            "sessions": sessions,
            "bounce_rate": float(row.metric_values[4].value),
            "total_user_engagement_seconds": engagement_seconds,
            "average_session_duration_seconds": float(row.metric_values[6].value),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["bounce_rate"] = normalize_bounce_rate(df["bounce_rate"])

    grouped_rows = []
    for url, group in df.groupby("url", dropna=True):
        sessions = group["sessions"]
        top_row = group.sort_values("views", ascending=False).iloc[0]
        total_engagement = group["total_user_engagement_seconds"].sum()
        total_views = group["views"].sum()

        grouped_rows.append({
            "url": url,
            "ga4_title": top_row["ga4_title"],
            "views": total_views,
            "users": group["users"].sum(),
            "new_users": group["new_users"].sum(),
            "returning_users_estimated": returning_users_by_url.get(url, 0),
            "sessions": sessions.sum(),
            "bounce_rate": weighted_average(group["bounce_rate"], sessions),
            "total_user_engagement_seconds": total_engagement,
            "average_engagement_time_seconds": (
                total_engagement / total_views if total_views else np.nan
            ),
            "average_session_duration_seconds": weighted_average(
                group["average_session_duration_seconds"],
                sessions,
            ),
        })

    return pd.DataFrame(grouped_rows)


# =========================
# ANALYSIS
# =========================

def add_publish_time_columns(df):
    df["published_datetime"] = pd.to_datetime(
        df["published_datetime"],
        errors="coerce",
    )
    df["published_date"] = df["published_datetime"].dt.date
    df["published_weekday"] = df["published_datetime"].dt.day_name()
    df["published_weekday_number"] = df["published_datetime"].dt.weekday
    if "published_time_available" not in df.columns:
        df["published_time_available"] = df["published_datetime"].notna()
    df["published_time_available"] = df["published_time_available"].fillna(False)
    df["published_hour"] = df["published_datetime"].dt.hour.where(
        df["published_time_available"]
    )
    df["published_hour_label"] = df["published_hour"].map(
        lambda hour: f"{int(hour):02d}:00" if pd.notna(hour) else ""
    )
    df["published_weekday_hour"] = (
        df["published_weekday"].fillna("")
        + " "
        + df["published_hour_label"].fillna("")
    ).str.strip()
    df["has_publish_time"] = df["published_datetime"].notna()
    return df


def first_non_blank(values):
    for value in values:
        if pd.notna(value) and str(value).strip():
            return value
    return ""


def aggregate_articles_by_title(article_df):
    if article_df.empty or "article_title" not in article_df.columns:
        return article_df

    grouped_rows = []
    for article_title, group in article_df.groupby("article_title", dropna=False):
        sessions = group["sessions"] if "sessions" in group.columns else pd.Series(dtype=float)
        views = group["views"] if "views" in group.columns else pd.Series(dtype=float)
        total_views = views.sum()
        total_engagement_seconds = (
            group["total_user_engagement_seconds"].sum()
            if "total_user_engagement_seconds" in group.columns
            else np.nan
        )
        unique_urls = sorted(set(group["url"].dropna()))
        top_row = group.sort_values("views", ascending=False).iloc[0]
        published_values = group["published_datetime"].dropna()

        row = {
            "article_title": article_title,
            "url": " | ".join(unique_urls),
            "primary_url": top_row["url"],
            "url_count": len(unique_urls),
            "published_datetime": (
                published_values.min() if not published_values.empty else pd.NaT
            ),
            "published_time_available": group.get(
                "published_time_available",
                pd.Series(dtype=bool),
            ).fillna(False).astype(bool).any(),
            "published_datetime_source": first_non_blank(
                group.get("published_datetime_source", pd.Series(dtype=str))
            ),
            "views": total_views,
            "users": group["users"].sum() if "users" in group.columns else 0,
            "new_users": group["new_users"].sum()
            if "new_users" in group.columns
            else 0,
            "returning_users_estimated": group["returning_users_estimated"].sum()
            if "returning_users_estimated" in group.columns
            else 0,
            "sessions": sessions.sum(),
            "bounce_rate": weighted_average(group["bounce_rate"], sessions)
            if "bounce_rate" in group.columns
            else np.nan,
            "average_engagement_time_seconds": (
                total_engagement_seconds / total_views
                if total_views > 0 and pd.notna(total_engagement_seconds)
                else np.nan
            ),
            "average_session_duration_seconds": weighted_average(
                group["average_session_duration_seconds"],
                sessions,
            )
            if "average_session_duration_seconds" in group.columns
            else np.nan,
            "total_user_engagement_seconds": total_engagement_seconds,
            "word_count": group["word_count"].dropna().max()
            if "word_count" in group.columns and not group["word_count"].dropna().empty
            else np.nan,
            "character_count": group["character_count"].dropna().max()
            if "character_count" in group.columns and not group["character_count"].dropna().empty
            else np.nan,
            "ga4_title": first_non_blank(group.get("ga4_title", pd.Series(dtype=str))),
        }
        grouped_rows.append(row)

    grouped_df = pd.DataFrame(grouped_rows)
    grouped_df = add_publish_time_columns(grouped_df)
    return grouped_df.sort_values("views", ascending=False)


def build_article_analysis(
    enriched_df,
    ga4_df,
    published_df,
    start_date,
    end_date,
    selected_active_urls=None,
):
    start_dt = resolve_ga4_date(start_date)
    end_dt = resolve_ga4_date(end_date)

    article_df = ga4_df.merge(enriched_df, on="url", how="outer")
    article_df = article_df.merge(published_df, on="url", how="left")
    article_df = add_publish_time_columns(article_df)

    if selected_active_urls is None:
        selected_active_urls = set(ga4_df["url"].dropna()) if "url" in ga4_df.columns else set()
    else:
        selected_active_urls = set(selected_active_urls)

    published_in_range = (
        (article_df["published_datetime"] >= start_dt)
        & (article_df["published_datetime"] <= end_dt + pd.Timedelta(days=1))
    )

    for column in [
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "total_user_engagement_seconds",
        "average_engagement_time_seconds",
        "average_session_duration_seconds",
        "word_count",
        "character_count",
    ]:
        if column in article_df.columns:
            article_df[column] = pd.to_numeric(
                article_df[column],
                errors="coerce",
            )

    for column in [
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "total_user_engagement_seconds",
    ]:
        if column in article_df.columns:
            article_df[column] = article_df[column].fillna(0)

    if "article_title" in article_df.columns:
        article_df["article_title"] = article_df["article_title"].fillna(
            article_df.get("title")
        )
        article_df["article_title"] = article_df["article_title"].fillna(
            article_df["ga4_title"]
        )
    elif "title" in article_df.columns:
        article_df["article_title"] = article_df["title"].fillna(
            article_df["ga4_title"]
        )
    else:
        article_df["article_title"] = article_df["ga4_title"]

    article_df["article_title"] = article_df["article_title"].map(clean_article_title)
    selected_article_mask = article_df["url"].isin(selected_active_urls) | published_in_range
    selected_titles = set(
        article_df.loc[selected_article_mask, "article_title"]
        .dropna()
        .map(clean_article_title)
    )
    selected_titles.discard("")
    article_df = article_df[
        selected_article_mask | article_df["article_title"].isin(selected_titles)
    ].copy()

    article_df = aggregate_articles_by_title(article_df)

    output_columns = [
        "article_title",
        "url",
        "primary_url",
        "url_count",
        "published_datetime",
        "published_date",
        "published_weekday",
        "published_weekday_number",
        "published_hour",
        "published_hour_label",
        "published_weekday_hour",
        "has_publish_time",
        "published_time_available",
        "published_datetime_source",
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "average_engagement_time_seconds",
        "average_session_duration_seconds",
        "total_user_engagement_seconds",
        "word_count",
        "character_count",
        "ga4_title",
    ]
    output_columns = [column for column in output_columns if column in article_df.columns]

    return article_df[output_columns].sort_values("views", ascending=False)


def performance_summary(df, group_columns, sort_columns=None):
    if isinstance(group_columns, str):
        group_columns = [group_columns]

    valid_df = df.dropna(subset=["published_datetime"]).copy()
    for column in group_columns:
        valid_df = valid_df[valid_df[column].notna()]

    if valid_df.empty:
        return pd.DataFrame()

    summary = (
        valid_df.groupby(group_columns, dropna=False)
        .agg(
            article_count=("url", "nunique"),
            total_views=("views", "sum"),
            avg_views=("views", "mean"),
            median_views=("views", "median"),
            max_views=("views", "max"),
            total_users=("users", "sum"),
            avg_users=("users", "mean"),
            total_new_users=("new_users", "sum"),
            avg_new_users=("new_users", "mean"),
            total_returning_users_estimated=(
                "returning_users_estimated",
                "sum",
            ),
            avg_returning_users_estimated=(
                "returning_users_estimated",
                "mean",
            ),
            total_sessions=("sessions", "sum"),
            avg_sessions=("sessions", "mean"),
            weighted_bounce_rate=(
                "bounce_rate",
                lambda values: weighted_average(
                    values,
                    valid_df.loc[values.index, "sessions"],
                ),
            ),
            avg_engagement_time_seconds=(
                "average_engagement_time_seconds",
                "mean",
            ),
            avg_session_duration_seconds=(
                "average_session_duration_seconds",
                "mean",
            ),
        )
        .reset_index()
    )

    summary["views_per_article_rank"] = summary["median_views"].rank(
        method="dense",
        ascending=False,
    ).astype(int)
    summary["avg_views_rank"] = summary["avg_views"].rank(
        method="dense",
        ascending=False,
    ).astype(int)

    if sort_columns is None:
        sort_columns = ["views_per_article_rank", "avg_views_rank"]

    return summary.sort_values(sort_columns).reset_index(drop=True)


def build_weekday_summary(article_df):
    summary = performance_summary(
        article_df,
        ["published_weekday_number", "published_weekday"],
        ["published_weekday_number"],
    )
    if summary.empty:
        return summary
    return summary.drop(columns=["published_weekday_number"])


def build_hour_summary(article_df):
    return performance_summary(
        article_df,
        ["published_hour", "published_hour_label"],
        ["published_hour"],
    )


def build_weekday_hour_summary(article_df):
    return performance_summary(
        article_df,
        [
            "published_weekday_number",
            "published_weekday",
            "published_hour",
            "published_hour_label",
            "published_weekday_hour",
        ],
    )


def build_date_summary(article_df):
    return performance_summary(article_df, "published_date")


def build_top_slots(weekday_df, hour_df, weekday_hour_df):
    frames = []
    for slot_type, df in [
        ("weekday", weekday_df),
        ("hour", hour_df),
        ("weekday_hour", weekday_hour_df),
    ]:
        if df.empty:
            continue

        eligible = df[df["article_count"] >= MIN_ARTICLES_PER_SLOT].copy()
        if eligible.empty:
            eligible = df.copy()

        if slot_type == "weekday":
            eligible["slot"] = eligible["published_weekday"]
        elif slot_type == "hour":
            eligible["slot"] = eligible["published_hour_label"]
        else:
            eligible["slot"] = eligible["published_weekday_hour"]

        eligible["slot_type"] = slot_type
        frames.append(
            eligible[
                [
                    "slot_type",
                    "slot",
                    "article_count",
                    "total_views",
                    "avg_views",
                    "median_views",
                    "total_users",
                    "total_new_users",
                    "avg_new_users",
                    "total_returning_users_estimated",
                    "avg_returning_users_estimated",
                    "weighted_bounce_rate",
                    "avg_engagement_time_seconds",
                    "views_per_article_rank",
                    "avg_views_rank",
                ]
            ].sort_values(
                ["views_per_article_rank", "avg_views_rank", "article_count"],
                ascending=[True, True, False],
            ).head(10)
        )

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


# =========================
# GOOGLE SHEETS WRITE
# =========================

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
        worksheet.update([["No rows"]])
        return

    write_df = df.copy()
    for column in write_df.columns:
        if pd.api.types.is_datetime64_any_dtype(write_df[column]):
            write_df[column] = write_df[column].dt.strftime("%Y-%m-%d %H:%M:%S")

    worksheet.update([write_df.columns.tolist()] + sheet_values(write_df))


def sheet_value(value):
    if pd.isna(value):
        return ""

    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d %H:%M:%S")

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


def write_analysis_to_google_sheet(
    credentials,
    article_df,
    weekday_df,
    hour_df,
    weekday_hour_df,
    date_df,
    top_slots_df,
):
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open(SPREADSHEET_NAME)

    update_or_create_worksheet(spreadsheet, ARTICLE_OUTPUT_WORKSHEET_NAME, article_df)
    update_or_create_worksheet(spreadsheet, WEEKDAY_OUTPUT_WORKSHEET_NAME, weekday_df)
    update_or_create_worksheet(spreadsheet, HOUR_OUTPUT_WORKSHEET_NAME, hour_df)
    update_or_create_worksheet(
        spreadsheet,
        WEEKDAY_HOUR_OUTPUT_WORKSHEET_NAME,
        weekday_hour_df,
    )
    update_or_create_worksheet(spreadsheet, DATE_OUTPUT_WORKSHEET_NAME, date_df)
    update_or_create_worksheet(
        spreadsheet,
        TOP_SLOTS_OUTPUT_WORKSHEET_NAME,
        top_slots_df,
    )


# =========================
# MAIN
# =========================

def main(start_date=None, end_date=None, scrape_missing=None):
    if start_date is None:
        start_date = input(
            "Enter start date (YYYY-MM-DD) or relative value like 30daysAgo: "
        ).strip()

    if end_date is None:
        end_date = input(
            "Enter end date (YYYY-MM-DD or today): "
        ).strip()

    if scrape_missing is None:
        scrape_missing = should_scrape_missing_content()

    credentials = get_credentials()

    print("Reading enriched article sheet...")
    enriched_df = read_enriched_articles(credentials)
    print(f"Loaded {len(enriched_df)} enriched article rows.")

    print("Pulling GA4 article URLs for the selected date range...")
    selected_ga4_df = get_ga4_article_metrics(credentials, start_date, end_date)
    print(f"Loaded {len(selected_ga4_df)} selected GA4 article URL rows.")

    if selected_ga4_df.empty:
        print("No article URLs found in GA4 for the requested date range.")
        selected_ga4_df = pd.DataFrame(
            columns=[
                "url",
                "ga4_title",
                "views",
                "users",
                "new_users",
                "returning_users_estimated",
                "sessions",
                "bounce_rate",
                "total_user_engagement_seconds",
                "average_engagement_time_seconds",
                "average_session_duration_seconds",
            ]
        )

    selected_active_urls = set(selected_ga4_df["url"].dropna())

    all_time_end_date = "today" if end_date != "yesterday" else "yesterday"
    print(
        "Pulling all-time GA4 article metrics "
        f"from {ALL_TIME_START_DATE} to {all_time_end_date}..."
    )
    ga4_df = get_ga4_article_metrics(
        credentials,
        ALL_TIME_START_DATE,
        all_time_end_date,
    )
    print(f"Loaded {len(ga4_df)} all-time GA4 article URL rows.")

    if ga4_df.empty:
        ga4_df = pd.DataFrame(
            columns=[
                "url",
                "ga4_title",
                "views",
                "users",
                "new_users",
                "returning_users_estimated",
                "sessions",
                "bounce_rate",
                "total_user_engagement_seconds",
                "average_engagement_time_seconds",
                "average_session_duration_seconds",
            ]
        )

    candidate_urls = sorted(set(enriched_df["url"].dropna()) | selected_active_urls)
    print(f"Candidate article URLs for publish timing analysis: {len(candidate_urls)}")

    known_published_df = build_known_published_datetimes(enriched_df)
    published_df = collect_published_datetimes_with_known(
        candidate_urls,
        known_published_df,
        scrape_missing,
    )

    print("Building title-grouped publish timing analysis with all-time metrics...")
    article_df = build_article_analysis(
        enriched_df,
        ga4_df,
        published_df,
        start_date,
        end_date,
        selected_active_urls,
    )

    if article_df.empty:
        print("No matched articles remained after filtering.")
        return

    weekday_df = build_weekday_summary(article_df)
    hour_df = build_hour_summary(article_df)
    weekday_hour_df = build_weekday_hour_summary(article_df)
    date_df = build_date_summary(article_df)
    top_slots_df = build_top_slots(weekday_df, hour_df, weekday_hour_df)

    article_df.to_csv(PUBLISH_TIMING_ARTICLE_OUTPUT_FILE, index=False)
    weekday_df.to_csv(PUBLISH_TIMING_WEEKDAY_OUTPUT_FILE, index=False)
    hour_df.to_csv(PUBLISH_TIMING_HOUR_OUTPUT_FILE, index=False)
    weekday_hour_df.to_csv(PUBLISH_TIMING_WEEKDAY_HOUR_OUTPUT_FILE, index=False)
    date_df.to_csv(PUBLISH_TIMING_DATE_OUTPUT_FILE, index=False)
    top_slots_df.to_csv(PUBLISH_TIMING_TOP_SLOTS_OUTPUT_FILE, index=False)

    print("Writing publish timing tabs to Google Sheets...")
    write_analysis_to_google_sheet(
        credentials,
        article_df,
        weekday_df,
        hour_df,
        weekday_hour_df,
        date_df,
        top_slots_df,
    )

    print("\nTop publish timing slots:")
    if top_slots_df.empty:
        print("No publish timing summaries were available.")
    else:
        print(top_slots_df.head(30).to_string(index=False))

    print("\nDone.")


if __name__ == "__main__":
    main()
