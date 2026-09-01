"""
GA4 Keyword Analysis Pipeline

Reads enriched article URLs from Google Sheets, scrapes article text, pulls GA4
metrics, and analyzes how keyword segments and article word frequency relate to
views and bounce rate.

Optional keyword segment config:
Create keyword_segments.json next to this file:

{
  "ai": ["ai", "artificial intelligence", "chatgpt"],
  "seo": ["seo", "search engine", "google ranking"],
  "events": ["conference", "webinar", "summit"]
}

Outputs:
- keyword_article_analysis.csv
- keyword_segment_summary.csv
- keyword_frequency_summary.csv
- keyword_article_text_cache.csv
- Google Sheet tabs:
  - Keyword_Articles
  - Keyword_Segments
  - Keyword_Frequency
"""

import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import gspread
import numpy as np
import pandas as pd
import requests

from bs4 import BeautifulSoup
from google.api_core import exceptions as google_api_exceptions

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

import company_list_store
import service_account_auth


# =========================
# CONFIG
# =========================

PROPERTY_ID = "432233519"

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent if SCRIPT_DIR.name == "scripts" else SCRIPT_DIR
DATA_DIR = BASE_DIR / "data"
KEYWORD_DATA_DIR = DATA_DIR / "keyword"
TITLE_DATA_DIR = DATA_DIR / "title"
COMPANY_DATA_DIR = DATA_DIR / "company"
SECTION_DATA_DIR = DATA_DIR / "section"
CACHE_DATA_DIR = DATA_DIR / "cache"
for data_dir in [
    KEYWORD_DATA_DIR,
    TITLE_DATA_DIR,
    COMPANY_DATA_DIR,
    SECTION_DATA_DIR,
    CACHE_DATA_DIR,
]:
    data_dir.mkdir(parents=True, exist_ok=True)

SPREADSHEET_NAME = "AM_Enriched_Articles"
SOURCE_WORKSHEET_NAME = "Keyword_Articles"
FALLBACK_SOURCE_WORKSHEET_NAME = "Article_List"

ARTICLE_OUTPUT_WORKSHEET_NAME = "Keyword_Articles"
ARTICLE_DAILY_OUTPUT_WORKSHEET_NAME = "Keyword_Articles_Daily"
SEGMENT_OUTPUT_WORKSHEET_NAME = "Keyword_Segments"
FREQUENCY_OUTPUT_WORKSHEET_NAME = "Keyword_Frequency"
SEGMENT_DISTRIBUTION_WORKSHEET_NAME = "Keyword_Segment_Distribution"
TITLE_ARTICLE_OUTPUT_WORKSHEET_NAME = "Title_Articles"
TITLE_KEYWORD_OUTPUT_WORKSHEET_NAME = "Title_Keywords"
TITLE_LENGTH_OUTPUT_WORKSHEET_NAME = "Title_Length"
COMPANY_ARTICLE_OUTPUT_WORKSHEET_NAME = "Company_Article_Mentions"
COMPANY_SUMMARY_OUTPUT_WORKSHEET_NAME = "Company_Mentions"
SECTION_ARTICLE_OUTPUT_WORKSHEET_NAME = "Section_Articles"
SECTION_SUMMARY_OUTPUT_WORKSHEET_NAME = "Section_Performance"
SECTION_DAILY_OUTPUT_WORKSHEET_NAME = "Section_Daily_Article_Metrics"

KEYWORD_SEGMENTS_FILE = BASE_DIR / "config" / "keyword_segments.json"
COMPANY_NAMES_FILE = BASE_DIR / "config" / "company_names.json"
ARTICLE_TEXT_CACHE_FILE = CACHE_DATA_DIR / "keyword_article_text_cache.csv"
PUBLISHED_DATETIME_CACHE_FILE = CACHE_DATA_DIR / "article_published_datetime_cache.csv"
ARTICLE_TEXT_CACHE_COLUMNS = [
    "url",
    "article_text",
    "scrape_status",
    "published_date",
    "scraped_author",
    "scraped_section",
    "metadata_scrape_status",
]

KEYWORD_ARTICLE_OUTPUT_FILE = KEYWORD_DATA_DIR / "keyword_article_analysis.csv"
KEYWORD_ARTICLE_DAILY_OUTPUT_FILE = (
    KEYWORD_DATA_DIR / "keyword_article_daily_metrics.csv"
)
KEYWORD_SEGMENT_OUTPUT_FILE = KEYWORD_DATA_DIR / "keyword_segment_summary.csv"
KEYWORD_FREQUENCY_OUTPUT_FILE = KEYWORD_DATA_DIR / "keyword_frequency_summary.csv"
KEYWORD_SEGMENT_DISTRIBUTION_OUTPUT_FILE = (
    KEYWORD_DATA_DIR / "keyword_segment_distribution.csv"
)
TITLE_ARTICLE_OUTPUT_FILE = TITLE_DATA_DIR / "title_article_analysis.csv"
TITLE_KEYWORD_OUTPUT_FILE = TITLE_DATA_DIR / "title_keyword_summary.csv"
TITLE_LENGTH_OUTPUT_FILE = TITLE_DATA_DIR / "title_length_summary.csv"
COMPANY_ARTICLE_OUTPUT_FILE = COMPANY_DATA_DIR / "company_article_mentions.csv"
COMPANY_SUMMARY_OUTPUT_FILE = COMPANY_DATA_DIR / "company_mentions_summary.csv"
SECTION_ARTICLE_OUTPUT_FILE = SECTION_DATA_DIR / "section_article_analysis.csv"
SECTION_SUMMARY_OUTPUT_FILE = SECTION_DATA_DIR / "section_performance_summary.csv"
SECTION_DAILY_OUTPUT_FILE = SECTION_DATA_DIR / "section_daily_article_metrics.csv"

AUTHOR_SOURCE_COLUMNS = [
    "author",
    "article_author",
    "byline",
    "writer",
]
SECTION_SOURCE_COLUMNS = [
    "section",
    "article_section",
    "category",
    "primary_section",
    "primary_section_tag",
]
SECTION_ARTICLE_COLUMNS = [
    "article_title",
    "url",
    "published_date",
    "views",
    "users",
    "new_users",
    "returning_users_estimated",
    "sessions",
    "bounce_rate",
    "avg_time_on_page",
    "average_engagement_time_seconds",
    "section_tag",
    "primary_section_tag",
]
SECTION_SUMMARY_COLUMNS = [
    "section_tag",
    "article_count",
    "total_views",
    "avg_views",
    "median_views",
    "total_users",
    "total_new_users",
    "total_returning_users_estimated",
    "median_returning_users_estimated",
    "total_sessions",
    "weighted_bounce_rate",
    "weighted_time_on_page",
    "avg_engagement_time_seconds",
]

ALL_TIME_START_DATE = "2020-01-01"
MAX_WORD_COUNT = 99999
TOP_WORD_LIMIT = 250
MIN_WORD_LENGTH = 3
MIN_ARTICLES_WITH_WORD = 5
MIN_ARTICLES_WITH_TITLE_WORD = 3
MIN_ARTICLES_WITH_COMPANY = 2
DELAY_SECONDS = 3
MAX_SCRAPE_RETRIES = 1
GA4_PAGE_LIMIT = 25000
GA4_REQUEST_TIMEOUT_SECONDS = 180
GA4_MAX_RETRIES = 3
GA4_RETRY_DELAY_SECONDS = 10
GA4_AUTHOR_DIMENSION_CANDIDATES = [
    "customEvent:author",
    "customEvent:article_author",
    "customEvent:byline",
    "customEvent:writer",
    "author",
]

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

STOP_WORDS = {
    "about", "above", "after", "again", "against", "also", "although",
    "always", "among", "because", "been", "before", "being", "below",
    "between", "both", "but", "cannot", "could", "did", "does", "doing",
    "down", "during", "each", "from", "further", "had", "has", "have",
    "having", "here", "hers", "him", "his", "how", "into", "its", "itself",
    "just", "like", "more", "most", "other", "our", "ours", "out", "over",
    "own", "same", "she", "should", "some", "such", "than", "that", "the",
    "their", "theirs", "them", "then", "there", "these", "they", "this",
    "those", "through", "too", "under", "until", "very", "was", "were",
    "what", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "would", "you", "your",
}

