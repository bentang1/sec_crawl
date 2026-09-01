"""Locates each company's most recent quarterly report.

Run as its own program:

    python -m edgar_scraper.quarterly --limit 2          # download the documents
    python -m edgar_scraper.quarterly --index-only       # metadata only, nothing stored

This is the quarterly counterpart to the annual pipeline in `scraper.py` and
deliberately shares its machinery - the same throttled client, the same
`data/filings/{cik10}/{accession}/` cache layout, the same cached
`data/submissions/` filing histories (7,298 companies are already cached, so
most of the work needs no network at all) - but keeps its own checkpoint
table so the two can be run and resumed independently.

Form priority mirrors the annual one:

    10-Q -> 6-K -> 10-Q/A -> 6-K/A -> failure

Foreign private issuers and Canadian MJDS filers never file a 10-Q; 6-K is
how they report interim results, and every 20-F/40-F filer in a 600-company
sample had one. The caveat is that 6-K is a generic wrapper for "anything
disclosed abroad" - the most recent one may be a press release or a
shareholder notice rather than interim financials - so for those companies
`form` is best read as "most recent interim filing", not "quarterly report".
`primary_doc_description` is carried through so that is checkable per row.

**Storage.** The index is small - one short row per company, a few MB for the
whole universe - but the documents are not: 10-Qs average well into the
hundreds of KB and downloading all ~7,600 of them would add roughly as much
again as the 20GB the annual filings already occupy. `--index-only` skips the
documents entirely and still gives a `document_url` per company to fetch on
demand. A run also stops cleanly when free disk falls below
`EDGAR_MIN_FREE_GB` rather than filling the volume, because a truncated
download in the shared cache is a silent, sticky failure for every later run.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .cache import FilingCache
from .checkpoint import QuarterlyStore
from .client import EdgarClient
from .config import Config, load_config
from .output import QUARTERLY_SCHEMA, ParquetOutput
from .scraper import Company, SkipCompany, _filing_url, _get_submissions, fetch_company_list

logger = logging.getLogger(__name__)

# Plain forms before amendments, same rule as the annual FORM_PRIORITY: the
# first form type in this order that the company has filed wins, even if an
# amendment of a lower-priority form is more recent.
QUARTERLY_FORM_PRIORITY = ["10-Q", "6-K", "10-Q/A", "6-K/A"]


@dataclass
class QuarterlyReport:
    form: str
    accession: str
    primary_document: str
    filing_date: str
    report_date: str
    description: str
    size: int


def _latest_of_form(submissions: dict[str, Any], form: str) -> QuarterlyReport | None:
    """The most recent filing of one form type.

    `recent` is ordered newest-first by EDGAR, so the first match wins. Note
    it only holds the ~1,000 most recent filings; a company that files a high
    volume of other forms can push its quarterly reports off the end, which is
    the same limitation the annual pipeline has.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form_type in enumerate(forms):
        if form_type != form:
            continue
        primary_document = _at(recent, "primaryDocument", i)
        if not primary_document:
            continue
        return QuarterlyReport(
            form=form,
            accession=_at(recent, "accessionNumber", i),
            primary_document=primary_document,
            filing_date=_at(recent, "filingDate", i),
            report_date=_at(recent, "reportDate", i),
            description=_at(recent, "primaryDocDescription", i),
            size=int(_at(recent, "size", i) or 0),
        )
    return None


def _at(recent: dict[str, Any], field: str, index: int) -> str:
    values = recent.get(field, [])
    return str(values[index]) if index < len(values) and values[index] is not None else ""


def find_quarterly_report(submissions: dict[str, Any]) -> QuarterlyReport | None:
    for form in QUARTERLY_FORM_PRIORITY:
        found = _latest_of_form(submissions, form)
        if found is not None:
            return found
    return None


def _free_gb(path) -> float:
    return shutil.disk_usage(path).free / 1e9


class DiskFull(Exception):
    """Raised to stop a run cleanly before the volume fills."""


