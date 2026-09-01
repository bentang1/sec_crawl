"""Orchestrates: company_tickers.json -> submissions -> annual report -> business text.

Each company's most recent annual report is located in strict priority
order: 10-K, then 20-F (foreign private issuers), then 40-F (Canadian MJDS
filers), then their amended variants (10-K/A, 20-F/A, 40-F/A) as a fallback
for companies with no plain filing of any of the three types. Whichever
form is found first in that order is used, even if a lower-priority form is
more recently filed (e.g. a company with an older 20-F and a newer 40-F
still uses the 20-F).

10-K and 20-F carry their business-description text directly in the
primary filed document. 40-F is different: the primary document is just a
cover-page wrapper, and the real narrative lives in a separately-filed
exhibit (the Annual Information Form, conventionally EX-99.1) that has to
be located via the filing's own document index first.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from .cache import FilingCache
from .checkpoint import CheckpointStore
from .client import EdgarClient
from .config import Config
from .extract import extract_10k_business, extract_20f_business, extract_aif_business
from .output import PART_GLOB, ParquetOutput

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_document}"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{accession}-index.html"

# Strict priority order: first form type the company has ever filed wins,
# regardless of whether a lower-priority form is more recently filed.
FORM_PRIORITY = ["10-K", "20-F", "40-F", "10-K/A", "20-F/A", "40-F/A"]

_DIRECT_EXTRACTORS = {
    "10-K": extract_10k_business,
    "10-K/A": extract_10k_business,
    "20-F": extract_20f_business,
    "20-F/A": extract_20f_business,
}
_AIF_FORMS = {"40-F", "40-F/A"}

# Exhibit "Type" values (from the filing index) that conventionally hold a
# 40-F's Annual Information Form, most-likely first. The convention varies by
# filer - most use EX-99.1, but e.g. Royal Bank of Canada uses plain EX-1.
_AIF_EXHIBIT_TYPES = [
    "EX-99.1", "EX-99.A", "EX-99(A)", "EX-99A", "EX-99", "EX-1", "EX-1.1",
    "EX-99.2", "EX-99.3", "EX-99.B", "EX-2", "EX-2.1",
]

# Exhibit descriptions that rule a document out as the AIF. The exhibit
# numbering is only a convention, and a filer who puts something else at
# EX-99.1 (Ballard Power files its financial statements there) would
# otherwise have the wrong document handed to the extractor.
_NON_AIF_DESCRIPTION_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"financial statements", r"management'?s discussion", r"\bmd&a\b",
        r"consent", r"certification", r"auditors?'? report", r"cover page",
        r"interactive data", r"press release", r"news release",
    )
]

# A 40-F filing index can list a dozen exhibits; trying them all would
# mean a dozen SEC round-trips for every company whose AIF is not where
# it is expected. The ranking puts the real one first in almost every
# case, so only the top few are worth downloading.
_MAX_AIF_CANDIDATES = 4

_AIF_DESCRIPTION_RE = re.compile(r"annual information form|\bAIF\b", re.IGNORECASE)


class SkipCompany(Exception):
    """Raised for any per-company condition that should be logged and skipped."""


@dataclass
class Company:
    ticker: str
    cik10: str
    name: str


@dataclass
class AnnualReportResult:
    form: str
    description: str


def fetch_company_list(client: EdgarClient, cache: FilingCache) -> list[Company]:
    cached = cache.load_company_tickers()
    if cached is None:
        response = client.get(TICKERS_URL)
        cached = response.json()
        cache.save_company_tickers(cached)

    companies = []
    for entry in cached.values():
        ticker = entry.get("ticker")
        cik = entry.get("cik_str")
        name = entry.get("title")
        if not ticker or cik is None:
            continue
        companies.append(Company(ticker=ticker, cik10=str(cik).zfill(10), name=name or ""))
    return companies


def _get_submissions(client: EdgarClient, cache: FilingCache, cik10: str) -> dict[str, Any]:
    cached = cache.load_submissions(cik10)
    if cached is not None:
        return cached
    response = client.get(SUBMISSIONS_URL.format(cik10=cik10))
    data = response.json()
    cache.save_submissions(cik10, data)
    return data


def _latest_filing(submissions: dict[str, Any], form: str) -> tuple[str, str, str] | None:
    """Returns (accessionNumber, primaryDocument, filingDate) for the most
    recent filing of the given form type, or None if there isn't one.
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    dates = recent.get("filingDate", [])

    for i, form_type in enumerate(forms):
        if form_type == form:
            return accessions[i], docs[i], dates[i]
    return None


