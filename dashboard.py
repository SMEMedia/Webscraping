"""Interactive dashboard views backed by AM_Enriched_Articles."""
import os
import sys
from pathlib import Path

import gspread
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
import service_account_auth  # noqa: E402

SHEET_ID = "1JLfIsfecWiGYfIzsYfDfOcRvlritfCmSFhUuJPxj0W4"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
COMPANY_LIST_URL = "https://docs.google.com/spreadsheets/d/194SdsfBVsJVKSOzV64jLB4ds3iE8hHVdf0RmwKYj_ag/edit"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
NAVY = "#193b60"
BLUE = "#6591b7"
LIME = "#d8d94f"
TEAL = "#3a9b85"

STATE_CODES = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
    "Colorado":"CO","Connecticut":"CT","Delaware":"DE","District of Columbia":"DC",
    "Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID","Illinois":"IL",
    "Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
    "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
    "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY",
    "North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR",
    "Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD",
    "Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
    "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY",
}


def _credentials_signature():
    return os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "local-file")


@st.cache_data(ttl=300, show_spinner=False)
def load_sheet(sheet_name, _signature):
    credentials = service_account_auth.get_credentials(SCOPES)
    worksheet = gspread.authorize(credentials).open_by_key(SHEET_ID).worksheet(sheet_name)
    values = worksheet.get_all_values()
    if not values:
        return pd.DataFrame()
    width = len(values[0])
    rows = [row[:width] + [""] * max(0, width - len(row)) for row in values[1:]]
    return pd.DataFrame(rows, columns=values[0])


def sheet(name):
    return load_sheet(name, _credentials_signature()).copy()


def numeric(df, columns):
    for column in columns:
        if column in df:
            df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)
    return df


def dated(df, column="published_date"):
    if column in df:
        df[column] = pd.to_datetime(df[column], errors="coerce").dt.date
    return df


def filtered_dates(df, key, column="published_date"):
    df = dated(df, column)
    valid = df[column].dropna() if column in df else pd.Series(dtype=object)
    if valid.empty:
        return df
    default_start = max(valid.min(), pd.Timestamp.today().date() - pd.Timedelta(days=365))
    chosen = st.date_input(
        "Published date",
        (default_start, valid.max()),
        min_value=valid.min(),
        max_value=valid.max(),
        key=key,
    )
    if len(chosen) == 2:
        return df[df[column].between(chosen[0], chosen[1])]
    return df


def company_filter(df, key):
    companies = sorted(df["company"].dropna().astype(str).unique())
    selected = st.multiselect("Company", companies, key=key, placeholder="All companies")
    return df[df["company"].isin(selected)] if selected else df


def chart(fig, height=430):
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=55, b=10),
                      font=dict(family="Arial"), legend_title_text="")
    st.plotly_chart(fig, width="stretch")


def company_summary(df):
    metrics = numeric(df.copy(), ["views","users","returning_users_estimated","sessions","bounce_rate"])
    metrics["weighted_bounce"] = metrics["bounce_rate"] * metrics["sessions"]
    return metrics.groupby("company", as_index=False).agg(
        article_count=("article_title", "nunique"), total_views=("views", "sum"),
        total_users=("users", "sum"), returning_users=("returning_users_estimated", "sum"),
        sessions=("sessions", "sum"), weighted_bounce=("weighted_bounce", "sum"),
    ).assign(weighted_bounce_rate=lambda x: x.weighted_bounce / x.sessions.replace(0, np.nan))


