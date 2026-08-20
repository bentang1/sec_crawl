"""Locate a filing's business-description section and pull its full text.

Three document types are supported, each with its own heading convention and
its own extraction function below, but they share one engine (`_clean_lines`
+ `_extract_section`):

- **10-K** (`extract_10k_business`): "Item 1. Business" through Item 1A/1B/2.
- **20-F** (`extract_20f_business`): foreign private issuers' annual report
  equivalent, "Item 4, B. Business Overview" through the next lettered
  subsection or Item 4A/5.
- **40-F Annual Information Form** (`extract_aif_business`): Canadian MJDS
  filers' 40-F wraps a separately-filed exhibit (the AIF) that has the real
  narrative; its business section is titled "GENERAL DEVELOPMENT OF THE
  BUSINESS" (Canadian AIFs, unlike 10-Ks, use plain ALL-CAPS headings, not
  "Item N") through the next major ALL-CAPS section (e.g. "DIVIDENDS AND
  DISTRIBUTIONS").

Filing HTML is produced by dozens of different filing agents with no shared
template, so none of this relies on a single tag/selector. Instead it
flattens the document to text and works line-by-line:

1. Find every line that *looks* like the section's heading.
2. A filing almost always has two such lines: one in the table of contents
   (immediately followed by the next TOC entry, so ~no content before the
   next boundary line) and one at the real section start (followed by
   substantial prose before the boundary). Whichever candidate has the most
   trailing content is treated as the real one.
3. From there, boilerplate (sub-headings, ALL CAPS lines, forward-looking
   disclaimers, repeated page headers/footers) is filtered out and
   everything else through the boundary is kept.

If no candidate has enough trailing content, each function returns None so
the caller can log a failure instead of guessing.
"""

from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_MIN_SECTION_CHARS = 200  # min trailing content to treat a heading as "real", not TOC
_MIN_RESULT_CHARS = 40  # sanity floor on the final joined text

_BOILERPLATE_LINE_RES = [
    re.compile(r"forward[\s-]looking statements", re.IGNORECASE),
    re.compile(r"forward[\s-]looking information", re.IGNORECASE),
    re.compile(r"safe harbor", re.IGNORECASE),
    re.compile(r"^table of contents$", re.IGNORECASE),
    re.compile(r"^part\s+[ivx]+$", re.IGNORECASE),
    re.compile(r"unless (the )?context (otherwise )?(requires|indicates)", re.IGNORECASE),
    re.compile(r"^\$?\s*[\d,.\s]+$"),  # stray numbers/currency (financial tables)
    re.compile(r"form\s*10-k\s*\|\s*\d+\s*$", re.IGNORECASE),  # repeated page header/footer
]

# Only break lines at block-level tag boundaries, not every tag boundary.
# get_text(sep) inserts `sep` between *every* tag's text, which shreds inline
# runs like "<b>iPhone</b> is the Company's line..." into separate one-word
# lines that then look like noise and get filtered out. Block-level breaks
# keep a full sentence spanning several inline tags on one line.
_BLOCK_TAGS = (
    "p", "div", "li", "tr", "td", "th", "br",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr",
)


def _clean_lines(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()

    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    raw_text = soup.get_text("")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.split("\n")]
    return [line for line in lines if line]


def _is_boilerplate(line: str) -> bool:
    if len(line) < 25:
        return True
    if line.isupper():
        return True
    return any(pattern.search(line) for pattern in _BOILERPLATE_LINE_RES)


_HEADING_WINDOW = 3  # some filers split a heading's label and title across
# separate lines/table cells - e.g. Amazon's real "Item 1. Business" heading
# renders as two lines, "Item 1." then "Business", not one. Joining up to
# this many consecutive lines catches that without much false-positive risk:
# a spurious window match just yields another (probably short/low-quality)
# section candidate, which the trailing-content and quality checks below
# already have to filter out anyway.


def _match_at(lines: list[str], start: int, regex: re.Pattern) -> int | None:
    """If a window of 1.._HEADING_WINDOW lines starting at `start` matches
    regex (joined with a space), returns the index of the last line in that
    window; otherwise None. Some filers split a heading's or boundary's
    label and title across separate lines/table cells - e.g. Amazon's real
    "Item 1. Business" heading renders as two lines, "Item 1." then
    "Business", not one, and the same filer splits "Item 1A. Risk Factors"
    the same way. A spurious window match just yields another (probably
    short/low-quality) section candidate, which the trailing-content and
    quality checks below already have to filter out anyway.
    """
    n = len(lines)
    for window in range(1, _HEADING_WINDOW + 1):
        if start + window > n:
            break
        if regex.match(" ".join(lines[start : start + window])):
            return start + window - 1
    return None


def _find_heading_ends(lines: list[str], heading_re: re.Pattern) -> list[int]:
    """Returns, for each heading match, the index of the last line it
    consumed (content starts at that index + 1)."""
    ends = []
    for i in range(len(lines)):
        end = _match_at(lines, i, heading_re)
        if end is not None:
            ends.append(end)
    return ends