TITLE_STOP_WORDS = STOP_WORDS | {
    "advancedmanufacturing",
    "advanced",
    "manufacturing",
    "org",
    "and",
    "for",
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


def clean_author_name(value):
    value = clean_text(value)
    value = re.sub(r"^(by|author)\s*[:\-]?\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\|\s*.*$", "", value)
    value = re.sub(r"\s*/\s*.*$", "", value)
    return clean_text(value)


def fill_blank_from_column(df, target_column, source_column):
    if source_column not in df.columns:
        return df

    if target_column not in df.columns:
        df[target_column] = ""

    target_blank = df[target_column].isna() | (
        df[target_column].astype(str).str.strip() == ""
    )
    source_not_blank = df[source_column].notna() & (
        df[source_column].astype(str).str.strip() != ""
    )
    df.loc[target_blank & source_not_blank, target_column] = df.loc[
        target_blank & source_not_blank,
        source_column,
    ]
    return df


def drop_helper_columns(df, columns):
    return df.drop(columns=[column for column in columns if column in df.columns])


def coerce_numeric(series):
    return pd.to_numeric(series, errors="coerce")


def normalize_bounce_rate(series):
    values = coerce_numeric(series)

    if values.dropna().empty:
        return values

    # Keep bounce rate as a decimal for Looker Studio percentage formatting.
    # Example: 0.45 displays as 45% when formatted as Percent.
    if values.max(skipna=True) > 1:
        values = values / 100

    return values


def weighted_average(values, weights):
    valid = values.notna() & weights.notna() & (weights > 0)
    if not valid.any():
        return np.nan
    return np.average(values[valid], weights=weights[valid])


def median_numeric(values):
    numeric_values = pd.Series(pd.to_numeric(values, errors="coerce")).dropna()
    if numeric_values.empty:
        return np.nan
    return numeric_values.median()


def estimated_returning_users(total_users, new_users):
    if pd.isna(total_users) or pd.isna(new_users):
        return np.nan
    return max(float(total_users) - float(new_users), 0)


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


def tokenize(text):
    words = re.findall(r"[a-z][a-z'-]+", str(text).lower())
    return [
        word.strip("'-")
        for word in words
        if len(word.strip("'-")) >= MIN_WORD_LENGTH
        and word.strip("'-") not in STOP_WORDS
    ]


def tokenize_title(text):
    text = clean_article_title(text)
    words = re.findall(r"[a-z][a-z'-]+", str(text).lower())
    return [
        word.strip("'-")
        for word in words
        if len(word.strip("'-")) >= MIN_WORD_LENGTH
        and word.strip("'-") not in TITLE_STOP_WORDS
    ]


def keyword_pattern(keyword):
    escaped = re.escape(keyword.lower().strip())
    return re.compile(rf"(?<!\w){escaped}(?!\w)", flags=re.IGNORECASE)


def extract_section_tag(url):
    parsed = urlparse(str(url))
    path_parts = [
        part for part in parsed.path.strip("/").split("/")
        if part and not part.startswith("article_")
    ]
    if not path_parts:
        return ""
    return path_parts[0].strip().lower()


def extract_section_tags(url_value):
    return sorted({
        extract_section_tag(url)
        for url in split_grouped_urls(url_value)
        if extract_section_tag(url)
    })


def parse_published_date(value):
    if not value:
        return pd.NaT

    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return pd.NaT

    return parsed.tz_convert(None).normalize()


def extract_published_date(soup):
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
            return parse_published_date(tag.get(value_attr))

    return pd.NaT


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

    required_columns = {"url"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(
            f"Source sheet is missing required columns: {sorted(missing)}"
        )

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

    if "word_count" in df.columns:
        df["word_count"] = coerce_numeric(df["word_count"])

    if "character_count" in df.columns:
        df["character_count"] = coerce_numeric(df["character_count"])

    if "published_date" in df.columns:
        df["published_date"] = pd.to_datetime(
            df["published_date"],
            errors="coerce",
        )

    print(f"Using source worksheet: {source_name}")
    return df.dropna(subset=["url"]).drop_duplicates("url")


def load_keyword_segments():
    if os.path.exists(KEYWORD_SEGMENTS_FILE):
        with open(KEYWORD_SEGMENTS_FILE, "r", encoding="utf-8") as file:
            segments = json.load(file)
    else:
        raw_segments = input(
            "Enter keyword segments as name=term,term;name=term,term "
            "or press Enter to skip segment analysis: "
        ).strip()

        segments = {}
        if raw_segments:
            for segment_definition in raw_segments.split(";"):
                if "=" not in segment_definition:
                    continue
                name, raw_keywords = segment_definition.split("=", 1)
                keywords = [
                    keyword.strip()
                    for keyword in raw_keywords.split(",")
                    if keyword.strip()
                ]
                if name.strip() and keywords:
                    segments[name.strip()] = keywords

    normalized_segments = {}
    for segment_name, keywords in segments.items():
        normalized_segments[str(segment_name).strip()] = [
            str(keyword).strip().lower()
            for keyword in keywords
            if str(keyword).strip()
        ]

    return normalized_segments


def normalize_company_list(names):
    deduped_names = {}
    for name in names:
        canonical = canonical_company_name(name)
        if canonical and not is_generic_company_name(name):
            deduped_names.setdefault(canonical.casefold(), canonical)

    return sorted(deduped_names.values(), key=str.casefold)


def company_list_key(name):
    return company_filter_key(canonical_company_name(name))


def google_modified_time(file_metadata):
    modified_time = file_metadata.get("modifiedTime") if file_metadata else None
    if not modified_time:
        return pd.NaT
    return pd.to_datetime(modified_time, errors="coerce", utc=True)


def local_company_list_modified_time():
    if not company_list_store.COMPANY_NAMES_FILE.exists():
        return pd.NaT
    return pd.to_datetime(
        company_list_store.COMPANY_NAMES_FILE.stat().st_mtime,
        unit="s",
        utc=True,
    )


def update_excluded_company_names(doc_names, local_names, file_metadata=None):
    doc_names = normalize_company_list(doc_names)
    local_names = normalize_company_list(local_names)
    excluded_names = normalize_company_list(
        company_list_store.read_excluded_company_list()
    )

    doc_keys = {company_list_key(name) for name in doc_names}
    excluded_by_key = {
        company_list_key(name): name
        for name in excluded_names
        if company_list_key(name)
    }

    doc_modified_at = google_modified_time(file_metadata)
    local_modified_at = local_company_list_modified_time()
    should_capture_removals = (
        pd.notna(doc_modified_at)
        and (
            pd.isna(local_modified_at)
            or doc_modified_at > local_modified_at
        )
    )

    if should_capture_removals:
        for name in local_names:
            key = company_list_key(name)
            if key and key not in doc_keys:
                excluded_by_key[key] = name
    elif local_names:
        print(
            "Skipped removal detection because the local company cache is "
            "newer than the Google Sheet."
        )

    # If a user manually adds a company back to the doc, allow enrichment to keep it.
    for key in list(excluded_by_key):
        if key in doc_keys:
            excluded_by_key.pop(key)

    updated_excluded_names = normalize_company_list(excluded_by_key.values())
    if updated_excluded_names != excluded_names:
        company_list_store.write_excluded_company_list(updated_excluded_names)
        print(
            "Updated manual company exclusions: "
            f"{len(updated_excluded_names)} companies."
        )

    return updated_excluded_names


def load_company_names(credentials=None):
    names = []

    if credentials is not None:
        try:
            names, file_metadata = company_list_store.read_company_list_sheet(credentials)
            normalized_names = normalize_company_list(names)
            update_excluded_company_names(
                normalized_names,
                company_list_store.read_local_company_list(),
                file_metadata,
            )
            company_list_store.write_local_company_list(normalized_names)
            print(
                "Using company list from Google Sheet: "
                f"{file_metadata.get('webViewLink', file_metadata.get('id'))}"
            )
            return normalized_names
        except Exception as error:
            print(
                "Could not read Google Sheet company_list; falling back to "
                f"{COMPANY_NAMES_FILE}: {error}"
            )

    names = company_list_store.read_local_company_list()

    return normalize_company_list(names)


def run_ga4_report(client, request, label):
    rows = []
    offset = 0

    while True:
        request.offset = offset
        request.limit = GA4_PAGE_LIMIT

        for attempt in range(1, GA4_MAX_RETRIES + 1):
            try:
                response = client.run_report(
                    request,
                    timeout=GA4_REQUEST_TIMEOUT_SECONDS,
                )
                break
            except (
                google_api_exceptions.DeadlineExceeded,
                google_api_exceptions.ServiceUnavailable,
                google_api_exceptions.InternalServerError,
            ) as error:
                if attempt == GA4_MAX_RETRIES:
                    raise
                wait_seconds = GA4_RETRY_DELAY_SECONDS * attempt
                print(
                    f"GA4 {label} timed out/failed at offset {offset}; "
                    f"retrying in {wait_seconds}s "
                    f"({attempt}/{GA4_MAX_RETRIES}): {error}"
                )
                time.sleep(wait_seconds)

        if not response.rows:
            break

        rows.extend(response.rows)
        if len(response.rows) < GA4_PAGE_LIMIT:
            break

        offset += len(response.rows)
        print(f"Loaded {len(rows)} GA4 {label} rows...")

    return rows


def get_ga4_author_dimension(client):
    try:
        metadata = client.get_metadata(name=f"properties/{PROPERTY_ID}/metadata")
    except Exception as error:
        print(f"Could not load GA4 metadata for author dimension lookup: {error}")
        return None

    dimensions = list(metadata.dimensions)
    by_api_name = {dimension.api_name: dimension for dimension in dimensions}
    for candidate in GA4_AUTHOR_DIMENSION_CANDIDATES:
        if candidate in by_api_name:
            print(f"Using GA4 author dimension: {candidate}")
            return candidate

    author_like = []
    for dimension in dimensions:
        api_name = str(dimension.api_name or "")
        ui_name = str(dimension.ui_name or "")
        description = str(dimension.description or "")
        searchable_text = " ".join([api_name, ui_name, description]).casefold()
        if any(term in searchable_text for term in ["author", "byline", "writer"]):
            author_like.append(dimension)

    if author_like:
        author_like = sorted(
            author_like,
            key=lambda dimension: (
                "author" not in str(dimension.api_name).casefold(),
                str(dimension.api_name).casefold(),
            ),
        )
        selected_dimension = author_like[0].api_name
        print(f"Using GA4 author-like dimension: {selected_dimension}")
        return selected_dimension

    print("No GA4 author/byline dimension found; author will fall back to sheet/scrape data.")
    return None


def get_ga4_author_by_url(client, author_dimension, article_filter, start_date, end_date):
    if not author_dimension:
        return {}

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="fullPageUrl"),
            Dimension(name=author_dimension),
        ],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=article_filter,
        order_bys=[
            OrderBy(
                metric=OrderBy.MetricOrderBy(metric_name="eventCount"),
                desc=True,
            )
        ],
    )
    response_rows = run_ga4_report(client, request, "article author lookup")
    author_values_by_url = {}
    for row in response_rows:
        url = clean_url(row.dimension_values[0].value)
        author = clean_text(row.dimension_values[1].value)
        if not url or not author or author == "(not set)":
            continue
        author_values_by_url.setdefault(url, []).append(author)

    return {
        url: first_non_blank(authors)
        for url, authors in author_values_by_url.items()
    }