class QuarterlyRun:
    def __init__(
        self,
        config: Config,
        limit: int | None = None,
        retry_failed: bool = False,
        index_only: bool = False,
    ):
        self.config = config
        self.client = EdgarClient(config)
        self.cache = FilingCache(config)
        self.store = QuarterlyStore(config.checkpoint_db_path)
        self.limit = limit
        self.retry_failed = retry_failed
        self.index_only = index_only
        self.output = ParquetOutput(
            config.quarterly_output_dir,
            batch_size=config.output_batch_size,
            compression=config.parquet_compression,
            compression_level=config.parquet_compression_level,
            schema=QUARTERLY_SCHEMA,
        )
        self._lock = threading.Lock()
        self._stopped = False

    def _pending(self, companies: list[Company]) -> tuple[list[Company], int]:
        done = self.store.tickers_with_status("done")
        skip = done if self.retry_failed else done | self.store.tickers_with_status("failed")
        resumable = [c for c in companies if c.ticker not in skip]
        already = len(companies) - len(resumable)
        return (resumable[: self.limit] if self.limit is not None else resumable), already

    def _row(self, company: Company, report: QuarterlyReport, stored_path: str, downloaded: int) -> dict[str, Any]:
        """One index row.

        `document_bytes` is what was actually written to disk, so it is 0 for
        an --index-only run; `submission_bytes` is EDGAR's own figure for the
        whole submission and is always present, which is what makes it usable
        for sizing a download before committing to one.
        """
        return {
            "stock_ticker": company.ticker,
            "cik": company.cik10,
            "name": company.name,
            "form": report.form,
            "filing_date": report.filing_date,
            "report_date": report.report_date,
            "accession_number": report.accession,
            "primary_document": report.primary_document,
            "primary_doc_description": report.description,
            "document_url": _filing_url(company.cik10, report.accession, report.primary_document),
            "document_bytes": downloaded,
            "submission_bytes": report.size,
            "stored_path": stored_path,
        }

    def _handle_one(self, company: Company) -> None:
        if self._stopped:
            return
        try:
            row = self._locate(company)
        except DiskFull:
            # One worker hitting the floor stops the whole run: every other
            # worker is about to write to the same volume.
            with self._lock:
                if not self._stopped:
                    self._stopped = True
                    logger.error(
                        "stopping: free disk below %.1f GB. Re-run with --index-only, or free space "
                        "and resume - completed companies are checkpointed.",
                        self.config.min_free_gb,
                    )
            return
        except SkipCompany as exc:
            logger.info("skip %s: %s", company.ticker, exc)
            self.store.mark_failed(company.ticker, company.cik10, company.name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - one company must never kill the run
            logger.exception("unexpected error on %s", company.ticker)
            self.store.mark_failed(company.ticker, company.cik10, company.name, f"unexpected: {exc}")
            return

        for written in (self.output.add(row),):
            for done_row in written:
                self.store.mark_done(
                    done_row["stock_ticker"],
                    done_row["cik"],
                    done_row["name"],
                    done_row["form"],
                    done_row["filing_date"],
                )

    def _locate(self, company: Company) -> dict[str, Any]:
        try:
            submissions = _get_submissions(self.client, self.cache, company.cik10)
        except Exception as exc:  # noqa: BLE001
            raise SkipCompany(f"submissions_fetch_failed: {exc}") from exc

        report = find_quarterly_report(submissions)
        if report is None:
            raise SkipCompany("no_quarterly_report_found")

        if self.index_only:
            return self._row(company, report, "", 0)

        if _free_gb(self.config.data_dir) < self.config.min_free_gb:
            raise DiskFull

        accession_no_dashes = report.accession.replace("-", "")
        content = self.cache.load_filing_document(
            company.cik10, accession_no_dashes, report.primary_document
        )
        if content is None:
            url = _filing_url(company.cik10, report.accession, report.primary_document)
            try:
                response = self.client.get(url)
            except Exception as exc:  # noqa: BLE001
                raise SkipCompany(f"filing_fetch_failed: {exc}") from exc
            content = response.content
            self.cache.save_document(
                company.cik10,
                accession_no_dashes,
                report.primary_document,
                content,
                metadata={
                    "cik": company.cik10,
                    "ticker": company.ticker,
                    "name": company.name,
                    "form": report.form,
                    "accessionNumber": report.accession,
                    "primaryDocument": report.primary_document,
                    "filingDate": report.filing_date,
                    "reportDate": report.report_date,
                    "url": url,
                },
            )

        path = self.cache.filing_dir(company.cik10, accession_no_dashes) / report.primary_document
        return self._row(company, report, str(path), len(content))

    def run(self, companies: list[Company]) -> None:
        pending, already = self._pending(companies)
        total = len(pending)
        logger.info(
            "locating quarterly reports for %d companies (%d already resolved, %s)",
            total,
            already,
            "index only" if self.index_only else f"downloading, {_free_gb(self.config.data_dir):.1f} GB free",
        )

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = [pool.submit(self._handle_one, c) for c in pending]
            for i, future in enumerate(as_completed(futures), start=1):
                future.result()
                if i % 100 == 0 or i == total:
                    logger.info("progress: %d/%d", i, total)

        for done_row in self.output.flush():
            self.store.mark_done(
                done_row["stock_ticker"],
                done_row["cik"],
                done_row["name"],
                done_row["form"],
                done_row["filing_date"],
            )
        self.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N pending companies.")
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated ticker list.")
    parser.add_argument("--retry-failed", action="store_true", help="Re-attempt previously failed companies.")
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Record where each quarterly report is without downloading it. Costs no disk beyond the "
        "index itself; every row still carries a document_url to fetch on demand.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config()
    run = QuarterlyRun(
        config, limit=args.limit, retry_failed=args.retry_failed, index_only=args.index_only
    )
    companies = fetch_company_list(run.client, run.cache)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",")}
        companies = [c for c in companies if c.ticker.upper() in wanted]

    run.run(companies)
    print(f"Quarterly index: {config.quarterly_output_dir}")


if __name__ == "__main__":
    main()
