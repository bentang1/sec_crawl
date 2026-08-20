"""Entry point: python -m edgar_scraper [--limit N] [--retry-failed] [--tickers AAPL,MSFT]"""

from __future__ import annotations

import argparse
import logging

from .config import load_config
from .scraper import ScraperRun, fetch_company_list


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None, help="Only process the first N pending companies (smoke-testing)."
    )
    parser.add_argument(
        "--retry-failed", action="store_true", help="Re-attempt companies previously marked failed."
    )
    parser.add_argument(
        "--tickers", type=str, default=None, help="Comma-separated ticker list to restrict this run to."
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config()
    run = ScraperRun(config, retry_failed=args.retry_failed, limit=args.limit)

    companies = fetch_company_list(run.client, run.cache)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        companies = [c for c in companies if c.ticker.upper() in wanted]

    run.run(companies)
    print(f"Output CSV: {config.output_csv_path}")
    print(f"Failure CSV: {config.failure_csv_path}")


if __name__ == "__main__":
    main()
