"""Entry point: python -m edgar_scraper [--limit N] [--retry-failed] [--tickers AAPL,MSFT]

Or `--export-parquet` to rebuild data/output/ from the checkpoint without running.
"""

from __future__ import annotations

import argparse
import logging

from .config import load_config
from .scraper import ScraperRun, export_checkpoint_to_parquet, fetch_company_list


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
    parser.add_argument(
        "--export-parquet",
        action="store_true",
        help="Rebuild data/output/ from the checkpoint and exit. No filings are read and no "
        "requests are made - use after an extraction change, or to migrate an output.csv-era run.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config()

    if args.export_parquet:
        rows, written = export_checkpoint_to_parquet(config)
        print(f"Exported {rows} rows to {config.output_dir} ({written / 1e6:.1f} MB)")
        return

    run = ScraperRun(config, retry_failed=args.retry_failed, limit=args.limit)

    companies = fetch_company_list(run.client, run.cache)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        companies = [c for c in companies if c.ticker.upper() in wanted]

    run.run(companies)
    print(f"Output:      {config.output_dir}  (Parquet parts; pd.read_parquet on the directory)")
    print(f"Failure CSV: {config.failure_csv_path}")


if __name__ == "__main__":
    main()
