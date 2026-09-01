"""Run the general, keyword, and publish timing GA4 analyses together."""

import argparse
import sys
import time

import ga4_general_analytics
import ga4_keyword_analysis
import ga4_publish_timing_analysis


DEFAULT_START_DATE = "700daysAgo"
DEFAULT_END_DATE = "today"


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run general analytics, keyword analysis, and publish timing "
            "analysis with one shared date range and scraping choice."
        )
    )
    parser.add_argument(
        "--start-date",
        help=f"GA4 start date (default: {DEFAULT_START_DATE})",
    )
    parser.add_argument(
        "--end-date",
        help=f"GA4 end date (default: {DEFAULT_END_DATE})",
    )

    scrape_group = parser.add_mutually_exclusive_group()
    scrape_group.add_argument(
        "--scrape",
        dest="scrape_missing",
        action="store_true",
        help="Scrape missing article text and publish times.",
    )
    scrape_group.add_argument(
        "--no-scrape",
        dest="scrape_missing",
        action="store_false",
        help="Use GA4 and existing scrape caches only.",
    )
    parser.set_defaults(scrape_missing=None)
    return parser.parse_args()


def prompt_with_default(prompt, default):
    value = input(f"{prompt} (default {default}): ").strip()
    return value or default


def prompt_for_scraping():
    choice = input(
        "Scrape missing article data? "
        "[Y = scrape + GA4, N = GA4/cache only] (default Y): "
    ).strip().lower()
    return choice not in {"n", "no"}


def run_analysis(name, function, *args, **kwargs):
    print("\n" + "=" * 72)
    print(f"RUNNING: {name}")
    print("=" * 72)
    started_at = time.monotonic()
    function(*args, **kwargs)
    elapsed = time.monotonic() - started_at
    print(f"\nCompleted {name} in {elapsed / 60:.1f} minutes.")


def main():
    args = parse_args()

    start_date = args.start_date or DEFAULT_START_DATE
    end_date = args.end_date or DEFAULT_END_DATE
    scrape_missing = (
        args.scrape_missing
        if args.scrape_missing is not None
        else prompt_for_scraping()
    )

    print("\nRun settings")
    print(f"  Date range: {start_date} to {end_date}")
    print(f"  Scraping:   {'enabled' if scrape_missing else 'disabled'}")

    analyses = [
        (
            "General Analytics",
            ga4_general_analytics.main,
            {"start_date": start_date, "end_date": end_date},
        ),
        (
            "Publish Timing Analysis",
            ga4_publish_timing_analysis.main,
            {
                "start_date": start_date,
                "end_date": end_date,
                "scrape_missing": scrape_missing,
            },
        ),
        (
            "Keyword Analysis",
            ga4_keyword_analysis.main,
            {
                "start_date": start_date,
                "end_date": end_date,
                "scrape_missing": scrape_missing,
            },
        ),
    ]

    total_started_at = time.monotonic()
    for name, function, kwargs in analyses:
        try:
            run_analysis(name, function, **kwargs)
        except KeyboardInterrupt:
            print(f"\nStopped while running {name}.")
            raise
        except Exception as error:
            print(f"\nERROR: {name} failed: {error}")
            print("The remaining analyses were not run.")
            return 1

    elapsed = time.monotonic() - total_started_at
    print("\n" + "=" * 72)
    print(f"ALL ANALYSES COMPLETED in {elapsed / 60:.1f} minutes")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