def get_ga4_article_metrics(credentials, start_date, end_date):
    client = BetaAnalyticsDataClient(credentials=credentials)
    author_dimension = get_ga4_author_dimension(client)
    dimensions = [
        Dimension(name="pageTitle"),
        Dimension(name="fullPageUrl"),
    ]

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
        dimensions=dimensions,
        metrics=[
            Metric(name="screenPageViews"),
            Metric(name="totalUsers"),
            Metric(name="newUsers"),
            Metric(name="sessions"),
            Metric(name="bounceRate"),
            Metric(name="averageSessionDuration"),
            Metric(name="userEngagementDuration"),
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
    )

    response_rows = run_ga4_report(client, request, "article metrics")
    author_by_url = get_ga4_author_by_url(
        client,
        author_dimension,
        article_filter,
        start_date,
        end_date,
    )

    rows = []
    for row in response_rows:
        full_page_url = row.dimension_values[1].value
        url = clean_url(full_page_url)
        rows.append({
            "ga4_title": row.dimension_values[0].value,
            "full_page_url": full_page_url,
            "url": url,
            "author": author_by_url.get(url, ""),
            "views": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
            "new_users": int(row.metric_values[2].value),
            "sessions": int(row.metric_values[3].value),
            "bounce_rate": float(row.metric_values[4].value),
            "avg_time_on_page": float(row.metric_values[5].value),
            "total_user_engagement_seconds": float(row.metric_values[6].value),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["bounce_rate"] = normalize_bounce_rate(df["bounce_rate"])

    grouped_rows = []
    for url, group in df.groupby("url", dropna=True):
        top_row = group.sort_values("views", ascending=False).iloc[0]
        sessions = top_row["sessions"]
        total_engagement_seconds = top_row["total_user_engagement_seconds"]
        users = top_row["users"]
        new_users = top_row["new_users"]

        grouped_rows.append({
            "url": url,
            "ga4_full_page_url": top_row["full_page_url"],
            "ga4_title": top_row["ga4_title"],
            "author": first_non_blank(group["author"])
            if "author" in group.columns
            else "",
            "views": top_row["views"],
            "users": users,
            "new_users": new_users,
            "returning_users_estimated": estimated_returning_users(users, new_users),
            "sessions": sessions,
            "bounce_rate": top_row["bounce_rate"],
            "avg_time_on_page": top_row["avg_time_on_page"],
            "total_user_engagement_seconds": total_engagement_seconds,
            "average_engagement_time_seconds": (
                total_engagement_seconds / sessions
                if sessions > 0
                else np.nan
            ),
        })

    return pd.DataFrame(grouped_rows)


def get_ga4_article_metrics_by_date(credentials, start_date, end_date):
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

    dimensions = ["date", "pageTitle", "fullPageUrl"]
    metrics = [
        "screenPageViews",
        "totalUsers",
        "newUsers",
        "sessions",
        "bounceRate",
        "averageSessionDuration",
        "userEngagementDuration",
    ]
    rows = []

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name=dimension) for dimension in dimensions],
        metrics=[Metric(name=metric) for metric in metrics],
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimension_filter=article_filter,
    )
    response_rows = run_ga4_report(client, request, "daily article metrics")

    for row in response_rows:
        full_page_url = row.dimension_values[2].value
        rows.append({
            "date": row.dimension_values[0].value,
            "ga4_title": row.dimension_values[1].value,
            "full_page_url": full_page_url,
            "url": clean_url(full_page_url),
            "views": int(row.metric_values[0].value),
            "users": int(row.metric_values[1].value),
            "new_users": int(row.metric_values[2].value),
            "sessions": int(row.metric_values[3].value),
            "bounce_rate": float(row.metric_values[4].value),
            "avg_time_on_page": float(row.metric_values[5].value),
            "total_user_engagement_seconds": float(row.metric_values[6].value),
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["bounce_rate"] = normalize_bounce_rate(df["bounce_rate"])
    grouped_rows = []
    for (date_value, url), group in df.groupby(["date", "url"], dropna=True):
        top_row = group.sort_values("views", ascending=False).iloc[0]
        sessions = top_row["sessions"]
        total_engagement_seconds = top_row["total_user_engagement_seconds"]
        users = top_row["users"]
        new_users = top_row["new_users"]
        grouped_rows.append({
            "date": pd.to_datetime(date_value, format="%Y%m%d", errors="coerce"),
            "url": url,
            "ga4_full_page_url": top_row["full_page_url"],
            "ga4_title": top_row["ga4_title"],
            "views": top_row["views"],
            "users": users,
            "new_users": new_users,
            "returning_users_estimated": estimated_returning_users(users, new_users),
            "sessions": sessions,
            "bounce_rate": top_row["bounce_rate"],
            "avg_time_on_page": top_row["avg_time_on_page"],
            "total_user_engagement_seconds": total_engagement_seconds,
            "average_engagement_time_seconds": (
                total_engagement_seconds / sessions
                if sessions > 0
                else np.nan
            ),
        })

    daily_df = pd.DataFrame(grouped_rows)
    return daily_df.sort_values(["date", "views"], ascending=[True, False])


# =========================
# ARTICLE TEXT
# =========================

def get_soup(url, max_retries=MAX_SCRAPE_RETRIES, base_delay=10):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=20)

            if response.status_code == 429:
                wait_time = base_delay * (attempt + 1)
                print(f"Rate limited on {url}. Waiting {wait_time} seconds...")
                if attempt + 1 < max_retries:
                    time.sleep(wait_time)
                continue

            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")

        except Exception as error:
            print(f"Request error on attempt {attempt + 1} for {url}: {error}")
            if attempt + 1 < max_retries:
                time.sleep(base_delay)

    return None


def first_meta_content(soup, selectors):
    for selector in selectors:
        tag = soup.select_one(selector)
        if not tag:
            continue
        content = clean_text(tag.get("content"))
        if content:
            return content
    return ""


def extract_author_from_json_ld(soup):
    for script in soup.find_all("script", type="application/ld+json"):
        raw_json = script.string or script.get_text()
        if not raw_json:
            continue

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue

            author = item.get("author")
            if isinstance(author, str):
                author = clean_author_name(author)
                if author:
                    return author
            if isinstance(author, dict):
                author = clean_author_name(author.get("name"))
                if author:
                    return author
            if isinstance(author, list):
                names = [
                    clean_author_name(author_item.get("name"))
                    for author_item in author
                    if isinstance(author_item, dict)
                ]
                names = [name for name in names if name]
                if names:
                    return ", ".join(names)

            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

    return ""


def extract_article_author(soup):
    author = first_meta_content(
        soup,
        [
            'meta[name="author"]',
            'meta[property="article:author"]',
            'meta[name="parsely-author"]',
            'meta[property="og:article:author"]',
        ],
    )
    if author:
        return clean_author_name(author)

    author = extract_author_from_json_ld(soup)
    if author:
        return author

    for selector in [
        '[itemprop="author"]',
        'a[rel="author"]',
        '[class*="byline"]',
        '[class*="author"]',
    ]:
        tag = soup.select_one(selector)
        if not tag:
            continue
        author = clean_author_name(tag.get_text(" ", strip=True))
        if author and len(author.split()) <= 8:
            return author

    return ""


def extract_article_section_from_page(soup):
    section = first_meta_content(
        soup,
        [
            'meta[property="article:section"]',
            'meta[name="section"]',
            'meta[name="parsely-section"]',
        ],
    )
    return clean_text(section)


def extract_article_text(url):
    soup = get_soup(url)

    if soup is None:
        return {
            "url": url,
            "article_text": "",
            "published_date": pd.NaT,
            "scrape_status": "failed_request",
            "scraped_author": "",
            "scraped_section": "",
            "metadata_scrape_status": "failed_request",
        }

    scraped_author = extract_article_author(soup)
    scraped_section = extract_article_section_from_page(soup)

    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    article = soup.find("article")
    paragraphs = article.find_all("p") if article else soup.find_all("p")

    paragraph_texts = [
        p.get_text(" ", strip=True)
        for p in paragraphs
        if p.get_text(" ", strip=True)
    ]

    article_text = clean_text(" ".join(paragraph_texts))
    word_count = len(article_text.split())
    published_date = extract_published_date(soup)

    return {
        "url": url,
        "article_text": article_text,
        "published_date": published_date,
        "scrape_status": "success" if word_count >= 150 else "probably_not_article",
        "scraped_author": scraped_author,
        "scraped_section": scraped_section,
        "metadata_scrape_status": "success" if scraped_author else "no_author_found",
    }


def load_article_text_cache():
    if not os.path.exists(ARTICLE_TEXT_CACHE_FILE):
        return pd.DataFrame(columns=ARTICLE_TEXT_CACHE_COLUMNS)

    cache_df = pd.read_csv(ARTICLE_TEXT_CACHE_FILE)
    if "url" not in cache_df.columns:
        return pd.DataFrame(columns=ARTICLE_TEXT_CACHE_COLUMNS)

    cache_df["url"] = cache_df["url"].map(clean_url)
    for column in ARTICLE_TEXT_CACHE_COLUMNS:
        if column not in cache_df.columns:
            cache_df[column] = ""

    if "published_date" not in cache_df.columns:
        cache_df["published_date"] = pd.NaT
    else:
        cache_df["published_date"] = pd.to_datetime(
            cache_df["published_date"],
            errors="coerce",
        )
    cache_df = cache_df.dropna(subset=["url"]).drop_duplicates("url", keep="last")
    return cache_df[ARTICLE_TEXT_CACHE_COLUMNS]


def has_cached_article_text(cache_row):
    if cache_row.empty:
        return False

    row = cache_row.iloc[-1]
    scrape_status = str(row.get("scrape_status", "")).strip().lower()
    article_text = str(row.get("article_text", "")).strip()

    if scrape_status in {"success", "probably_not_article"}:
        return True

    if scrape_status == "failed_request":
        return False

    return bool(article_text)


def has_checked_article_metadata(cache_row):
    if cache_row.empty:
        return False

    row = cache_row.iloc[-1]
    metadata_status = str(row.get("metadata_scrape_status", "")).strip().lower()
    scraped_author = str(row.get("scraped_author", "")).strip()

    return bool(scraped_author) or metadata_status in {
        "success",
        "no_author_found",
        "failed_request",
    }


def collect_article_texts(article_urls, scrape_missing=True):
    cache_df = load_article_text_cache()
    cached_urls = set(cache_df["url"].dropna())
    urls_to_scrape = [
        url for url in article_urls
        if url
        and (
            url not in cached_urls
            or not has_cached_article_text(cache_df.loc[cache_df["url"] == url])
        )
    ]

    print(f"Article text cache rows: {len(cache_df)}")
    print(f"New article texts to scrape: {len(urls_to_scrape)}")

    if not scrape_missing:
        print("Skipping article text scraping; using existing cache/sheet data only.")
        return cache_df

    new_rows = []
    for index, url in enumerate(urls_to_scrape, start=1):
        print(f"Scraping article text {index}/{len(urls_to_scrape)}: {url}")
        new_rows.append(extract_article_text(url))
        time.sleep(DELAY_SECONDS)

    if new_rows:
        cache_df = pd.concat(
            [cache_df, pd.DataFrame(new_rows)],
            ignore_index=True,
        ).drop_duplicates("url", keep="last")
        for column in ARTICLE_TEXT_CACHE_COLUMNS:
            if column not in cache_df.columns:
                cache_df[column] = ""
        cache_df = cache_df[ARTICLE_TEXT_CACHE_COLUMNS]
        cache_df.to_csv(ARTICLE_TEXT_CACHE_FILE, index=False)

    return cache_df


def should_scrape_missing_content():
    choice = input(
        "Scrape missing article data before pulling GA4? "
        "[Y = scrape + GA4, N = GA4/cache only] (default Y): "
    ).strip().lower()
    return choice not in {"n", "no", "g", "ga4", "ga4 only", "cache"}


def load_published_datetime_cache():
    if not os.path.exists(PUBLISHED_DATETIME_CACHE_FILE):
        return pd.DataFrame(columns=["url", "published_date"])

    cache_df = pd.read_csv(PUBLISHED_DATETIME_CACHE_FILE)
    if "url" not in cache_df.columns:
        return pd.DataFrame(columns=["url", "published_date"])

    cache_df["url"] = cache_df["url"].map(clean_url)

    date_column = None
    if "published_datetime" in cache_df.columns:
        date_column = "published_datetime"
    elif "published_date" in cache_df.columns:
        date_column = "published_date"

    if date_column is None:
        return pd.DataFrame(columns=["url", "published_date"])

    cache_df["published_date"] = pd.to_datetime(
        cache_df[date_column],
        errors="coerce",
    ).dt.normalize()

    return (
        cache_df[["url", "published_date"]]
        .dropna(subset=["url"])
        .drop_duplicates("url", keep="last")
    )


def build_published_date_updates(enriched_df, text_df):
    date_frames = []

    if "published_date" in enriched_df.columns:
        date_frames.append(enriched_df[["url", "published_date"]])

    if "published_date" in text_df.columns:
        date_frames.append(text_df[["url", "published_date"]])

    published_cache_df = load_published_datetime_cache()
    if not published_cache_df.empty:
        date_frames.append(published_cache_df)

    if not date_frames:
        return pd.DataFrame(columns=["url", "published_date"])

    combined_df = pd.concat(date_frames, ignore_index=True)
    combined_df["url"] = combined_df["url"].map(clean_url)
    combined_df["published_date"] = pd.to_datetime(
        combined_df["published_date"],
        errors="coerce",
    ).dt.normalize()

    combined_df = combined_df.dropna(subset=["url", "published_date"])
    if combined_df.empty:
        return pd.DataFrame(columns=["url", "published_date"])

    return combined_df.drop_duplicates("url", keep="last")


# =========================
# ANALYSIS
# =========================

def add_keyword_segment_columns(article_df, keyword_segments):
    result_df = article_df.copy()

    for segment_name, keywords in keyword_segments.items():
        match_column = f"segment_{segment_name}"
        count_column = f"segment_{segment_name}_keyword_count"

        patterns = [keyword_pattern(keyword) for keyword in keywords]
        result_df[count_column] = result_df["article_text"].map(
            lambda text: sum(
                len(pattern.findall(str(text).lower()))
                for pattern in patterns
            )
        )
        result_df[match_column] = result_df[count_column] > 0

    return result_df


COMPANY_SUFFIX_PATTERN = re.compile(
    r"\b([A-Z][A-Za-z0-9&.,'-]*(?:\s+[A-Z][A-Za-z0-9&.,'-]*){0,5}\s+"
    r"(?:Inc\.?|Corp\.?|Corporation|Company|Co\.?|LLC|Ltd\.?|Limited|"
    r"Group|Systems|Technologies|Technology|Manufacturing|Automation|"
    r"Robotics|Aerospace|Motors|Motor|Electric|Industries|Industrial))\b"
)

ORG_STOP_NAMES = {
    "Advanced Manufacturing",
    "Manufacturing News",
    "Manufacturing Engineering",
    "News Desk",
    "United States",
    "North America",
    "South America",
    "European Union",
}

GENERIC_COMPANY_NAMES = {
    "A Manufacturing",
    "Additive Manufacturing",
    "Additive Manufacturing User Group",
    "Additive Manufacturing Users Group",
    "Advanced Manufacturing",
    "Advanced Manufacturing Technology",
    "Advanced Manufacturing Technologies",
    "Advanced Manufacturing Systems",
    "AeroDef Manufacturing",
    "Aerospace Manufacturing",
    "American Manufacturing",
    "AM Start-Up Technology",
    "An Industrial",
    "Apple Manufacturing",
    "Association For Manufacturing Technology",
    "Automotive Automation",
    "Automotive Manufacturing",
    "Bioindustrial Manufacturing",
    "Build Manufacturing",
    "Build Your Automation",
    "Boeing Additive Manufacturing",
    "Canadian Manufacturing Technology",
    "Carbon Fiber Technology",
    "Celebrating Manufacturing",
    "Certified Manufacturing",
    "Chief Manufacturing",
    "Chief Technology",
    "Clean Energy Smart Manufacturing",
    "Composite Manufacturing",
    "Composites Manufacturing",
    "Consumer Technology",
    "Cybersecurity Manufacturing",
    "Defense, Manufacturing Technology",
    "Defense Industrial",
    "Defense Manufacturing",
    "Detroit Manufacturing",
    "Digital Manufacturing",
    "Dow Jones Industrial",
    "Drawing Automation",
    "Educational Robotics",
    "Electronics Manufacturing",
    "Emerging Technologies",
    "Emerging Technology",
    "Engineering Technology",
    "Future Manufacturing",
    "Future-Ready Manufacturing",
    "Get Smart Manufacturing",
    "Grinding Technology",
    "Global Industrial",
    "Global Robotics",
    "Harbour IQ Manufacturing",
    "Hybrid Manufacturing",
    "Improve Manufacturing",
    "Industrial Automation",
    "Industrial Manufacturing",
    "Industrial Motor",
    "Information Technology",
    "Innovative Food Technology",
    "Innovative Technology",
    "Integrated Systems",
    "Intelligent Manufacturing",
    "International Manufacturing Technology",
    "ISM Manufacturing",
    "Laser Technology",
    "Lean Manufacturing",
    "Manufacturing",
    "Manufacturing Automation",
    "Manufacturing Corp",
    "Manufacturing Corporation",
    "Manufacturing Inc",
    "Manufacturing Industries",
    "Manufacturing Propulsion Technology",
    "Manufacturing Systems",
    "Manufacturing Technologies",
    "Manufacturing Technology",
    "Manufacturing Technology Deployment",
    "Manufacturing Technology Deployment Group",
    "Manufacturing Technology's U.S. Manufacturing Technology",
    "Maritime Industrial",
    "Michigan Manufacturing",
    "Michigan Maritime Manufacturing",
    "Mobile Industrial",
    "National Additive Manufacturing",
    "National Defense Industrial",
    "National Manufacturing",
    "New Manufacturing",
    "Nonfederal Systems",
    "North American Manufacturing",
    "October Manufacturing",
    "Ohio Manufacturing",
    "Oregon Manufacturing",
    "Outstanding Young Manufacturing",
    "Partnership Response In Manufacturing",
    "Product Manufacturing",
    "Private Company",
    "Robotics Manufacturing",
    "Sandra L. Bouckley Outstanding Young Manufacturing",
    "Science, Technology",
    "Secure Manufacturing",
    "Semiconductor Manufacturing",
    "Siemens Industrial",
    "Systems",
    "Smart Manufacturing",
    "Smart Manufacturing Systems",
    "Space Systems",
    "Submarine Industrial",
    "Sustainable Automation",
    "Sustainable Manufacturing",
    "Systems Corp",
    "Systems Inc",
    "The Additive Manufacturing",
    "The Advanced Manufacturing",
    "The Chief Manufacturing",
    "The Company",
    "The Hollings Manufacturing",
    "The International Manufacturing Technology",
    "The ISM Manufacturing",
    "The Manufacturing",
    "The October Manufacturing",
    "The Smart Manufacturing",
    "Transportation Technology",
    "U.S. Defense Industrial",
    "U.S. Manufacturing",
    "U.S. Manufacturing Technology",
    "Unmanned Aerial Systems",
    "Welding Automation",
    "Wire Arc Additive Manufacturing",
    "Working Group",
    "World Manufacturing",
    "World Military Unmanned Aerial Vehicle Systems",
    "Adopting Automation",
    "Advanced Technology",
    "Company",
    "Fourth Industrial",
    "Hollings Manufacturing",
    "Joint Additive Manufacturing Working Group",
    "March Manufacturing",
    "Measurement Systems",
    "Robot Systems",
    "Scale Industrial",
    "U.S. Advanced Manufacturing",
}

COMPANY_ALIASES = {
    "boeing co": "Boeing",
    "boeing company": "Boeing",
    "the boeing": "Boeing",
    "general motors co": "General Motors",
    "general motors corp": "General Motors",
    "meld manufacturing": "Meld Manufacturing",
    "northrop grumman aerospace systems": "Northrop Grumman",
    "northrop grumman systems": "Northrop Grumman",
    "zeiss industrial": "Zeiss Industrial Quality Solutions",
}

GENERIC_COMPANY_KEYWORDS = {
    "additive manufacturing",
    "advanced manufacturing",
    "aerospace manufacturing",
    "automotive manufacturing",
    "digital manufacturing",
    "future manufacturing",
    "industrial manufacturing",
    "lean manufacturing",
    "smart manufacturing",
    "sustainable manufacturing",
    "u.s. manufacturing",
}

LEGAL_SUFFIX_PATTERN = re.compile(
    r"\s+(?:Inc\.?|Corp\.?|Corporation|Company|Co\.?|LLC|Ltd\.?|Limited)$",
    re.IGNORECASE,
)
LOCATION_PREFIX_PATTERN = re.compile(
    r"^(?:[A-Z][A-Za-z. ]+,\s*)?[A-Z][A-Za-z. ]+-based\s+",
)


def normalize_company_name(name):
    name = clean_text(name)
    name = re.sub(r"^(?:At|CEO,)\s+", "", name, flags=re.IGNORECASE)
    name = LOCATION_PREFIX_PATTERN.sub("", name)
    name = re.sub(r"^[Tt]he\s+", "", name)
    name = re.sub(r"[.,;:]+$", "", name)
    return name


def canonical_company_name(name):
    name = normalize_company_name(name)
    name = re.sub(r"\s+", " ", name).strip()
    alias = COMPANY_ALIASES.get(company_filter_key(name))
    if alias:
        return alias
    previous = None
    while previous != name:
        previous = name
        name = LEGAL_SUFFIX_PATTERN.sub("", name).strip()
        name = re.sub(r"[.,;:]+$", "", name).strip()
    alias = COMPANY_ALIASES.get(company_filter_key(name))
    if alias:
        return alias
    return name


def company_filter_key(name):
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def is_generic_company_name(name):
    name = normalize_company_name(name)
    canonical = canonical_company_name(name)
    if not canonical or len(canonical) < 3:
        return True

    filter_values = {
        company_filter_key(name),
        company_filter_key(canonical),
    }
    generic_values = {company_filter_key(value) for value in GENERIC_COMPANY_NAMES}
    stop_values = {company_filter_key(value) for value in ORG_STOP_NAMES}
    if filter_values & (generic_values | stop_values):
        return True

    lowered = company_filter_key(canonical)
    if lowered in GENERIC_COMPANY_KEYWORDS:
        return True

    words = lowered.split()
    if len(words) <= 3 and any(keyword in lowered for keyword in GENERIC_COMPANY_KEYWORDS):
        return True

    return False


def extract_company_mentions(text, configured_companies=None, infer_unconfigured=False):
    text = str(text or "")
    mentions = Counter()
    occupied_spans = []

    configured_names = sorted(
        configured_companies or [],
        key=lambda name: len(str(name)),
        reverse=True,
    )

    for company in configured_names:
        pattern = keyword_pattern(company)
        count = 0
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied_spans):
                continue
            count += 1
            occupied_spans.append(span)

        if count:
            canonical = canonical_company_name(company)
            if canonical and not is_generic_company_name(canonical):
                mentions[canonical] += count

    if infer_unconfigured:
        for match in COMPANY_SUFFIX_PATTERN.findall(text):
            company = canonical_company_name(match)
            if company and not is_generic_company_name(company):
                mentions[company] += 1

    return mentions