def _boundary_match_at(lines: list[str], start: int, regex: re.Pattern) -> tuple[int, str] | None:
    """Like _match_at, but also returns the matched text (lowercased,
    stripped) so nearby matches can be compared by *identity*, not just
    presence - see _is_clustered_match.
    """
    n = len(lines)
    for window in range(1, _HEADING_WINDOW + 1):
        if start + window > n:
            break
        joined = " ".join(lines[start : start + window])
        m = regex.match(joined)
        if m:
            # group(1) is just the matched alternative (e.g. "1a" out of
            # "1a|1b|2") - using it instead of the full match (group(0))
            # means "Item 1A" and "ITEM 1A. RISK FACTORS" compare as the
            # same identity despite differing trailing text.
            identity = m.group(1) if m.groups() else m.group(0)
            return start + window - 1, identity.strip().lower()
    return None


_CLUSTER_WINDOW = 20


def _is_clustered_match(lines: list[str], match_end: int, own_text: str, regex: re.Pattern) -> bool:
    """A genuine section boundary (e.g. a real "Item 1A. Risk Factors"
    heading) is followed by substantial prose before the *next, different*
    item's boundary - a real Item 1A to Item 1B gap is pages, not lines. A
    cross-reference table lists several *different* item numbers in quick
    succession with nothing but page references between them (seen with
    McDonald's during testing: "Item 1A" / "Item 1B" / "Item 2" all within 9
    lines of each other, nowhere else in the document) - detected here by a
    nearby match whose text differs from this one's.

    Comparing by identity (not just presence) matters because some filers
    legitimately repeat the *same* heading as a running page header
    throughout a many-page section (seen with Microsoft: "Item 1A" recurring
    every ~10-20 lines for hundreds of lines through the real Risk Factors
    section) - that repetition alone isn't a cross-reference table and
    shouldn't be rejected.
    """
    lo = max(0, match_end - _CLUSTER_WINDOW)
    hi = min(len(lines), match_end + _CLUSTER_WINDOW + 1)
    k = lo
    while k < hi:
        result = _boundary_match_at(lines, k, regex)
        if result is not None:
            m, text = result
            if m != match_end and text != own_text:
                return True
            k = m + 1
        else:
            k += 1
    return False


def _extract_section(
    lines: list[str],
    heading_re: re.Pattern,
    boundary_re: re.Pattern,
    max_heading_index: int | None = None,
    require_boundary: bool = True,
) -> str | None:
    heading_indices = _find_heading_ends(lines, heading_re)
    if max_heading_index is not None:
        heading_indices = [i for i in heading_indices if i <= max_heading_index]
    if not heading_indices:
        return None

    # Pass 1: pick which heading candidate is the real one, using the FIRST
    # boundary found after each (regardless of clustering). A TOC/
    # cross-reference-table heading entry is followed by minimal content
    # before the very next item-like text; the real body heading is followed
    # by substantial content - so this simple "most trailing content"
    # ranking reliably tells them apart even though the boundary it lands on
    # might not be the semantically correct place to actually stop (that's
    # refined in pass 2, only for whichever candidate wins here). Applying
    # cluster-skipping in this ranking pass instead would let a TOC
    # candidate's search "leak through" its own small cluster and latch onto
    # the same far-away real boundary the genuine candidate uses, making the
    # TOC candidate's span look artificially larger than the real one.
    best_start: int | None = None
    best_len = 0
    for start in heading_indices:
        end = len(lines)
        j = start + 1
        while j < len(lines):
            if _match_at(lines, j, boundary_re) is not None:
                end = j
                break
            j += 1
        section_len = sum(len(line) for line in lines[start + 1 : end])
        if section_len > best_len:
            best_len = section_len
            best_start = start

    if best_start is None or best_len < _MIN_SECTION_CHARS:
        return None

    # Pass 2: refine the winning candidate's boundary, skipping past any
    # cross-reference-table cluster (several item numbers in quick
    # succession, with nothing but page references between them - seen with
    # McDonald's during testing) to find a genuine, isolated boundary.
    # Finding *a* heading doesn't guarantee the filer's document has a real,
    # findable boundary after it (e.g. Item 1A never appears as a real body
    # heading, only inside such a table) - silently keeping "everything from
    # the heading to EOF" in that case risks sweeping in risk factors,
    # financial statements, and the exhibit index, which is what the old
    # single-pass version did for McDonald's (a 240K-character result
    # spanning nearly the entire filing).
    end = len(lines)
    boundary_found = False
    j = best_start + 1
    while j < len(lines):
        result = _boundary_match_at(lines, j, boundary_re)
        if result is not None:
            m, text = result
            if _is_clustered_match(lines, m, text, boundary_re):
                j = m + 1
                continue
            end = j
            boundary_found = True
            break
        j += 1

    if require_boundary and not boundary_found:
        return None

    best_section = lines[best_start + 1 : end]
    best_len = sum(len(line) for line in best_section)  # recompute: pass 2 may have moved the boundary
    if best_len < _MIN_SECTION_CHARS:
        return None

    content_lines = [line for line in best_section if not _is_boilerplate(line)]
    if not content_lines:
        return None

    # Some filing agents render body text with one <div>/table cell per
    # word instead of per paragraph (seen in both a 10-K and a 40-F AIF
    # during testing). That defeats the block-tag line grouping in
    # _clean_lines: most "lines" become isolated short words, which
    # _is_boilerplate correctly filters out one at a time, but the surviving
    # text is then a disconnected patchwork with words silently missing
    # mid-sentence - worse than an outright failure, since it reads as
    # plausible prose. A well-formed section keeps ~98%+ of its characters
    # after boilerplate filtering (short sub-headings are a small fraction
    # of total text); a fragmented one loses the majority. Below this
    # threshold, the section is too shredded to trust.
    kept_chars = sum(len(line) for line in content_lines)
    if kept_chars / best_len < 0.7:
        return None

    text = " ".join(content_lines)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) < _MIN_RESULT_CHARS:
        return None
    return text