def company_mentions_view():
    st.header("Company Mentions")
    df = filtered_dates(sheet("Company_Article_Mentions"), "cm_dates")
    df = company_filter(df, "cm_company")
    if df.empty:
        st.info("No company mentions match these filters."); return
    summary = company_summary(df)
    left, right = st.columns([1, 1.25])
    with left:
        top = summary.nlargest(25, "article_count").sort_values("article_count")
        chart(px.bar(top, x="article_count", y="company", orientation="h",
                     title="Top mentioned companies by article count", color_discrete_sequence=[NAVY]))
    with right:
        shown = summary.sort_values("article_count", ascending=False).rename(columns={
            "company":"Company","article_count":"Article count","total_views":"Views",
            "returning_users":"Returning users","total_users":"Total users",
            "weighted_bounce_rate":"Bounce rate"})
        st.subheader("Company performance")
        st.dataframe(shown[["Company","Article count","Views","Returning users","Total users","Bounce rate"]],
                     hide_index=True, width="stretch",
                     column_config={"Bounce rate": st.column_config.NumberColumn(format="%.1%%")})
        articles = df[["company","article_title","url","published_date"]].drop_duplicates()
        st.subheader("Mentioned articles")
        st.dataframe(articles.rename(columns={"company":"Company","article_title":"Article title",
                     "url":"Article URL","published_date":"Published"}), hide_index=True,
                     width="stretch", column_config={"Article URL":st.column_config.LinkColumn()})


def company_performance_view():
    st.header("Company Performance")
    df = company_filter(filtered_dates(sheet("Company_Article_Mentions"), "cp_dates"), "cp_company")
    if df.empty: st.info("No companies match these filters."); return
    summary = company_summary(df)
    top_bounce = summary.query("article_count >= 2").nlargest(15, "weighted_bounce_rate").sort_values("weighted_bounce_rate")
    chart(px.bar(top_bounce, x="weighted_bounce_rate", y="company", orientation="h",
                 title="Weighted bounce rate by company", color_discrete_sequence=[NAVY],
                 labels={"weighted_bounce_rate":"Bounce rate","company":"Company"}))
    top = summary.nlargest(12, "total_views")
    fig = go.Figure([go.Bar(name="Total views", x=top.company, y=top.total_views, marker_color=NAVY),
                     go.Bar(name="Total users", x=top.company, y=top.total_users, marker_color=LIME),
                     go.Scatter(name="Article count", x=top.company, y=top.article_count,
                                yaxis="y2", mode="lines+markers", line=dict(color=BLUE, width=3))])
    fig.update_layout(title="Top performing companies", barmode="group",
                      yaxis2=dict(overlaying="y", side="right", title="Articles"))
    chart(fig, 500)


def company_section_view():
    st.header("Company Breakdown by Section Tag")
    companies = filtered_dates(sheet("Company_Article_Mentions"), "cs_dates")
    companies = company_filter(companies, "cs_company")
    sections = sheet("Section_Articles")[["article_title","section_tag"]].drop_duplicates()
    merged = companies.merge(sections, on="article_title", how="left")
    grouped = merged.groupby(["company","section_tag"])["article_title"].nunique().reset_index(name="articles")
    top_companies = grouped.groupby("company").articles.sum().nlargest(15).index
    grouped = grouped[grouped.company.isin(top_companies)]
    chart(px.bar(grouped, x="company", y="articles", color="section_tag", barmode="group",
                 title="Company mentions by section tag",
                 labels={"company":"Company","articles":"Article count","section_tag":"Section tag"}), 550)


def article_table_view():
    st.header("Article Table")
    df = filtered_dates(sheet("Keyword_Articles"), "at_dates")
    c1, c2 = st.columns(2)
    with c1:
        authors = st.multiselect("Author", sorted(df.author.dropna().unique()), key="at_author")
    with c2:
        sections = st.multiselect("Section", sorted(df.primary_section_tag.dropna().unique()), key="at_section")
    if authors: df = df[df.author.isin(authors)]
    if sections: df = df[df.primary_section_tag.isin(sections)]
    df = numeric(df, ["views","users","returning_users_estimated","bounce_rate"])
    shown = df[["article_title","primary_url","views","users","returning_users_estimated","bounce_rate"]]
    shown = shown.rename(columns={"article_title":"Article title","primary_url":"URL","views":"Views",
        "users":"Users","returning_users_estimated":"Returning users","bounce_rate":"Bounce rate"})
    st.dataframe(shown, hide_index=True, width="stretch", height=650,
                 column_config={"URL":st.column_config.LinkColumn(),
                                "Bounce rate":st.column_config.NumberColumn(format="%.1%%")})