def article_metric_row(row):
    return {
        "article_title": row.get("article_title", ""),
        "url": row.get("url", ""),
        "ga4_full_page_url": row.get("ga4_full_page_url", ""),
        "published_date": row.get("published_date", pd.NaT),
        "views": row.get("views", np.nan),
        "users": row.get("users", np.nan),
        "new_users": row.get("new_users", np.nan),
        "returning_users_estimated": row.get("returning_users_estimated", np.nan),
        "sessions": row.get("sessions", np.nan),
        "bounce_rate": row.get("bounce_rate", np.nan),
        "avg_time_on_page": row.get("avg_time_on_page", np.nan),
        "average_engagement_time_seconds": row.get(
            "average_engagement_time_seconds",
            np.nan,
        ),
    }


def first_non_blank(values):
    for value in values:
        if pd.notna(value) and str(value).strip():
            return value
    return ""


def first_existing_column(columns, candidates):
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def longest_text(values):
    texts = [str(value) for value in values if pd.notna(value) and str(value).strip()]
    if not texts:
        return ""
    return max(texts, key=len)


def combined_scrape_status(group):
    statuses = []
    for column in ["scrape_status", "scrape_status_y", "scrape_status_x"]:
        if column in group.columns:
            statuses.extend(
                str(value).strip()
                for value in group[column]
                if pd.notna(value) and str(value).strip()
            )

    if not statuses:
        return ""

    for preferred_status in ["success", "probably_not_article", "failed_request"]:
        if preferred_status in statuses:
            return preferred_status

    return statuses[0]