# -- 10-K: "Item 1. Business" through Item 1A/1B/2 ---------------------------

_HEADING_10K_RE = re.compile(r"^item\s+1\.?\s*[:\-–—]?\s*business\b", re.IGNORECASE)
_BOUNDARY_10K_RE = re.compile(r"^item\s+(1a|1b|2)\b", re.IGNORECASE)

# Tried only if the strict "Item 1. Business" heading isn't found anywhere -
# some filers (e.g. Intel, Citigroup) reorder their 10-K under entirely
# custom headings like "Overview" or "Our Business" and never print the
# formal heading in the body at all (only in a cross-reference table). This
# list deliberately excludes generic single-word headings like "Overview"
# or "Our Business": those also commonly appear as subheadings inside the
# unrelated MD&A section later in the same document, and a false match
# there would silently return the wrong content instead of a clean failure.
# These four are specific enough to be low-risk.
_HEADING_10K_FALLBACK_RES = [
    re.compile(r"^general development of (the )?business\b", re.IGNORECASE),
    re.compile(r"^description of (the |our )?business\b", re.IGNORECASE),
    re.compile(r"^nature of (the |our )?business\b", re.IGNORECASE),
    re.compile(r"^business overview\b", re.IGNORECASE),
]

# Fallback headings aren't anchored to "Item 1", so without a positional
# check a stray match late in the document - e.g. inside a cross-reference
# table, which lists these same phrases as topic labels pointing at page
# numbers - could be treated as a heading with nothing sensible to bound
# against. Item 1 always appears in the first half of a 10-K, well before
# Item 7 (MD&A) and any trailing cross-reference table, so matches past
# that point are rejected rather than risking a runaway/wrong section.
_FALLBACK_POSITION_LIMIT_RATIO = 0.5


def extract_10k_business(html: bytes) -> str | None:
    lines = _clean_lines(html)
    result = _extract_section(lines, _HEADING_10K_RE, _BOUNDARY_10K_RE)
    if result is not None:
        return result

    max_heading_index = int(len(lines) * _FALLBACK_POSITION_LIMIT_RATIO)
    for fallback_re in _HEADING_10K_FALLBACK_RES:
        result = _extract_section(lines, fallback_re, _BOUNDARY_10K_RE, max_heading_index=max_heading_index)
        if result is not None:
            return result
    return None


# -- 20-F: "Item 4, B. Business Overview" through C./Item 4A/Item 5 ---------

_HEADING_20F_RE = re.compile(r"^b\.\s*business overview\b", re.IGNORECASE)
_BOUNDARY_20F_RE = re.compile(
    r"^(c\.\s*organi[sz]ational structure|item\s+4a\b|item\s+5\b)", re.IGNORECASE
)


def extract_20f_business(html: bytes) -> str | None:
    return _extract_section(_clean_lines(html), _HEADING_20F_RE, _BOUNDARY_20F_RE)


# -- 40-F's Annual Information Form exhibit: "GENERAL DEVELOPMENT OF THE
# BUSINESS" through the next major ALL-CAPS AIF section. Canadian AIFs
# follow a fairly consistent template (National Instrument 51-102F2), so the
# section names that typically follow are hardcoded here rather than
# guessed at.

_HEADING_AIF_RE = re.compile(r"^general development of the business\b", re.IGNORECASE)
_BOUNDARY_AIF_RE = re.compile(
    r"^(dividends?( and distributions)?|description of capital structure|"
    r"market for securities|risk factors|capitalization|escrowed securities|"
    r"directors and officers)\b",
    re.IGNORECASE,
)


def extract_aif_business(html: bytes) -> str | None:
    return _extract_section(_clean_lines(html), _HEADING_AIF_RE, _BOUNDARY_AIF_RE)