def kpi_view():
    st.header("AM KPIs")
    kpis = numeric(sheet("General_KPIs"), ["current_value","percent_change"])
    labels = {"total_views":"Views","total_users":"Users","total_sessions":"Sessions",
              "returning_users_estimated":"Returning users","new_users":"New users"}
    cols = st.columns(5)
    for col, metric in zip(cols, labels):
        row = kpis[kpis.metric == metric]
        if not row.empty:
            col.metric(labels[metric], f"{row.current_value.iloc[0]:,.0f}", f"{row.percent_change.iloc[0]:+.1%}")
    dates = numeric(sheet("General_Users_By_Date"), ["users","new_users","returning_users_estimated"])
    dates["date"] = pd.to_datetime(dates.date, errors="coerce")
    left, right = st.columns([1.35, 1])
    with left:
        long = dates.melt("date", ["users","new_users","returning_users_estimated"], var_name="User type", value_name="Users")
        chart(px.line(long, x="date", y="Users", color="User type", title="Users by date",
                      color_discrete_sequence=[BLUE,LIME,NAVY]), 420)
    with right:
        sources = numeric(sheet("General_Source_Medium"), ["views"])
        chart(px.pie(sources.nlargest(8,"views"), names="session_source_medium", values="views",
                     title="Session source/medium by views", color_discrete_sequence=[NAVY,LIME,BLUE,TEAL]), 420)
    state_map(sheet("General_Users_By_State"), "users", "Users by state")


def state_map(df, value, title):
    df = numeric(df, [value]); df["code"] = df.state.map(STATE_CODES)
    df = df.dropna(subset=["code"])
    fig = px.choropleth(df, locations="code", locationmode="USA-states", color=value,
                        scope="usa", hover_name="state", title=title,
                        color_continuous_scale=[[0,"#eef2d1"],[1,NAVY]])
    chart(fig, 480)


def title_segment_view():
    st.header("Title Keywords and Segments")
    words = numeric(sheet("Title_Keywords"), ["median_views_for_articles","article_count_with_title_word"])
    words = words.nlargest(25, "median_views_for_articles")
    segments = numeric(sheet("Keyword_Segments"), ["matched_median_views","matched_articles"])
    segments = segments.nlargest(25, "matched_median_views")
    c1, c2 = st.columns(2)
    with c1: combo(words.title_word, words.median_views_for_articles, words.article_count_with_title_word,
                   "Top title keywords by article views", "Median views", "Article count")
    with c2: combo(segments.segment, segments.matched_median_views, segments.matched_articles,
                   "Top segments by views", "Median views", "Number of articles")


def combo(x, bars, line, title, bar_name, line_name):
    fig = go.Figure([go.Bar(x=x, y=bars, name=bar_name, marker_color=NAVY),
                     go.Scatter(x=x, y=line, name=line_name, yaxis="y2", line=dict(color=LIME, width=3))])
    fig.update_layout(title=title, yaxis2=dict(overlaying="y", side="right"))
    chart(fig, 500)