def aggregate_articles_by_title(article_df, keyword_segments):
    if article_df.empty or "article_title" not in article_df.columns:
        return article_df

    grouped_rows = []
    segment_columns = []
    for segment_name in keyword_segments:
        segment_columns.extend([
            f"segment_{segment_name}",
            f"segment_{segment_name}_keyword_count",
        ])

    for article_title, group in article_df.groupby("article_title", dropna=False):
        sessions = group["sessions"] if "sessions" in group.columns else pd.Series(dtype=float)
        views = group["views"] if "views" in group.columns else pd.Series(dtype=float)
        total_engagement_seconds = (
            group["total_user_engagement_seconds"].sum()
            if "total_user_engagement_seconds" in group.columns
            else np.nan
        )
        unique_urls = sorted(set(group["url"].dropna()))
        top_row = group.sort_values("views", ascending=False).iloc[0]
        users = median_numeric(group["users"]) if "users" in group.columns else 0
        new_users = (
            median_numeric(group["new_users"])
            if "new_users" in group.columns
            else 0
        )

        row = {
            "article_title": article_title,
            "url": " | ".join(unique_urls),
            "primary_url": top_row["url"],
            "ga4_full_page_url": top_row.get("ga4_full_page_url", ""),
            "url_count": len(unique_urls),
            "ga4_title": first_non_blank(group.get("ga4_title", pd.Series(dtype=str))),
            "views": views.sum(),
            "users": users,
            "new_users": new_users,
            "returning_users_estimated": estimated_returning_users(users, new_users),
            "sessions": sessions.sum(),
            "bounce_rate": weighted_average(group["bounce_rate"], sessions)
            if "bounce_rate" in group.columns
            else np.nan,
            "avg_time_on_page": weighted_average(group["avg_time_on_page"], sessions)
            if "avg_time_on_page" in group.columns
            else np.nan,
            "total_user_engagement_seconds": total_engagement_seconds,
            "average_engagement_time_seconds": (
                total_engagement_seconds / sessions.sum()
                if sessions.sum() > 0 and pd.notna(total_engagement_seconds)
                else np.nan
            ),
            "title_word_count": top_row.get("title_word_count", np.nan),
            "title_character_count": top_row.get("title_character_count", np.nan),
            "word_count": group["word_count"].dropna().max()
            if "word_count" in group.columns and not group["word_count"].dropna().empty
            else np.nan,
            "character_count": group["character_count"].dropna().max()
            if "character_count" in group.columns and not group["character_count"].dropna().empty
            else np.nan,
            "article_text": longest_text(group["article_text"])
            if "article_text" in group.columns
            else "",
            "published_date": group["published_date"].dropna().min()
            if "published_date" in group.columns and not group["published_date"].dropna().empty
            else pd.NaT,
        }
        row["section_tags"] = ", ".join(extract_section_tags(row["url"]))
        row["primary_section_tag"] = (
            extract_section_tag(top_row["url"]) if pd.notna(top_row["url"]) else ""
        )
        row["author"] = (
            first_non_blank(group["author"]) if "author" in group.columns else ""
        )
        row["section"] = (
            first_non_blank(group["section"]) if "section" in group.columns else ""
        )
        if not row["section"]:
            row["section"] = row["primary_section_tag"]

        row["analysis_word_count"] = len(str(row["article_text"]).split())

        row["scrape_status"] = combined_scrape_status(group)

        for column in segment_columns:
            if column not in group.columns:
                continue
            if column.startswith("segment_") and column.endswith("_keyword_count"):
                row[column] = group[column].max()
            else:
                row[column] = group[column].fillna(False).astype(bool).any()

        grouped_rows.append(row)

    return pd.DataFrame(grouped_rows).sort_values("views", ascending=False)


def build_article_analysis(
    enriched_df,
    ga4_df,
    text_df,
    keyword_segments,
    start_date,
    end_date,
    selected_active_urls=None,
    published_date_df=None,
):
    start_dt = resolve_ga4_date(start_date)
    end_dt = resolve_ga4_date(end_date)
    title_column = None
    if "article_title" in enriched_df.columns:
        title_column = "article_title"
    elif "title" in enriched_df.columns:
        title_column = "title"
    sheet_columns = ["url"]
    author_column = first_existing_column(enriched_df.columns, AUTHOR_SOURCE_COLUMNS)
    section_column = first_existing_column(enriched_df.columns, SECTION_SOURCE_COLUMNS)

    if title_column:
        sheet_columns.insert(0, title_column)

    for optional_column in ["word_count", "character_count", "scrape_status"]:
        if optional_column in enriched_df.columns:
            sheet_columns.append(optional_column)

    for optional_column in [author_column, section_column]:
        if optional_column and optional_column not in sheet_columns:
            sheet_columns.append(optional_column)

    base_df = enriched_df[sheet_columns].copy()
    rename_columns = {}
    if author_column and author_column != "author":
        rename_columns[author_column] = "author"
    if section_column and section_column != "section":
        rename_columns[section_column] = "section"
    if rename_columns:
        base_df = base_df.rename(columns=rename_columns)
    article_df = ga4_df.merge(base_df, on="url", how="outer")
    article_df = fill_blank_from_column(article_df, "author", "author_x")
    article_df = fill_blank_from_column(article_df, "author", "author_y")
    article_df = drop_helper_columns(article_df, ["author_x", "author_y"])
    article_df = article_df.merge(text_df, on="url", how="left")
    article_df = fill_blank_from_column(article_df, "author", "scraped_author")
    article_df = fill_blank_from_column(article_df, "section", "scraped_section")

    if published_date_df is not None and not published_date_df.empty:
        article_df = article_df.merge(
            published_date_df,
            on="url",
            how="left",
            suffixes=("", "_updated"),
        )
        if "published_date_updated" in article_df.columns:
            article_df["published_date"] = article_df[
                "published_date_updated"
            ].combine_first(article_df["published_date"])
            article_df = article_df.drop(columns=["published_date_updated"])

    if selected_active_urls is None:
        selected_active_urls = set(ga4_df["url"].dropna())
    else:
        selected_active_urls = set(selected_active_urls)

    article_df["article_text"] = article_df["article_text"].fillna("")
    article_df["published_date"] = pd.to_datetime(
        article_df["published_date"],
        errors="coerce",
    )
    published_in_range = (
        (article_df["published_date"] >= start_dt)
        & (article_df["published_date"] <= end_dt)
    )

    for metric_column in [
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "avg_time_on_page",
        "total_user_engagement_seconds",
        "average_engagement_time_seconds",
    ]:
        if metric_column in article_df.columns:
            article_df[metric_column] = pd.to_numeric(
                article_df[metric_column],
                errors="coerce",
            ).fillna(0)

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

    article_df["title_word_count"] = article_df["article_title"].map(
        lambda title: len(str(title).split())
    )
    article_df["title_character_count"] = article_df["article_title"].map(
        lambda title: len(str(title))
    )
    article_df["analysis_word_count"] = article_df["article_text"].map(
        lambda text: len(str(text).split())
    )

    if "word_count" in article_df.columns:
        article_df = article_df[
            article_df["word_count"].isna()
            | (article_df["word_count"] <= MAX_WORD_COUNT)
        ]
    else:
        article_df = article_df[
            article_df["analysis_word_count"] <= MAX_WORD_COUNT
        ]

    article_df = add_keyword_segment_columns(article_df, keyword_segments)
    article_df = aggregate_articles_by_title(article_df, keyword_segments)
    return article_df.sort_values("views", ascending=False)


