"""Regression tests for extract.py against real cached filings.

Run with:
    .venv/bin/python tests/test_extraction.py

These fixtures were captured by running the scraper (see README) and are
checked in under data/filings/. Each is a real filing that broke the
extractor at some point, kept as the case that pins the fix. They assert on
the *opening words* of the result, not merely that something came back:
every regression these caught produced a plausible-looking string anchored
on the wrong heading, which a bare non-None check would have waved through.

10-K (extract_10k_business):
  - AAPL, MSFT, NVDA: standard, clean iXBRL 10-Ks.
  - AMZN, SPWR (SunPower): the real "Item 1. Business" heading renders as
    two separate lines/table cells ("Item 1." then "Business" on the next
    line) -> exercises the multi-line heading/boundary matching that a naive
    single-line regex would miss entirely.
  - GS (Goldman Sachs): "Item 1A" recurs every ~10-20 lines for hundreds of
    lines as a running page header throughout the real Risk Factors section
    -> the first boundary after the heading has to be the one taken, without
    being spooked by the repetition.
  - INTC (Intel), MCD (McDonald's): large filers that reorder their 10-K and
    never print a literal "Item 1. Business" heading in the body, only in a
    cross-reference index table mapping their own headings to item numbers
    -> exercises the generic heading tiers. Intel additionally has its only
    "Item 1A" line inside that index at 96% of the way through the document,
    so bounding against it swept up 462K characters of unrelated content
    until the section cap and the soft-boundary fallback were added.
  - C (Citigroup): same style, and its own "Overview" heading recurs a dozen
    times through the filing - under Capital Resources, under Managing
    Global Risk. Its risk-management "Overview" section is 240K characters
    and wins outright on size, so generic tiers take the *earliest*
    substantial match instead. Citi also prints a bare "Business" line on
    its contents page whose "section" is the next few contents entries,
    which is what the lead-prose filter exists to reject.

20-F (extract_20f_business):
  - BABA (Alibaba): standard SEC-style 20-F.
  - ASML: a European "integrated annual report" with no SEC-style heading
    anywhere in the body -> best-effort case, see CONTAINS_CASES.

40-F's Annual Information Form exhibit (extract_aif_business):
  - SNDL, RY (Royal Bank of Canada): standard NI 51-102F2 AIF structure.
    RY's heading splits across lines like AMZN's above.
  - TD (Toronto-Dominion Bank): the filing agent renders body text with one
    div/table cell per *word*, not per paragraph, so the flattened text is
    a column of one- and two-word lines. Left alone the section survives
    boilerplate filtering as a patchwork with words missing mid-sentence -
    worse than an outright failure, since it reads as plausible prose. The
    fix re-joins those fragments, but only in a document that is shredded
    throughout: an earlier version merged any run of short lines anywhere
    and swallowed real body headings ("Item 1. Business" is itself three
    short words), costing filings that had extracted cleanly before.
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

# (extractor, cik10, accession_no_dashes, primary_document, phrase the result
# must start with). The opening phrase, not just "something was returned", is
# what the assertion checks: every historical regression here produced a
# plausible-looking string anchored on the wrong heading, so a bare non-None
# check would have passed for all of them.
CASES = [
    (extract_10k_business, "0000320193", "000032019325000079", "aapl-20250927.htm",
     "The Company designs, manufactures and markets smartphones"),
    (extract_10k_business, "0000789019", "000119312526323660", "msft-20260630.htm",
     "Microsoft is a technology company"),
    (extract_10k_business, "0001045810", "000104581026000021", "nvda-20260125.htm",
     "NVIDIA pioneered accelerated computing"),
    (extract_10k_business, "0001018724", "000101872426000004", "amzn-20251231.htm",
     "We seek to be Earth"),
    (extract_10k_business, "0001838987", "000121390026043623", "ea0283920-10k_sunpower.htm",
     "mission is to deliver energy-efficient solutions"),
    (extract_10k_business, "0000886982", "000088698226000091", "gs-20251231.htm",
     "Goldman Sachs is a leading global financial institution"),
    (extract_10k_business, "0000050863", "000005086326000011", "intc-20251227.htm",
     "We are a global leader in the design and manufacturing of CPUs"),
    (extract_10k_business, "0000831001", "000083100126000011", "c-20251231.htm",
     "Citigroup\u2019s history dates back to the founding of the City Bank of New York"),
    (extract_10k_business, "0000063908", "000006390826000035", "mcd-20251231.htm",
     "The Company franchises and owns and operates McDonald"),
    (extract_20f_business, "0001577552", "000119312526231755", "baba-20260331.htm",
     "Our mission is to make it easy to do business anywhere"),
    (extract_aif_business, "0001766600", "000119312526102524", "sndl-ex99_1.htm",
     "The following describes significant events and conditions"),
    (extract_aif_business, "0001000275", "000119312525305927", "d95203dex1.htm",
     "Our business strategies and actions are guided by our vision"),
    (extract_aif_business, "0000947263", "000156276225000289", "ex991.htm",
     "Three Year History Prior to October 6, 2022, the Bank was a major shareholder"),
]

# Best-effort cases: the filer prints no recognizable heading in the body at
# all, so the search anchors on their own contents-page wording and the
# result opens with front matter (ASML's is its forward-looking-statements
# note) before reaching the business narrative. Asserting on a phrase from
# deep inside the description rather than on its opening keeps these honest:
# the section still has to be in there.
CONTAINS_CASES = [
    (extract_20f_business, "0000937966", "000162828026011378", "asml-20251231.htm",
     "holistic lithography"),
]

# Filings with no recognizable business heading anywhere - not in the body,
# not on the fallback lists - where a result would mean the search guessed.
NO_EXTRACTION_CASES: list[tuple] = []


def run() -> int:
    failures = 0
    for extractor, cik10, accession, primary_document, expected_prefix in CASES:
        path = FILINGS_DIR / cik10 / accession / primary_document
        if not path.exists():
            print(f"SKIP  {primary_document}: fixture not cached at {path}")
            continue

        description = extractor(path.read_bytes())
        label = f"{extractor.__name__} {primary_document}"
        ok = description is not None and description.startswith(expected_prefix)
        status = "PASS" if ok else "FAIL"
        if ok:
            print(f"{status}  {label}: {description[:90]!r} ({len(description)} chars)")
        else:
            print(f"{status}  {label}: expected to start with {expected_prefix!r},"
                  f" got {(description or '')[:120]!r}")
        if not ok:
            failures += 1

    for extractor, cik10, accession, primary_document, phrase in CONTAINS_CASES:
        path = FILINGS_DIR / cik10 / accession / primary_document
        if not path.exists():
            print(f"SKIP  {primary_document}: fixture not cached at {path}")
            continue
        description = extractor(path.read_bytes())
        ok = description is not None and phrase in description
        print(f"{'PASS' if ok else 'FAIL'}  {extractor.__name__} {primary_document}:"
              f" expected to contain {phrase!r} ({len(description or '')} chars)")
        if not ok:
            failures += 1

    for extractor, cik10, accession, primary_document in NO_EXTRACTION_CASES:
        path = FILINGS_DIR / cik10 / accession / primary_document
        if not path.exists():
            print(f"SKIP  {primary_document}: fixture not cached at {path}")
            continue
        description = extractor(path.read_bytes())
        ok = description is None
        print(f"{'PASS' if ok else 'FAIL'}  {extractor.__name__} {primary_document}:"
              f" expected no extraction, got {(description or '')[:90]!r}")
        if not ok:
            failures += 1

    total = len(CASES) + len(CONTAINS_CASES) + len(NO_EXTRACTION_CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