def _find_annual_report(submissions: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Returns (form, accessionNumber, primaryDocument, filingDate) for the
    highest-priority annual report the company has on file, or None.
    """
    for form in FORM_PRIORITY:
        result = _latest_filing(submissions, form)
        if result and result[1]:  # has a primaryDocument
            accession, primary_document, filing_date = result
            return form, accession, primary_document, filing_date
    return None


def _filing_url(cik10: str, accession: str, filename: str) -> str:
    return FILING_URL.format(
        cik_int=int(cik10),
        accession_no_dashes=accession.replace("-", ""),
        primary_document=filename,
    )


def _fetch_document(
    client: EdgarClient, cache: FilingCache, company: Company, form: str, accession: str, filename: str
) -> bytes:
    accession_no_dashes = accession.replace("-", "")
    content = cache.load_filing_document(company.cik10, accession_no_dashes, filename)
    if content is not None:
        return content

    url = _filing_url(company.cik10, accession, filename)
    try:
        response = client.get(url)
    except Exception as exc:  # noqa: BLE001
        raise SkipCompany(f"filing_fetch_failed: {exc}") from exc
    content = response.content
    cache.save_filing_document(
        company.cik10,
        accession_no_dashes,
        filename,
        content,
        metadata={
            "cik": company.cik10,
            "ticker": company.ticker,
            "name": company.name,
            "form": form,
            "accessionNumber": accession,
            "primaryDocument": filename,
            "url": url,
        },
    )
    return content


def _find_aif_exhibit_filenames(index_html: bytes) -> list[str]:
    """Filenames from the filing index that might be the Annual Information
    Form, best guess first.

    A list rather than a single name because neither signal is conclusive on
    its own: the description is authoritative when present but most filers
    leave it generic, and the exhibit number is only a convention. The caller
    tries them in order and keeps the first one the extractor can read.
    """
    soup = BeautifulSoup(index_html, "lxml")
    table = soup.find("table", class_="tableFile")
    if table is None:
        return []

    rows = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[2].find("a")
        if not link or not link.get("href"):
            continue
        filename = link["href"].rsplit("/", 1)[-1]
        if not filename.lower().endswith((".htm", ".html", ".txt")):
            continue
        rows.append((cells[1].get_text(" ", strip=True), cells[3].get_text(strip=True).upper(), filename))

    def rank(row: tuple[str, str, str]) -> int:
        description, doc_type, _ = row
        # An explicit "Annual Information Form" description wins outright
        # (e.g. Royal Bank of Canada labels it "EX-1 ANNUAL INFORMATION
        # FORM"); a description naming some other document loses outright.
        if _AIF_DESCRIPTION_RE.search(description):
            return -1
        if any(pattern.search(description) for pattern in _NON_AIF_DESCRIPTION_RES):
            return len(_AIF_EXHIBIT_TYPES) + 1
        return _AIF_EXHIBIT_TYPES.index(doc_type) if doc_type in _AIF_EXHIBIT_TYPES else len(_AIF_EXHIBIT_TYPES)

    plausible = [row for row in rows if rank(row) <= len(_AIF_EXHIBIT_TYPES)]
    return [filename for _, _, filename in sorted(plausible, key=rank)]


def _process_40f(
    company: Company, client: EdgarClient, cache: FilingCache, form: str, accession: str
) -> AnnualReportResult:
    accession_no_dashes = accession.replace("-", "")
    index_filename = f"{accession}-index.html"
    index_html = cache.load_filing_document(company.cik10, accession_no_dashes, index_filename)
    if index_html is None:
        url = FILING_INDEX_URL.format(cik_int=int(company.cik10), accession_no_dashes=accession_no_dashes, accession=accession)
        try:
            response = client.get(url)
        except Exception as exc:  # noqa: BLE001
            raise SkipCompany(f"40f_index_fetch_failed: {exc}") from exc
        index_html = response.content
        cache.save_filing_document(
            company.cik10, accession_no_dashes, index_filename, index_html,
            metadata={"cik": company.cik10, "form": form, "kind": "filing_index", "url": url},
        )

    exhibit_filenames = _find_aif_exhibit_filenames(index_html)
    if not exhibit_filenames:
        raise SkipCompany("40f_aif_exhibit_not_found")

    # Which exhibit holds the AIF is a convention, not a rule, so work down
    # the ranked candidates and keep the first one a business section can
    # actually be read out of. A candidate that fails to fetch is skipped
    # rather than failing the company, since a later one may still work.
    for filename in exhibit_filenames[:_MAX_AIF_CANDIDATES]:
        try:
            content = _fetch_document(client, cache, company, form, accession, filename)
        except SkipCompany:
            continue
        try:
            description = extract_aif_business(content)
        except Exception as exc:  # noqa: BLE001 - malformed HTML shouldn't crash the run
            raise SkipCompany(f"extraction_error: {exc}") from exc
        if description:
            return AnnualReportResult(form=form, description=description)

    raise SkipCompany(f"business_section_not_found ({form})")


def _process_company(company: Company, client: EdgarClient, cache: FilingCache) -> AnnualReportResult:
    try:
        submissions = _get_submissions(client, cache, company.cik10)
    except Exception as exc:  # noqa: BLE001 - network/parsing errors all become skips
        raise SkipCompany(f"submissions_fetch_failed: {exc}") from exc

    found = _find_annual_report(submissions)
    if found is None:
        raise SkipCompany("no_annual_report_found")
    form, accession, primary_document, _filing_date = found

    if form in _AIF_FORMS:
        return _process_40f(company, client, cache, form, accession)

    content = _fetch_document(client, cache, company, form, accession, primary_document)

    extractor = _DIRECT_EXTRACTORS[form]
    try:
        description = extractor(content)
    except Exception as exc:  # noqa: BLE001 - malformed HTML shouldn't crash the run
        raise SkipCompany(f"extraction_error: {exc}") from exc

    if not description:
        raise SkipCompany(f"business_section_not_found ({form})")

    return AnnualReportResult(form=form, description=description)


def export_checkpoint_to_parquet(config: Config) -> tuple[int, int]:
    """Rebuilds `data/output/` from the checkpoint. Returns (rows, bytes).

    The checkpoint holds every description already, so this both migrates the
    results of runs that predate the Parquet output and rebuilds the output
    after a change to extraction, without re-reading a single filing.
    """
    checkpoint = CheckpointStore(config.checkpoint_db_path)
    output = ParquetOutput(
        config.output_dir,
        batch_size=config.output_batch_size,
        compression=config.parquet_compression,
        compression_level=config.parquet_compression_level,
    )
    rows = 0
    try:
        for batch in checkpoint.iter_done(config.output_batch_size):
            for ticker, cik, name, form, description in batch:
                output.add(
                    {
                        "stock_ticker": ticker,
                        "cik": cik or "",
                        "name": name or "",
                        "filing_type": form or "",
                        "description": description,
                    }
                )
            rows += len(batch)
            logger.info("exported %d rows", rows)
        output.flush()
    finally:
        checkpoint.close()
    written = sum(p.stat().st_size for p in config.output_dir.glob(PART_GLOB))
    return rows, written


class ScraperRun:
    def __init__(self, config: Config, retry_failed: bool = False, limit: int | None = None):
        self.config = config
        self.client = EdgarClient(config)
        self.cache = FilingCache(config)
        self.checkpoint = CheckpointStore(config.checkpoint_db_path)
        self.retry_failed = retry_failed
        self.limit = limit
        self._csv_lock = threading.Lock()
        self.output = ParquetOutput(
            config.output_dir,
            batch_size=config.output_batch_size,
            compression=config.parquet_compression,
            compression_level=config.parquet_compression_level,
        )
        self._ensure_csv_headers()

    def _ensure_csv_headers(self) -> None:
        # Failures stay CSV: the whole file is under 200KB, and being able to
        # read it with `less` while a run is going matters more than its size.
        if not self.config.failure_csv_path.exists():
            with open(self.config.failure_csv_path, "w", newline="") as f:
                csv.writer(f).writerow(["stock_ticker", "cik", "name", "reason"])

    def _record_done(self, rows: list[dict[str, Any]]) -> None:
        """Checkpoint companies whose descriptions are now durable on disk.

        Marking `done` has to come *after* the Parquet part is written, not
        before. The checkpoint is what a resumed run skips on, so a company
        marked done whose row never reached disk would be skipped forever and
        silently missing from the output. In the other order the worst case is
        a company extracted twice, which costs nothing - the filing is already
        cached, so the retry never touches the network.
        """
        for row in rows:
            self.checkpoint.mark_done(
                row["stock_ticker"],
                row["cik"],
                row["name"],
                row["filing_type"],
                row["description"],
            )

    def _append_failure(self, ticker: str, cik: str, name: str, reason: str) -> None:
        with self._csv_lock:
            with open(self.config.failure_csv_path, "a", newline="") as f:
                csv.writer(f).writerow([ticker, cik, name, reason])

    def _pending_companies(self, companies: list[Company]) -> tuple[list[Company], int]:
        """Returns (companies to process this run, count already resolved)."""
        # Default: skip anything already resolved (done or failed) so a
        # resumed run doesn't re-hit companies whose outcome is already
        # known. --retry-failed additionally re-attempts the failed ones.
        done = self.checkpoint.tickers_with_status("done")
        skip = done if self.retry_failed else done | self.checkpoint.tickers_with_status("failed")
        resumable = [c for c in companies if c.ticker not in skip]
        already_resolved = len(companies) - len(resumable)
        pending = resumable[: self.limit] if self.limit is not None else resumable
        return pending, already_resolved

    def _handle_one(self, company: Company) -> None:
        try:
            outcome = _process_company(company, self.client, self.cache)
        except SkipCompany as exc:
            logger.info("skip %s: %s", company.ticker, exc)
            self.checkpoint.mark_failed(company.ticker, company.cik10, company.name, str(exc))
            self._append_failure(company.ticker, company.cik10, company.name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never let one company crash the run
            logger.exception("unexpected error on %s", company.ticker)
            self.checkpoint.mark_failed(company.ticker, company.cik10, company.name, f"unexpected: {exc}")
            self._append_failure(company.ticker, company.cik10, company.name, f"unexpected: {exc}")
            return

        written = self.output.add(
            {
                "stock_ticker": company.ticker,
                "cik": company.cik10,
                "name": company.name,
                "filing_type": outcome.form,
                "description": outcome.description,
            }
        )
        self._record_done(written)

    def run(self, companies: list[Company]) -> None:
        pending, already_resolved = self._pending_companies(companies)
        total = len(pending)
        deferred_by_limit = len(companies) - already_resolved - total
        logger.info(
            "processing %d companies (%d already resolved, %d deferred by --limit)",
            total,
            already_resolved,
            deferred_by_limit,
        )

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = [pool.submit(self._handle_one, c) for c in pending]
            for i, future in enumerate(as_completed(futures), start=1):
                future.result()  # re-raise unexpected exceptions from the worker itself
                if i % 100 == 0 or i == total:
                    logger.info("progress: %d/%d", i, total)

        self._record_done(self.output.flush())
        self.checkpoint.close()