def section_performance_view():
    st.header("Performance by Section Tag")
    df = numeric(sheet("Section_Performance"), ["median_views","weighted_bounce_rate","article_count",
                                                "weighted_time_on_page"])
    top = df.nlargest(15, "article_count")
    c1, c2 = st.columns([1.4, 1])
    with c1: combo(top.section_tag, top.median_views, top.weighted_bounce_rate,
                   "Median views and bounce rate by section tag", "Median views", "Bounce rate")
    with c2:
        ordered = top.sort_values("article_count")
        chart(px.bar(ordered, x="article_count", y="section_tag", orientation="h",
                     title="Article count by section tag", color_discrete_sequence=[NAVY]), 500)
    focus = df[df.section_tag.isin(["news-desk","manufacturing-engineering","news-desk-press-releases"])]
    grouped = focus.melt("section_tag", ["median_views","weighted_time_on_page","article_count"],
                         var_name="Metric", value_name="Value")
    chart(px.bar(grouped, x="section_tag", y="Value", color="Metric", barmode="group",
                 title="News Desk vs Manufacturing Engineering vs Press Releases",
                 color_discrete_sequence=[NAVY,LIME,BLUE]), 450)


def returning_profile_view():
    st.header("Returning User Profile")
    sections = numeric(sheet("Section_Performance"), ["total_returning_users_estimated","total_new_users"])
    top = sections.nlargest(12, "total_returning_users_estimated")
    long = top.melt("section_tag", ["total_returning_users_estimated","total_new_users"],
                    var_name="User type", value_name="Users")
    left, right = st.columns(2)
    with left:
        chart(px.bar(long, x="Users", y="section_tag", color="User type", orientation="h",
                     barmode="stack", title="New and returning users by section",
                     color_discrete_sequence=[NAVY,LIME]), 600)
    with right:
        sources = numeric(sheet("General_Source_Medium"), ["users","returning_users_estimated"])
        sources["returning_share"] = sources.returning_users_estimated / sources.users.replace(0,np.nan)
        chart(px.pie(sources.nlargest(10,"returning_users_estimated"), names="session_source_medium",
                     values="returning_users_estimated", title="Returning users by source/medium",
                     color_discrete_sequence=[NAVY,LIME,BLUE,TEAL]), 380)
        states = numeric(sheet("General_Users_By_State"), ["users","returning_users_estimated"])
        states["returning_share"] = states.returning_users_estimated / states.users.replace(0,np.nan)
        state_map(states, "returning_share", "Returning user share by state")


def returning_top_view():
    st.header("Top Sections for Returning Users")
    sections = numeric(sheet("Section_Performance"), ["median_returning_users_estimated"])
    words = numeric(sheet("Title_Keywords"), ["median_returning_users_for_articles"])
    c1, c2 = st.columns(2)
    with c1:
        top = sections.nlargest(12,"median_returning_users_estimated").sort_values("median_returning_users_estimated")
        chart(px.bar(top, x="median_returning_users_estimated", y="section_tag", orientation="h",
                     title="Top sections for returning users", color_discrete_sequence=[NAVY]), 600)
    with c2:
        top = words.nlargest(12,"median_returning_users_for_articles")
        chart(px.bar(top, x="title_word", y="median_returning_users_for_articles",
                     title="Top title keywords for returning users", color_discrete_sequence=[NAVY]), 600)


VIEWS = {
    "Company Mentions": company_mentions_view,
    "Company Performance": company_performance_view,
    "Company Breakdown by Section Tag": company_section_view,
    "Article Table": article_table_view,
    "AM KPIs": kpi_view,
    "Title Keywords and Segments": title_segment_view,
    "Performance by Section Tag": section_performance_view,
    "Returning User Profile": returning_profile_view,
    "Top Sections for Returning Users": returning_top_view,
}


def render_dashboard():
    st.title("Web Analytics Dashboard")
    st.caption(f"Live data from [AM_Enriched_Articles]({SHEET_URL}) · [Company List]({COMPANY_LIST_URL})")
    if st.button("Reload dashboard data", help="Use after completing a dashboard refresh."):
        st.cache_data.clear()
        st.rerun()
    view = st.selectbox("Dashboard view", list(VIEWS), label_visibility="collapsed")
    try:
        with st.spinner("Loading dashboard data from Google Sheets…"):
            VIEWS[view]()
    except Exception as error:
        st.error("The dashboard could not load its Google Sheet data.")
        with st.expander("Technical details"):
            st.exception(error)
