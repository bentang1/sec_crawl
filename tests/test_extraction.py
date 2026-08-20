"""Regression tests for extract.py against real cached filings.

Run with:
    .venv/bin/python tests/test_extraction.py

These fixtures were captured by running the scraper (see README) and are
checked in under data/filings/. They cover the fragility patterns found
while validating this script against real EDGAR data across all three
supported form types:

10-K (extract_10k_business):
  - AAPL, MSFT, NVDA: standard, clean iXBRL 10-Ks -> should extract cleanly.
  - AMZN, SPWR (SunPower): the real "Item 1. Business" heading renders as
    two separate lines/table cells ("Item 1." then "Business" on the next
    line), not one -> exercises the multi-line heading/boundary matching
    that a naive single-line regex would miss entirely.
  - GS (Goldman Sachs): "Item 1A" recurs every ~10-20 lines for hundreds of
    lines as a running page header throughout the real Risk Factors section
    -> exercises the identity-aware clustering check (same text repeating
    nearby is fine; only a *different* item number nearby signals a
    cross-reference table) without which this would have been wrongly
    rejected as clustered.
  - INTC (Intel), C (Citigroup): large filers that reorder their 10-K and
    never print a literal "Item 1. Business" heading in the body (only in a
    cross-reference index table mapping their own headings, e.g. "Overview"
    / "Our Business", to item numbers) -> heading is never found; must fail
    rather than guess which custom heading counts.
  - MCD (McDonald's): same style as Intel/Citigroup, but here the fallback
    heading list (tried when the strict "Item 1. Business" match fails)
    does find an early, legitimate-looking heading - the failure has to
    come from recognizing that "Item 1A" only ever appears inside the
    cross-reference table (several different item numbers within 9 lines of
    each other, nowhere else in the document), not from heading detection.
    Before this was caught, extraction silently kept everything from that
    heading to end-of-document (240K characters spanning nearly the entire
    filing).

20-F (extract_20f_business):
  - BABA (Alibaba): standard SEC-style 20-F -> extracts cleanly.
  - ASML: uses the same "own headings + cross-reference table" style as
    Intel above (a European "integrated annual report") -> must fail.

40-F's Annual Information Form exhibit (extract_aif_business):
  - SNDL, RY (Royal Bank of Canada): standard NI 51-102F2 AIF structure ->
    extract cleanly. SNDL also exercises the fetch: the exhibit convention
    is EX-99.1. RY's heading also splits across lines like AMZN's above.
  - TD (Toronto-Dominion Bank): filing agent renders body text with one
    div/table cell per *word*, not per paragraph -> the heading is found
    but the surviving content after boilerplate-filtering is a
    disconnected patchwork with words silently missing mid-sentence. A
    quality gate (kept-content ratio after boilerplate filtering) rejects
    this rather than emitting a garbled result.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edgar_scraper.extract import (  # noqa: E402
    extract_10k_business,
    extract_20f_business,
    extract_aif_business,
)

FILINGS_DIR = Path(__file__).resolve().parent.parent / "data" / "filings"

# (extractor, cik10, accession_no_dashes, primary_document, expected outcome)
CASES = [
    (extract_10k_business, "0000320193", "000032019325000079", "aapl-20250927.htm", "success"),
    (extract_10k_business, "0000789019", "000119312526323660", "msft-20260630.htm", "success"),
    (extract_10k_business, "0001045810", "000104581026000021", "nvda-20260125.htm", "success"),
    (extract_10k_business, "0001018724", "000101872426000004", "amzn-20251231.htm", "success"),
    (extract_10k_business, "0001838987", "000121390026043623", "ea0283920-10k_sunpower.htm", "success"),
    (extract_10k_business, "0000886982", "000088698226000091", "gs-20251231.htm", "success"),
    (extract_10k_business, "0000050863", "000005086326000011", "intc-20251227.htm", "fail"),
    (extract_10k_business, "0000831001", "000083100126000011", "c-20251231.htm", "fail"),
    (extract_10k_business, "0000063908", "000006390826000035", "mcd-20251231.htm", "fail"),
    (extract_20f_business, "0001577552", "000119312526231755", "baba-20260331.htm", "success"),
    (extract_20f_business, "0000937966", "000162828026011378", "asml-20251231.htm", "fail"),
    (extract_aif_business, "0001766600", "000119312526102524", "sndl-ex99_1.htm", "success"),
    (extract_aif_business, "0001000275", "000119312525305927", "d95203dex1.htm", "success"),
    (extract_aif_business, "0000947263", "000156276225000289", "ex991.htm", "fail"),
]


def run() -> int:
    failures = 0
    for extractor, cik10, accession, primary_document, expected in CASES:
        path = FILINGS_DIR / cik10 / accession / primary_document
        if not path.exists():
            print(f"SKIP  {primary_document}: fixture not cached at {path}")
            continue

        html = path.read_bytes()
        description = extractor(html)
        label = f"{extractor.__name__} {primary_document}"

        if expected == "success":
            ok = description is not None and len(description) >= 40
            status = "PASS" if ok else "FAIL"
            preview = (description or "")[:90]
            print(f"{status}  {label}: {preview!r}")
        else:
            ok = description is None
            status = "PASS" if ok else "FAIL"
            print(f"{status}  {label}: expected no extraction, got {description!r}")

        if not ok:
            failures += 1

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