def build_segment_summary(article_df, keyword_segments):
    rows = []

    for segment_name in keyword_segments:
        match_column = f"segment_{segment_name}"
        count_column = f"segment_{segment_name}_keyword_count"

        if match_column not in article_df.columns:
            continue

        matched_df = article_df[article_df[match_column]].copy()
        unmatched_df = article_df[~article_df[match_column]].copy()

        rows.append({
            "segment": segment_name,
            "keywords": ", ".join(keyword_segments[segment_name]),
            "matched_articles": len(matched_df),
            "matched_total_views": matched_df["views"].sum(),
            "matched_avg_views": matched_df["views"].mean(),
            "matched_median_views": matched_df["views"].median(),
            "matched_total_new_users": matched_df["new_users"].sum(),
            "matched_avg_new_users": matched_df["new_users"].mean(),
            "matched_median_new_users": matched_df["new_users"].median(),
            "matched_total_returning_users": matched_df[
                "returning_users_estimated"
            ].sum(),
            "matched_avg_returning_users": matched_df[
                "returning_users_estimated"
            ].mean(),
            "matched_median_returning_users": matched_df[
                "returning_users_estimated"
            ].median(),
            "matched_avg_bounce_rate": matched_df["bounce_rate"].mean(),
            "matched_median_bounce_rate": matched_df["bounce_rate"].median(),
            "matched_avg_time_on_page": matched_df["avg_time_on_page"].mean(),
            "matched_median_time_on_page": matched_df["avg_time_on_page"].median(),
            "matched_avg_engagement_time_seconds": matched_df[
                "average_engagement_time_seconds"
            ].mean(),
            "matched_median_engagement_time_seconds": matched_df[
                "average_engagement_time_seconds"
            ].median(),
            "unmatched_articles": len(unmatched_df),
            "unmatched_avg_views": unmatched_df["views"].mean(),
            "unmatched_median_views": unmatched_df["views"].median(),
            "unmatched_total_new_users": unmatched_df["new_users"].sum(),
            "unmatched_avg_new_users": unmatched_df["new_users"].mean(),
            "unmatched_median_new_users": unmatched_df["new_users"].median(),
            "unmatched_total_returning_users": unmatched_df[
                "returning_users_estimated"
            ].sum(),
            "unmatched_avg_returning_users": unmatched_df[
                "returning_users_estimated"
            ].mean(),
            "unmatched_median_returning_users": unmatched_df[
                "returning_users_estimated"
            ].median(),
            "unmatched_avg_bounce_rate": unmatched_df["bounce_rate"].mean(),
            "unmatched_median_bounce_rate": unmatched_df["bounce_rate"].median(),
            "unmatched_avg_time_on_page": unmatched_df["avg_time_on_page"].mean(),
            "unmatched_median_time_on_page": unmatched_df["avg_time_on_page"].median(),
            "unmatched_avg_engagement_time_seconds": unmatched_df[
                "average_engagement_time_seconds"
            ].mean(),
            "unmatched_median_engagement_time_seconds": unmatched_df[
                "average_engagement_time_seconds"
            ].median(),
            "avg_keyword_mentions_per_matched_article": matched_df[count_column].mean(),
        })

    return pd.DataFrame(rows)


def build_frequency_summary(article_df):
    rows = []

    for _, row in article_df.iterrows():
        word_counts = Counter(tokenize(row["article_text"]))

        for word, count in word_counts.items():
            rows.append({
                "word": word,
                "url": row["url"],
                "occurrences": count,
                "views": row["views"],
                "users": row["users"],
                "new_users": row.get("new_users", np.nan),
                "returning_users_estimated": row.get(
                    "returning_users_estimated",
                    np.nan,
                ),
                "sessions": row["sessions"],
                "bounce_rate": row["bounce_rate"],
                "avg_time_on_page": row["avg_time_on_page"],
                "average_engagement_time_seconds": row[
                    "average_engagement_time_seconds"
                ],
            })

    if not rows:
        return pd.DataFrame()

    word_df = pd.DataFrame(rows)
    summary_rows = []

    for word, group in word_df.groupby("word"):
        article_count = group["url"].nunique()
        if article_count < MIN_ARTICLES_WITH_WORD:
            continue

        summary_rows.append({
            "word": word,
            "article_count_with_word": article_count,
            "total_occurrences": group["occurrences"].sum(),
            "avg_occurrences_per_article": group["occurrences"].mean(),
            "total_views_for_articles": group["views"].sum(),
            "avg_views_for_articles": group["views"].mean(),
            "median_views_for_articles": group["views"].median(),
            "total_new_users_for_articles": group["new_users"].sum(),
            "avg_new_users_for_articles": group["new_users"].mean(),
            "median_new_users_for_articles": group["new_users"].median(),
            "total_returning_users_for_articles": group[
                "returning_users_estimated"
            ].sum(),
            "avg_returning_users_for_articles": group[
                "returning_users_estimated"
            ].mean(),
            "median_returning_users_for_articles": group[
                "returning_users_estimated"
            ].median(),
            "avg_bounce_rate_for_articles": group["bounce_rate"].mean(),
            "median_bounce_rate_for_articles": group["bounce_rate"].median(),
            "avg_time_on_page_for_articles": group["avg_time_on_page"].mean(),
            "median_time_on_page_for_articles": group["avg_time_on_page"].median(),
            "avg_engagement_time_seconds_for_articles": group[
                "average_engagement_time_seconds"
            ].mean(),
            "median_engagement_time_seconds_for_articles": group[
                "average_engagement_time_seconds"
            ].median(),
            "weighted_bounce_rate_for_articles": weighted_average(
                group["bounce_rate"],
                group["sessions"],
            ),
            "weighted_time_on_page_for_articles": weighted_average(
                group["avg_time_on_page"],
                group["sessions"],
            ),
        })

    summary_df = pd.DataFrame(summary_rows)
    if summary_df.empty:
        return summary_df

    return (
        summary_df.sort_values(
            ["avg_views_for_articles", "article_count_with_word"],
            ascending=[False, False],
        )
        .head(TOP_WORD_LIMIT)
        .reset_index(drop=True)
    )


def build_segment_distribution(article_df, keyword_segments):
    rows = []

    for segment_name in keyword_segments:
        match_column = f"segment_{segment_name}"
        count_column = f"segment_{segment_name}_keyword_count"

        if match_column not in article_df.columns:
            continue

        matched_df = article_df[article_df[match_column]].copy()

        for _, row in matched_df.iterrows():
            rows.append({
                "segment": segment_name,
                "title": row.get("title", row.get("ga4_title", "")),
                "url": row["url"],
                "views": row["views"],
                "users": row["users"],
                "sessions": row["sessions"],
                "bounce_rate": row["bounce_rate"],
                "avg_time_on_page": row.get("avg_time_on_page", np.nan),
                "average_engagement_time_seconds": row.get(
                    "average_engagement_time_seconds",
                    np.nan,
                ),
                "word_count": row.get("word_count", row.get("analysis_word_count")),
                "keyword_mentions": row.get(count_column, 0),
            })

    distribution_df = pd.DataFrame(rows)
    if distribution_df.empty:
        return distribution_df

    distribution_df["views_bin"] = pd.cut(
        distribution_df["views"],
        bins=[0, 1, 5, 10, 25, 50, 100, 250, 500, 1000, float("inf")],
        labels=[
            "0-1",
            "2-5",
            "6-10",
            "11-25",
            "26-50",
            "51-100",
            "101-250",
            "251-500",
            "501-1000",
            "1000+",
        ],
        include_lowest=True,
    )

    distribution_df["bounce_rate_bin"] = pd.cut(
        distribution_df["bounce_rate"],
        bins=[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1],
        labels=[
            "0-10%",
            "10-20%",
            "20-30%",
            "30-40%",
            "40-50%",
            "50-60%",
            "60-70%",
            "70-80%",
            "80-90%",
            "90-100%",
        ],
        include_lowest=True,
    )

    distribution_df["time_on_page_bin"] = pd.cut(
        distribution_df["avg_time_on_page"],
        bins=[0, 15, 30, 60, 90, 120, 180, 240, 300, float("inf")],
        labels=[
            "0-15 sec",
            "16-30 sec",
            "31-60 sec",
            "61-90 sec",
            "91-120 sec",
            "121-180 sec",
            "181-240 sec",
            "241-300 sec",
            "300+ sec",
        ],
        include_lowest=True,
    )

    return distribution_df.sort_values(["segment", "views"], ascending=[True, False])


