"""Locate a filing's business-description section and pull its full text.

Three document types are supported, each with its own heading conventions and
its own extraction function below, but they share one engine (`_clean_lines`
+ `_extract_section`):

- **10-K** (`extract_10k_business`): "Item 1. Business" through Item 1A/1B/1C/2.
- **20-F** (`extract_20f_business`): foreign private issuers' annual report
  equivalent, "Item 4, B. Business Overview" through the next lettered
  subsection or Item 4A/5.
- **40-F Annual Information Form** (`extract_aif_business`): Canadian MJDS
  filers' 40-F wraps a separately-filed exhibit (the AIF) that has the real
  narrative; its business section is conventionally titled "GENERAL
  DEVELOPMENT OF THE BUSINESS" (Canadian AIFs, unlike 10-Ks, use plain
  ALL-CAPS headings, not "Item N") through the next major AIF section (e.g.
  "DIVIDENDS AND DISTRIBUTIONS").

Filing HTML is produced by dozens of different filing agents with no shared
template, so none of this relies on a single tag/selector. Instead it
flattens the document to text and works line-by-line:

1. Find every line that *looks* like the section's heading, working through
   `HEADING_TIERS` from the form's canonical heading down to increasingly
   generic ones ("Business Overview", "Our Business", "Overview"). Only the
   first tier that yields a usable section is used, so a generic heading is
   never preferred over the canonical one in a filing that has both.
2. A filing almost always has two lines matching a given tier: one in the
   table of contents (immediately followed by the next TOC entry, so ~no
   content before the next boundary line) and one at the real section start
   (followed by substantial prose before the boundary). Whichever candidate
   has the most trailing content is treated as the real one.
3. The winning candidate's end is then refined: the first boundary with at
   least `_MIN_SECTION_CHARS` of content in front of it. Requiring content
   in front skips a cross-reference table sitting right after the heading
   (which lists several item numbers with only page references between
   them) without rejecting a genuinely short run of boundaries, e.g. a
   smaller reporting company whose Item 1A/1B/2 are one line each.
4. If no such boundary exists - or the one found is so far away that the
   section is implausibly long - a looser text-based boundary list ("Risk
   Factors", "Properties", "Management's Discussion...") is tried, and
   failing that the section runs to `_MAX_SECTION_CHARS`, so a missing
   boundary degrades to a long-but-usable description instead of nothing.
   Best-effort is only reached after *every* heading tier has been tried
   with a real boundary required.
5. Finally, boilerplate (sub-headings, ALL CAPS lines, forward-looking
   disclaimers, repeated page headers/footers) is filtered out.

If no tier yields a section, each function returns None so the caller can
log a failure instead of guessing.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_MIN_SECTION_CHARS = 200  # min trailing content to treat a heading as "real", not TOC
# Sanity floor on the final joined text. A real business section runs to
# tens of thousands of characters - only 1 of 5,679 known-good extractions
# came in under this - so anything shorter is a contents-page fragment or a
# stub, not a description.
_MIN_RESULT_CHARS = 400

# Ceiling on a section's length, and the point past which a structural
# boundary stops being believable. Intel's 10-K prints no Item 1A heading in
# the body at all - the only one is in a cross-reference index at 96% of the
# way through the document, so an otherwise-correct "Our Business" heading
# bounded against it swept up 462K characters of risk factors, MD&A and
# financial statements. Past this size the looser title-based boundaries are
# consulted as well and the earlier of the two wins; whatever survives is
# then truncated here. Chosen from the observed distribution of cleanly
# bounded sections, whose 99th percentile is ~235K characters, so genuine
# sections are left whole.
_MAX_SECTION_CHARS = 300_000

_BOILERPLATE_LINE_RES = [
    re.compile(r"forward[\s-]looking statements", re.IGNORECASE),
    re.compile(r"forward[\s-]looking information", re.IGNORECASE),
    re.compile(r"safe harbor", re.IGNORECASE),
    re.compile(r"^table of contents$", re.IGNORECASE),
    re.compile(r"^part\s+[ivx]+$", re.IGNORECASE),
    re.compile(r"unless (the )?context (otherwise )?(requires|indicates)", re.IGNORECASE),
    re.compile(r"^\$?\s*[\d,.\s]+$"),  # stray numbers/currency (financial tables)
    re.compile(r"form\s*10-k\s*\|\s*\d+\s*$", re.IGNORECASE),  # repeated page header/footer
    re.compile(r"^-\s*\d+\s*-$"),  # "- 3 -" page markers
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

# An inline-XBRL filing carries a machine-readable fact set in the document
# alongside the human-readable text. Filing agents put it either in an
# <ix:header> block or in a hidden container, and its contents are context
# ids, taxonomy references and raw values ("us-gaap:SubsequentEventMember",
# "0001011509", "2026-02-26"). Left in, that is thousands of junk lines
# ahead of the real body - seen with Golden Minerals, where it displaced the
# entire document as far as line-based heading detection was concerned.
_HIDDEN_TAGS = ("ix:header", "ix:hidden", "ix:references", "ix:resources")
_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


def _strip_hidden(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style"]):
        tag.decompose()
    for name in _HIDDEN_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()
    for tag in soup.find_all(style=_DISPLAY_NONE_RE):
        tag.decompose()


# A few filing agents render body text with one <div>/table cell per *word*
# instead of per paragraph (seen in both a 10-K and a 40-F AIF). That defeats
# the block-tag line grouping below: every "line" becomes an isolated word,
# which the boilerplate filter then discards one at a time, leaving a
# disconnected patchwork with words missing mid-sentence. Re-joining long
# runs of such fragments reconstructs the original paragraphs, which is
# strictly better than the alternative of detecting the damage afterwards
# and rejecting the filing.
_FRAGMENT_RUN = 6  # consecutive fragments before a run is treated as shredded prose
# Share of a document's characters sitting in lines too short to be prose.
# Word-per-cell filings measure ~0.47 here; ordinary ones, including
# number-heavy bank and utility 10-Ks, stay under 0.12.
_SHREDDED_DOC_RATIO = 0.30


_ITEM_LINE_RE = re.compile(r"^items?\s*\d", re.IGNORECASE)


def _is_fragment(line: str) -> bool:
    # ALL-CAPS lines and "Item N" lines are headings even in a shredded
    # document, and merging one into the prose around it would hide the very
    # thing the section search is looking for.
    if len(line) > 40 or line.isupper() or _ITEM_LINE_RE.match(line):
        return False
    if line[-1] in ".:;!?":
        return False
    return len(line.split()) <= 5


def _unshred(lines: list[str]) -> list[str]:
    """Re-join word-per-cell prose, but only in a document that is shredded
    throughout.

    Merging is gated on the whole document rather than applied wherever a
    run of short lines appears, because short lines are also what a table of
    contents, a page header/footer and a financial table look like. A
    document-local rule swallowed real body headings into the surrounding
    run - "Item 1. Business" is itself only three short words - and cost
    filings that had extracted cleanly before.
    """
    total = sum(len(line) for line in lines)
    short = sum(len(line) for line in lines if len(line) < 25)
    if not total or short / total < _SHREDDED_DOC_RATIO:
        return lines

    out: list[str] = []
    run: list[str] = []
    for line in lines + [""]:
        if line and _is_fragment(line):
            run.append(line)
            continue
        out.append(" ".join(run)) if len(run) >= _FRAGMENT_RUN else out.extend(run)
        run = []
        if line:
            out.append(line)
    return out


def _clean_lines(html: bytes) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    _strip_hidden(soup)

    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    raw_text = soup.get_text("")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.split("\n")]
    return _unshred([line for line in lines if line])


def _is_boilerplate(line: str) -> bool:
    if len(line) < 25:
        return True
    # Short ALL-CAPS lines are section/sub-section headings. Long ones are
    # prose - a handful of (mostly older) filers set whole paragraphs in
    # capitals, and dropping those would gut the section.
    if line.isupper() and len(line) < 200:
        return True
    return any(pattern.search(line) for pattern in _BOILERPLATE_LINE_RES)


_HEADING_WINDOW = 3  # some filers split a heading's label and title across
# separate lines/table cells - e.g. Amazon's real "Item 1. Business" heading
# renders as two lines, "Item 1." then "Business", not one. Joining up to
# this many consecutive lines catches that without much false-positive risk:
# a spurious window match just yields another (probably short/low-quality)
# section candidate, which the trailing-content and quality checks below
# already have to filter out anyway.


# Headings are short, but not always on a line of their own: plenty of
# filers run the heading and the first sentence together ("Item 1. Business.
# Cars.com Inc. (NYSE:CARS) is a trusted audience-powered..."). So a long
# line is accepted only when the text after the match starts a new sentence.
# That is what separates it from a pattern like "^our business" landing
# mid-prose - on a risk-factor headline ("Our business could be harmed if
# we fail to...") or a forward-looking-statements bullet ("our business
# plans and strategies,") - where the match runs on in lower case.
_MAX_HEADING_CHARS = 100
_HEADING_TRAILER_CHARS = " .:;)-–—\u2013\u2014"


def _is_heading_shaped(joined: str, match: re.Match) -> bool:
    if len(joined) <= _MAX_HEADING_CHARS:
        return True
    rest = joined[match.end() :].lstrip(_HEADING_TRAILER_CHARS)
    return not rest or rest[0].isupper()


def _match_at(
    lines: list[str], start: int, regexes: tuple[re.Pattern, ...], heading: bool = False
) -> int | None:
    """If a window of 1.._HEADING_WINDOW lines starting at `start` matches
    any of `regexes` (joined with a space), returns the index of the last
    line in that window; otherwise None. Some filers split a heading's or
    boundary's label and title across separate lines/table cells - e.g.
    Amazon's real "Item 1. Business" heading renders as two lines, "Item 1."
    then "Business", not one, and the same filer splits "Item 1A. Risk
    Factors" the same way.
    """
    n = len(lines)
    for window in range(1, _HEADING_WINDOW + 1):
        if start + window > n:
            break
        joined = " ".join(lines[start : start + window])
        for regex in regexes:
            match = regex.match(joined)
            if match is None:
                continue
            if heading and not _is_heading_shaped(joined, match):
                continue
            return start + window - 1
    return None


def _find_heading_ends(lines: list[str], heading_res: tuple[re.Pattern, ...]) -> list[int]:
    """Returns, for each heading match, the index of the last line it
    consumed (content starts at that index + 1)."""
    ends = []
    for i in range(len(lines)):
        end = _match_at(lines, i, heading_res, heading=True)
        if end is not None:
            ends.append(end)
    return ends


_LEAD_WINDOW = 15  # lines after a heading to look for prose in
_MIN_LEAD_PROSE = 200


def _looks_like_prose(line: str) -> bool:
    return len(line) >= 40 and " " in line and any(c.islower() for c in line)


def _lead_prose(lines: list[str], start: int) -> int:
    """Characters of prose in the handful of lines right after a heading.

    A real section heading is followed within a line or two by body text; a
    table-of-contents or cross-reference entry is followed by more entries
    and page numbers. Used to drop TOC candidates in the best-effort path,
    where there is no boundary to measure trailing content against.
    """
    window = lines[start + 1 : start + 1 + _LEAD_WINDOW]
    return sum(len(line) for line in window if _looks_like_prose(line))


def _content_len(lines: list[str], start: int, end: int) -> int:
    return sum(len(line) for line in lines[start:end])


@dataclass(frozen=True)
class SectionSpec:
    """How to find one form type's business section.

    `heading_tiers` runs from the form's canonical heading to progressively
    more generic ones; tiers at index >= `generic_from` are only searched in
    the first half of the document (see `_FALLBACK_POSITION_LIMIT_RATIO`).
    `boundaries` are the form's structural section markers; `soft_boundaries`
    are looser, title-based ones tried only when no structural boundary
    follows the heading at all.
    """

    heading_tiers: tuple[tuple[re.Pattern, ...], ...]
    boundaries: tuple[re.Pattern, ...]
    soft_boundaries: tuple[re.Pattern, ...]
    generic_from: int


# Generic headings ("Overview", "Our Business") also occur as sub-headings
# inside the unrelated MD&A section later in the same document, and inside
# cross-reference tables that list these same phrases as labels pointing at
# page numbers. The business section always appears in the first half of an
# annual report, well before MD&A and any trailing cross-reference table, so
# generic-tier matches past that point are ignored.
_FALLBACK_POSITION_LIMIT_RATIO = 0.5


def _first_boundary(
    lines: list[str], start: int, boundaries: tuple[re.Pattern, ...], min_content: int = 0
) -> int | None:
    """Index of the first boundary after `start` with at least `min_content`
    characters of text in front of it, or None."""
    j = start + 1
    while j < len(lines):
        end = _match_at(lines, j, boundaries)
        if end is not None and _content_len(lines, start + 1, j) >= min_content:
            return j
        j = (end + 1) if end is not None else (j + 1)
    return None


def _truncate(lines: list[str], start: int, end: int) -> int:
    total = 0
    for j in range(start + 1, end):
        total += len(lines[j])
        if total >= _MAX_SECTION_CHARS:
            return j
    return end


def _section_end(lines: list[str], start: int, spec: SectionSpec, allow_unbounded: bool) -> int | None:
    """Where the section starting after `start` ends, or None if it can't be
    determined and `allow_unbounded` is False.

    Requiring `_MIN_SECTION_CHARS` in front of the boundary is what skips a
    cross-reference table immediately following the heading (several item
    numbers within a few lines of each other, nothing but page references
    between them) while still accepting a filing whose real Item 1A/1B/2 are
    a single line each - the case a "boundaries close together must be a
    cross-reference table" rule got wrong, rejecting hundreds of otherwise
    clean filings.
    """
    end = _first_boundary(lines, start, spec.boundaries, _MIN_SECTION_CHARS)
    if end is None and not allow_unbounded:
        return None

    if end is None or _content_len(lines, start + 1, end) > _MAX_SECTION_CHARS:
        soft = _first_boundary(lines, start, spec.soft_boundaries, _MIN_SECTION_CHARS)
        if soft is not None and (end is None or soft < end):
            end = soft

    return _truncate(lines, start, end if end is not None else len(lines))


def _clean_section(lines: list[str], start: int, end: int) -> str | None:
    section = lines[start + 1 : end]
    if _content_len(section, 0, len(section)) < _MIN_SECTION_CHARS:
        return None

    content_lines = [line for line in section if not _is_boilerplate(line)]
    if not content_lines:
        return None

    text = re.sub(r"\s+", " ", " ".join(content_lines)).strip()
    if len(text) < _MIN_RESULT_CHARS:
        return None
    return text


# A generic heading recurs legitimately throughout a filing - Citigroup's
# 10-K has "Overview" under Capital Resources, under Managing Global Risk
# and half a dozen other places besides the one that opens the report. Its
# business description is the *first* of them, so generic tiers take the
# earliest candidate with a substantial section rather than the largest
# (Citi's risk-management "Overview" runs to 240K characters and would win
# a size contest outright). This floor is well above `_MIN_SECTION_CHARS`
# so a passing mention still can't outrank the real section.
_MIN_GENERIC_SECTION_CHARS = 2_000


def _best_heading(
    lines: list[str], starts: list[int], spec: SectionSpec, generic: bool
) -> int | None:
    """Pick the real heading out of a tier's matches.

    Ranking deliberately uses the *first* boundary after each candidate with
    no minimum-content requirement. A TOC entry is immediately followed by
    the next TOC entry, so it scores ~nothing; the real body heading is
    followed by pages of prose. (Applying the minimum-content skipping used
    in `_section_end` here instead would let a TOC candidate's search leak
    through its own TOC block and latch onto the same far-away real
    boundary the genuine candidate uses, making the TOC candidate look
    larger than the real one.)
    """
    # Only structural boundaries count here. Including the looser title-based
    # ones truncated the score of a genuine heading that happens to be
    # followed by a sub-heading matching one of them (Entergy's Item 1), and
    # dropped the filing entirely.
    lengths = []
    for start in starts:
        end = _first_boundary(lines, start, spec.boundaries)
        lengths.append((start, _content_len(lines, start + 1, end if end is not None else len(lines))))

    if generic:
        for start, section_len in lengths:  # `starts` is ascending
            if section_len >= _MIN_GENERIC_SECTION_CHARS:
                return start
        return None

    best_start, best_len = None, 0
    for start, section_len in lengths:
        if section_len > best_len:
            best_start, best_len = start, section_len
    if best_start is None or best_len < _MIN_SECTION_CHARS:
        return None
    return best_start


def _tier_starts(lines: list[str], spec: SectionSpec, tier_index: int) -> list[int]:
    starts = _find_heading_ends(lines, spec.heading_tiers[tier_index])
    if tier_index >= spec.generic_from:
        limit = int(len(lines) * _FALLBACK_POSITION_LIMIT_RATIO)
        starts = [i for i in starts if i <= limit]
    return starts


def _with_lead_prose(lines: list[str], starts: list[int]) -> list[str]:
    """Candidates that actually have body text behind them.

    A heading followed by more headings is a table-of-contents entry.
    Requiring prose is a hard filter in the best-effort path, where there is
    no boundary to corroborate the candidate and Citigroup's contents-page
    "Business" line would otherwise be accepted on its own; where a real
    boundary exists it is only a preference, because a filing can put its
    contents block between the heading and the body (Cars.com puts 70-odd
    lines there) and still be extracted correctly.
    """
    return [i for i in starts if _lead_prose(lines, i) >= _MIN_LEAD_PROSE]


def _extract_section(lines: list[str], spec: SectionSpec) -> str | None:
    bounded, unbounded = [], []
    for tier_index in range(len(spec.heading_tiers)):
        tier = _tier_starts(lines, spec, tier_index)
        if not tier:
            continue
        generic = tier_index >= spec.generic_from
        with_prose = _with_lead_prose(lines, tier)
        for candidates, pool in ((bounded, tier), (unbounded, with_prose)):
            if not pool:
                continue
            start = _best_heading(lines, pool, spec, generic)
            if start is not None:
                candidates.append(start)

    # Every tier is tried with a real boundary required before any tier is
    # allowed to fall back to a capped best-effort section, so a filing that
    # has a clean "Business Overview" is never served a truncated "Overview".
    for allow_unbounded, starts in ((False, bounded), (True, unbounded)):
        for start in starts:
            end = _section_end(lines, start, spec, allow_unbounded)
            if end is None:
                continue
            text = _clean_section(lines, start, end)
            if text is not None:
                return text
    return None


def _res(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# "Overview" has to stand alone as the whole heading, or introduce the
# business specifically. Allowing it to merely *start* a heading matched
# The Hartford's "Overview of Reserving for Property and Casualty Insurance
# Claims", which is the earliest such match in that filing and anchored the
# section 200K characters into the wrong part of the document.
_OVERVIEW_RE = r"^overview(?:\s*[:.\-–—]?\s*$| of (?:the |our )?(?:business|company|operations)\b)"


# -- 10-K: "Item 1. Business" through Item 1A/1B/1C/2 ------------------------

# "Items 1 and 2. Business and Properties" is a standard combined heading for
# oil & gas companies and REITs, whose properties *are* their business.
_SPEC_10K = SectionSpec(
    heading_tiers=(
        _res(r"^items?\s*1\s*(?:(?:and|&|,)\s*(?:item\s*)?2\s*)?[.:)\-–—\s]*business\b"),
        _res(
            r"^business\s*[.:]?\s*$",
            r"^business and properties\b",
            r"^general development of (?:the )?business\b",
            r"^(?:narrative )?description of (?:the |our )?business\b",
            r"^nature of (?:the |our )?business\b",
            r"^business overview\b",
        ),
        _res(
            r"^overview of (?:our|the) business\b",
            r"^(?:company|corporate|group) (?:overview|profile)\b",
            r"^our business\b",
            r"^business of the (?:company|registrant|corporation)\b",
            r"^about (?:us|our business|the company|our company)\b",
            r"^who we are\b",
        ),
        _res(_OVERVIEW_RE, r"^the company\s*[:.]?\s*$"),
    ),
    boundaries=_res(r"^item\s*(?:1a|1b|1c|2)\b"),
    soft_boundaries=_res(
        r"^item\s*(?:[3-9]|1[0-6])\b",
        r"^risk factors\b",
        r"^unresolved staff comments\b",
        r"^propert(?:y|ies)\s*[.:]?\s*$",
        r"^legal proceedings\b",
        r"^management'?s discussion and analysis\b",
        r"^quantitative and qualitative disclosures\b",
        r"^financial statements and supplementary data\b",
    ),
    generic_from=1,
)


def extract_10k_business(html: bytes) -> str | None:
    return _extract_section(_clean_lines(html), _SPEC_10K)


# -- 20-F: "Item 4, B. Business Overview" through C./Item 4A/Item 5 ---------

# Filers write the subsection label every possible way: "B. Business
# Overview", "B.Business Overview", "Item 4.B. Business Overview",
# "Item 4 - B. Business Overview". Tier 2 falls back to the whole of Item 4
# ("Information on the Company"), whose A/B/C/D subsections always contain
# the business description - a superset, but a reliable one.
_ITEM4_PREFIX = r"(?:items?\s*4\s*[.:)\-–—]?\s*)?"
_SPEC_20F = SectionSpec(
    heading_tiers=(
        _res(rf"^{_ITEM4_PREFIX}b\s*[.:)\-–—]\s*business\s*overview\b"),
        _res(r"^items?\s*4\s*[.:)\-–—]?\s*information on the (?:company|registrant|group)\b"),
        _res(
            r"^business overview\b",
            r"^overview of (?:our|the) business\b",
            r"^(?:narrative )?description of (?:the |our )?business\b",
            r"^principal (?:business )?activities\b",
            rf"^{_ITEM4_PREFIX}a\s*[.:)\-–—]\s*history and development\b",
        ),
        _res(
            r"^our business\b",
            r"^(?:company|corporate|group|business) (?:overview|profile)\b",
            r"^about (?:us|our business|the company|our company)\b",
            r"^who we are\b",
            _OVERVIEW_RE,
        ),
    ),
    boundaries=_res(
        rf"^{_ITEM4_PREFIX}c\s*[.:)\-–—]\s*organi[sz]ational structure\b",
        rf"^{_ITEM4_PREFIX}d\s*[.:)\-–—]\s*propert(?:y|ies)\b",
        r"^items?\s*4a\b",
        r"^items?\s*5\b",
    ),
    soft_boundaries=_res(
        r"^organi[sz]ational structure\b",
        r"^propert(?:y|ies), plants? and equipment\b",
        r"^operating and financial review\b",
        r"^items?\s*(?:[5-9]|1[0-9])\b",
        r"^unresolved staff comments\b",
    ),
    generic_from=2,
)


def extract_20f_business(html: bytes) -> str | None:
    return _extract_section(_clean_lines(html), _SPEC_20F)


# -- 40-F's Annual Information Form exhibit ---------------------------------

# Canadian AIFs follow National Instrument 51-102F2, so both the business
# heading and the sections that typically follow it come from that template.
# Filers routinely splice their own name into the heading ("GENERAL
# DEVELOPMENT OF OR ROYALTIES' BUSINESS"), and a sizeable minority use their
# own wording entirely ("About our Business", "DESCRIPTION OF BUSINESS").
# Filers that follow NI 51-102F2's own numbering print "ITEM 3 - GENERAL
# DEVELOPMENT OF THE BUSINESS" rather than the bare title, so every AIF
# heading and boundary tolerates that prefix.
_AIF_ITEM_PREFIX = r"(?:items?\s*\d{1,2}\s*[.:)\-–—]\s*)?"
_SPEC_AIF = SectionSpec(
    heading_tiers=(
        _res(rf"^{_AIF_ITEM_PREFIX}general development of\b.{{0,60}}\bbusiness\b"),
        _res(
            rf"^{_AIF_ITEM_PREFIX}(?:narrative )?description of (?:the |our )?business\b",
            rf"^{_AIF_ITEM_PREFIX}(?:an )?overview of (?:the |our )?business\b",
            rf"^{_AIF_ITEM_PREFIX}(?:about )?our business\b",
            rf"^{_AIF_ITEM_PREFIX}business of the (?:company|corporation|issuer)\b",
            rf"^{_AIF_ITEM_PREFIX}the business\b",
            rf"^{_AIF_ITEM_PREFIX}business overview\b",
            rf"^{_AIF_ITEM_PREFIX}general development\b",
        ),
        _res(
            rf"^{_AIF_ITEM_PREFIX}corporate structure\b",
            r"^(?:company|corporate|group|business) (?:overview|profile)\b",
            r"^about (?:us|the company|our company)\b",
            r"^who we are\b",
            _OVERVIEW_RE,
        ),
    ),
    boundaries=_res(
        rf"^{_AIF_ITEM_PREFIX}dividends?(?: and distributions)?\b",
        rf"^{_AIF_ITEM_PREFIX}description of capital structure\b",
        rf"^{_AIF_ITEM_PREFIX}market for securities\b",
        rf"^{_AIF_ITEM_PREFIX}risk factors\b",
        rf"^{_AIF_ITEM_PREFIX}capitali[sz]ation\b",
        rf"^{_AIF_ITEM_PREFIX}escrowed securities\b",
        rf"^{_AIF_ITEM_PREFIX}directors and (?:officers|executive officers)\b",
    ),
    soft_boundaries=_res(
        rf"^{_AIF_ITEM_PREFIX}audit committee\b",
        rf"^{_AIF_ITEM_PREFIX}legal proceedings(?: and regulatory actions)?\b",
        rf"^{_AIF_ITEM_PREFIX}interest of management and others\b",
        rf"^{_AIF_ITEM_PREFIX}material contracts\b",
        rf"^{_AIF_ITEM_PREFIX}transfer agents? and registrars?\b",
        rf"^{_AIF_ITEM_PREFIX}promoters?\b",
        rf"^{_AIF_ITEM_PREFIX}additional information\b",
        rf"^{_AIF_ITEM_PREFIX}names? and incorporation\b",
        rf"^{_AIF_ITEM_PREFIX}experts?\b",
    ),
    generic_from=2,
)


def extract_aif_business(html: bytes) -> str | None:
    return _extract_section(_clean_lines(html), _SPEC_AIF)