def build_company_mention_tables(article_df, configured_companies):
    rows = []

    for _, row in article_df.iterrows():
        mention_text = " ".join([
            str(row.get("article_title", "")),
            str(row.get("article_text", "")),
        ])
        mentions = extract_company_mentions(mention_text, configured_companies)

        for company, mention_count in mentions.items():
            metric_row = article_metric_row(row)
            metric_row.update({
                "company": company,
                "company_mentions": mention_count,
            })
            rows.append(metric_row)

    article_company_df = pd.DataFrame(rows)
    if article_company_df.empty:
        return article_company_df, pd.DataFrame()

    summary_rows = []
    for company, group in article_company_df.groupby("company"):
        article_count = group["url"].nunique()
        if article_count < MIN_ARTICLES_WITH_COMPANY:
            continue

        summary_rows.append({
            "company": company,
            "article_count": article_count,
            "total_company_mentions": group["company_mentions"].sum(),
            "avg_mentions_per_article": group["company_mentions"].mean(),
            "total_views": group["views"].sum(),
            "avg_views": group["views"].mean(),
            "median_views": group["views"].median(),
            "total_users": group["users"].sum(),
            "total_new_users": group["new_users"].sum()
            if "new_users" in group.columns
            else np.nan,
            "total_returning_users_estimated": group["returning_users_estimated"].sum()
            if "returning_users_estimated" in group.columns
            else np.nan,
            "total_sessions": group["sessions"].sum(),
            "weighted_bounce_rate": weighted_average(
                group["bounce_rate"],
                group["sessions"],
            ),
            "weighted_time_on_page": weighted_average(
                group["avg_time_on_page"],
                group["sessions"],
            ),
            "avg_engagement_time_seconds": group[
                "average_engagement_time_seconds"
            ].mean(),
        })

    company_summary_df = pd.DataFrame(summary_rows)
    if not company_summary_df.empty:
        company_summary_df = company_summary_df.sort_values(
            ["avg_views", "article_count"],
            ascending=[False, False],
        )

    return article_company_df.sort_values("views", ascending=False), company_summary_df


def build_section_tables(article_df):
    rows = []

    for _, row in article_df.iterrows():
        section_tags = extract_section_tags(row.get("url", ""))
        if not section_tags and row.get("primary_section_tag"):
            section_tags = [row.get("primary_section_tag")]

        for section_tag in section_tags:
            metric_row = article_metric_row(row)
            metric_row.update({
                "section_tag": section_tag,
                "primary_section_tag": row.get("primary_section_tag", ""),
            })
            rows.append(metric_row)

    section_article_df = pd.DataFrame(rows)
    if section_article_df.empty:
        return (
            pd.DataFrame(columns=SECTION_ARTICLE_COLUMNS),
            pd.DataFrame(columns=SECTION_SUMMARY_COLUMNS),
        )

    summary_rows = []
    for section_tag, group in section_article_df.groupby("section_tag"):
        summary_rows.append({
            "section_tag": section_tag,
            "article_count": group["url"].nunique(),
            "total_views": group["views"].sum(),
            "avg_views": group["views"].mean(),
            "median_views": group["views"].median(),
            "total_users": group["users"].sum(),
            "total_new_users": group["new_users"].sum()
            if "new_users" in group.columns
            else np.nan,
            "total_returning_users_estimated": group["returning_users_estimated"].sum()
            if "returning_users_estimated" in group.columns
            else np.nan,
            "median_returning_users_estimated": group[
                "returning_users_estimated"
            ].median()
            if "returning_users_estimated" in group.columns
            else np.nan,
            "total_sessions": group["sessions"].sum(),
            "weighted_bounce_rate": weighted_average(
                group["bounce_rate"],
                group["sessions"],
            ),
            "weighted_time_on_page": weighted_average(
                group["avg_time_on_page"],
                group["sessions"],
            ),
            "avg_engagement_time_seconds": group[
                "average_engagement_time_seconds"
            ].mean(),
        })

    section_summary_df = pd.DataFrame(summary_rows)
    for column in SECTION_SUMMARY_COLUMNS:
        if column not in section_summary_df.columns:
            section_summary_df[column] = np.nan
    section_summary_df = section_summary_df[SECTION_SUMMARY_COLUMNS].sort_values(
        "total_views",
        ascending=False,
    )

    for column in SECTION_ARTICLE_COLUMNS:
        if column not in section_article_df.columns:
            section_article_df[column] = np.nan

    return (
        section_article_df[SECTION_ARTICLE_COLUMNS].sort_values(
            "views",
            ascending=False,
        ),
        section_summary_df,
    )


def build_section_daily_table(article_df, daily_ga4_df):
    if daily_ga4_df.empty or article_df.empty:
        return pd.DataFrame()

    article_sections = []
    for _, row in article_df.iterrows():
        section_tags = [
            tag.strip()
            for tag in str(row.get("section_tags", "")).split(",")
            if tag.strip()
        ]
        if not section_tags and row.get("primary_section_tag"):
            section_tags = [row.get("primary_section_tag")]

        for url in split_grouped_urls(row.get("url", "")):
            for section_tag in section_tags:
                article_sections.append({
                    "url": url,
                    "article_title": row.get("article_title", ""),
                    "published_date": row.get("published_date", pd.NaT),
                    "section_tag": section_tag,
                    "primary_section_tag": row.get("primary_section_tag", ""),
                })

    section_lookup_df = pd.DataFrame(article_sections)
    if section_lookup_df.empty:
        return pd.DataFrame()

    section_lookup_df = section_lookup_df.drop_duplicates(["url", "section_tag"])
    daily_df = daily_ga4_df.merge(section_lookup_df, on="url", how="inner")
    if daily_df.empty:
        return daily_df

    columns = [
        "date",
        "article_title",
        "url",
        "published_date",
        "section_tag",
        "primary_section_tag",
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "avg_time_on_page",
        "average_engagement_time_seconds",
    ]
    available_columns = [column for column in columns if column in daily_df.columns]
    return daily_df[available_columns].sort_values(
        ["date", "section_tag", "views"],
        ascending=[True, True, False],
    )


def build_keyword_article_daily_table(article_df, daily_ga4_df, keyword_segments):
    if daily_ga4_df.empty or article_df.empty:
        return pd.DataFrame()

    segment_columns = []
    for segment_name in keyword_segments:
        segment_columns.extend([
            f"segment_{segment_name}",
            f"segment_{segment_name}_keyword_count",
        ])

    metadata_columns = [
        "article_title",
        "published_date",
        "author",
        "section",
        "primary_section_tag",
        "section_tags",
        *segment_columns,
    ]

    lookup_rows = []
    for _, row in article_df.iterrows():
        for url in split_grouped_urls(row.get("url", "")):
            lookup_row = {"url": url}
            for column in metadata_columns:
                if column in article_df.columns:
                    lookup_row[column] = row.get(column)
            lookup_rows.append(lookup_row)

    lookup_df = pd.DataFrame(lookup_rows)
    if lookup_df.empty:
        return pd.DataFrame()

    lookup_df = lookup_df.drop_duplicates("url", keep="first")
    daily_df = daily_ga4_df.merge(lookup_df, on="url", how="inner")
    if daily_df.empty:
        return daily_df

    columns = [
        "date",
        "article_title",
        "url",
        "ga4_full_page_url",
        "ga4_title",
        "published_date",
        "author",
        "section",
        "primary_section_tag",
        "section_tags",
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "avg_time_on_page",
        "average_engagement_time_seconds",
        *segment_columns,
    ]
    available_columns = [column for column in columns if column in daily_df.columns]
    return daily_df[available_columns].sort_values(
        ["date", "views"],
        ascending=[True, False],
    )


def build_title_article_analysis(article_df):
    columns = [
        "article_title",
        "url",
        "views",
        "users",
        "new_users",
        "returning_users_estimated",
        "sessions",
        "bounce_rate",
        "avg_time_on_page",
        "average_engagement_time_seconds",
        "title_word_count",
        "title_character_count",
        "word_count",
        "analysis_word_count",
        "primary_section_tag",
        "section_tags",
    ]
    available_columns = [column for column in columns if column in article_df.columns]
    return article_df[available_columns].sort_values("views", ascending=False)


def build_title_keyword_summary(article_df):
    rows = []

    for _, row in article_df.iterrows():
        title_word_counts = Counter(tokenize_title(row["article_title"]))

        for word, count in title_word_counts.items():
            rows.append({
                "title_word": word,
                "article_title": row["article_title"],
                "url": row["url"],
                "title_occurrences": count,
                "views": row["views"],
                "users": row["users"],
                "new_users": row.get("new_users", np.nan),
                "returning_users_estimated": row.get(
                    "returning_users_estimated",
                    np.nan,
                ),
                "sessions": row["sessions"],
                "bounce_rate": row["bounce_rate"],
                "avg_time_on_page": row["avg_time_on_page"],
                "average_engagement_time_seconds": row[
                    "average_engagement_time_seconds"
                ],
                "title_word_count": row["title_word_count"],
                "title_character_count": row["title_character_count"],
            })

    if not rows:
        return pd.DataFrame()

    title_word_df = pd.DataFrame(rows)
    summary_rows = []

    for word, group in title_word_df.groupby("title_word"):
        article_count = group["url"].nunique()
        if article_count < MIN_ARTICLES_WITH_TITLE_WORD:
            continue

        summary_rows.append({
            "title_word": word,
            "article_count_with_title_word": article_count,
            "total_title_occurrences": group["title_occurrences"].sum(),
            "avg_title_word_count": group["title_word_count"].mean(),
            "avg_title_character_count": group["title_character_count"].mean(),
            "total_views_for_articles": group["views"].sum(),
            "avg_views_for_articles": group["views"].mean(),
            "median_views_for_articles": group["views"].median(),
            "total_new_users_for_articles": group["new_users"].sum(),
            "avg_new_users_for_articles": group["new_users"].mean(),
            "median_new_users_for_articles": group["new_users"].median(),
            "total_returning_users_for_articles": group[
                "returning_users_estimated"
            ].sum(),
            "avg_returning_users_for_articles": group[
                "returning_users_estimated"
            ].mean(),
            "median_returning_users_for_articles": group[
                "returning_users_estimated"
            ].median(),
            "avg_bounce_rate_for_articles": group["bounce_rate"].mean(),
            "median_bounce_rate_for_articles": group["bounce_rate"].median(),
            "avg_time_on_page_for_articles": group["avg_time_on_page"].mean(),
            "median_time_on_page_for_articles": group["avg_time_on_page"].median(),
            "avg_engagement_time_seconds_for_articles": group[
                "average_engagement_time_seconds"
            ].mean(),
            "median_engagement_time_seconds_for_articles": group[
                "average_engagement_time_seconds"
            ].median(),
            "weighted_bounce_rate_for_articles": weighted_average(
                group["bounce_rate"],
                group["sessions"],
            ),
            "weighted_time_on_page_for_articles": weighted_average(
                group["avg_time_on_page"],
                group["sessions"],
            ),
        })

    summary_df = pd.DataFrame(summary_rows)
    if summary_df.empty:
        return summary_df

    return (
        summary_df.sort_values(
            ["avg_views_for_articles", "article_count_with_title_word"],
            ascending=[False, False],
        )
        .head(TOP_WORD_LIMIT)
        .reset_index(drop=True)
    )


def build_title_length_summary(article_df):
    title_df = article_df.copy()
    title_df["title_word_count_bucket"] = pd.cut(
        title_df["title_word_count"],
        bins=[0, 5, 8, 12, 16, 20, float("inf")],
        labels=["1-5", "6-8", "9-12", "13-16", "17-20", "21+"],
        include_lowest=True,
    )
    title_df["title_character_count_bucket"] = pd.cut(
        title_df["title_character_count"],
        bins=[0, 40, 60, 80, 100, 120, float("inf")],
        labels=["1-40", "41-60", "61-80", "81-100", "101-120", "121+"],
        include_lowest=True,
    )

    return (
        title_df.groupby("title_word_count_bucket", observed=False)
        .agg(
            article_count=("url", "nunique"),
            avg_title_word_count=("title_word_count", "mean"),
            avg_title_character_count=("title_character_count", "mean"),
            total_views=("views", "sum"),
            avg_views=("views", "mean"),
            median_views=("views", "median"),
            avg_bounce_rate=("bounce_rate", "mean"),
            median_bounce_rate=("bounce_rate", "median"),
            avg_time_on_page=("avg_time_on_page", "mean"),
            median_time_on_page=("avg_time_on_page", "median"),
            avg_engagement_time_seconds=(
                "average_engagement_time_seconds",
                "mean",
            ),
            median_engagement_time_seconds=(
                "average_engagement_time_seconds",
                "median",
            ),
        )
        .reset_index()
    )


# =========================
# GOOGLE SHEETS WRITE
# =========================

def clean_output_columns(df):
    result_df = df.copy()
    status_columns = [
        column for column in ["scrape_status", "scrape_status_y", "scrape_status_x"]
        if column in result_df.columns
    ]

    if status_columns:
        result_df["scrape_status"] = result_df.apply(
            lambda row: first_non_blank([row.get(column) for column in status_columns]),
            axis=1,
        )
        result_df = result_df.drop(
            columns=[
                column for column in ["scrape_status_x", "scrape_status_y"]
                if column in result_df.columns
            ],
            errors="ignore",
        )

    return result_df


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

    write_df = clean_output_columns(df)
    for column in write_df.columns:
        if pd.api.types.is_datetime64_any_dtype(write_df[column]):
            write_df[column] = write_df[column].dt.strftime("%Y-%m-%d")

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
    article_daily_df,
    segment_df,
    frequency_df,
    distribution_df,
    title_article_df,
    title_keyword_df,
    title_length_df,
    company_article_df,
    company_summary_df,
    section_article_df,
    section_summary_df,
    section_daily_df,
):
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open(SPREADSHEET_NAME)

    update_or_create_worksheet(
        spreadsheet,
        ARTICLE_OUTPUT_WORKSHEET_NAME,
        article_df.drop(columns=["article_text"], errors="ignore"),
    )
    update_or_create_worksheet(
        spreadsheet,
        ARTICLE_DAILY_OUTPUT_WORKSHEET_NAME,
        article_daily_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SEGMENT_OUTPUT_WORKSHEET_NAME,
        segment_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        FREQUENCY_OUTPUT_WORKSHEET_NAME,
        frequency_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SEGMENT_DISTRIBUTION_WORKSHEET_NAME,
        distribution_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        TITLE_ARTICLE_OUTPUT_WORKSHEET_NAME,
        title_article_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        TITLE_KEYWORD_OUTPUT_WORKSHEET_NAME,
        title_keyword_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        TITLE_LENGTH_OUTPUT_WORKSHEET_NAME,
        title_length_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        COMPANY_ARTICLE_OUTPUT_WORKSHEET_NAME,
        company_article_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        COMPANY_SUMMARY_OUTPUT_WORKSHEET_NAME,
        company_summary_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SECTION_ARTICLE_OUTPUT_WORKSHEET_NAME,
        section_article_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SECTION_SUMMARY_OUTPUT_WORKSHEET_NAME,
        section_summary_df,
    )
    update_or_create_worksheet(
        spreadsheet,
        SECTION_DAILY_OUTPUT_WORKSHEET_NAME,
        section_daily_df,
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
    keyword_segments = load_keyword_segments()
    configured_companies = load_company_names(credentials)
    if configured_companies:
        print(f"Loaded {len(configured_companies)} configured company names.")
    else:
        print(
            "No configured company list found; company mention outputs will be empty. "
            "Run scripts/enrich_company_list.py to build company_list first."
        )

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
                "avg_time_on_page",
                "total_user_engagement_seconds",
                "average_engagement_time_seconds",
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
                "avg_time_on_page",
                "total_user_engagement_seconds",
                "average_engagement_time_seconds",
            ]
        )

    print(
        "Pulling daily GA4 article metrics "
        f"from {ALL_TIME_START_DATE} to {all_time_end_date}..."
    )
    daily_ga4_df = get_ga4_article_metrics_by_date(
        credentials,
        ALL_TIME_START_DATE,
        all_time_end_date,
    )
    print(f"Loaded {len(daily_ga4_df)} daily GA4 article metric rows.")

    candidate_urls = sorted(set(enriched_df["url"]) | selected_active_urls)
    print(f"Candidate article URLs for text analysis: {len(candidate_urls)}")

    text_df = collect_article_texts(candidate_urls, scrape_missing)
    published_date_df = build_published_date_updates(enriched_df, text_df)
    print(f"Loaded {len(published_date_df)} known published date rows.")

    print(
        "Building keyword analysis with all-time metrics "
        f"and excluding word_count > {MAX_WORD_COUNT}..."
    )
    article_df = build_article_analysis(
        enriched_df,
        ga4_df,
        text_df,
        keyword_segments,
        start_date,
        end_date,
        selected_active_urls,
        published_date_df,
    )

    if article_df.empty:
        print("No matched articles remained after filtering.")
        return

    segment_df = build_segment_summary(article_df, keyword_segments)
    frequency_df = build_frequency_summary(article_df)
    distribution_df = build_segment_distribution(article_df, keyword_segments)
    title_article_df = build_title_article_analysis(article_df)
    title_keyword_df = build_title_keyword_summary(article_df)
    title_length_df = build_title_length_summary(article_df)
    company_article_df, company_summary_df = build_company_mention_tables(
        article_df,
        configured_companies,
    )
    section_article_df, section_summary_df = build_section_tables(article_df)
    article_daily_df = build_keyword_article_daily_table(
        article_df,
        daily_ga4_df,
        keyword_segments,
    )
    section_daily_df = build_section_daily_table(article_df, daily_ga4_df)

    article_output_df = clean_output_columns(
        article_df.drop(columns=["article_text"], errors="ignore")
    )
    title_article_df = clean_output_columns(title_article_df)

    article_output_df.to_csv(
        KEYWORD_ARTICLE_OUTPUT_FILE,
        index=False,
    )
    article_daily_df.to_csv(KEYWORD_ARTICLE_DAILY_OUTPUT_FILE, index=False)
    segment_df.to_csv(KEYWORD_SEGMENT_OUTPUT_FILE, index=False)
    frequency_df.to_csv(KEYWORD_FREQUENCY_OUTPUT_FILE, index=False)
    distribution_df.to_csv(KEYWORD_SEGMENT_DISTRIBUTION_OUTPUT_FILE, index=False)
    title_article_df.to_csv(TITLE_ARTICLE_OUTPUT_FILE, index=False)
    title_keyword_df.to_csv(TITLE_KEYWORD_OUTPUT_FILE, index=False)
    title_length_df.to_csv(TITLE_LENGTH_OUTPUT_FILE, index=False)
    company_article_df.to_csv(COMPANY_ARTICLE_OUTPUT_FILE, index=False)
    company_summary_df.to_csv(COMPANY_SUMMARY_OUTPUT_FILE, index=False)
    section_article_df.to_csv(SECTION_ARTICLE_OUTPUT_FILE, index=False)
    section_summary_df.to_csv(SECTION_SUMMARY_OUTPUT_FILE, index=False)
    section_daily_df.to_csv(SECTION_DAILY_OUTPUT_FILE, index=False)

    print("Writing keyword analysis tabs to Google Sheets...")
    write_analysis_to_google_sheet(
        credentials,
        article_output_df,
        article_daily_df,
        segment_df,
        frequency_df,
        distribution_df,
        title_article_df,
        title_keyword_df,
        title_length_df,
        company_article_df,
        company_summary_df,
        section_article_df,
        section_summary_df,
        section_daily_df,
    )

    print("\nTop keyword frequency rows:")
    if frequency_df.empty:
        print("No frequency rows met the minimum article threshold.")
    else:
        print(frequency_df.head(25).to_string(index=False))

    print("\nDone.")


if __name__ == "__main__":
    main()
