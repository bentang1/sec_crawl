# EDGAR company description scraper

For every company in SEC EDGAR's `company_tickers.json`, locates its most
recent annual report and extracts the full text of its business-description
section into `output.csv`. The annual report is located in **strict
priority order**:

```
10-K -> 20-F -> 40-F -> 10-K/A -> 20-F/A -> 40-F/A -> failure
```

10-K is for US domestic filers, 20-F for foreign private issuers, 40-F for
Canadian MJDS filers; the `/A` variants (amendments) are tried only if the
company has never filed a plain 10-K/20-F/40-F. Whichever form is found
first in that order is used, even if a lower-priority form is more recently
filed — e.g. a company with an older 20-F and a newer 40-F still uses the
20-F. Each form has its own business-description section and its own
heading convention:

- **10-K**: "Item 1. Business", through the Item 1A/1B/2 boundary. If that
  strict heading isn't found anywhere, a conservative fallback list is
  tried (early in the document only, before Item 7/MD&A): "General
  Development of Business", "Description of Business", "Nature of
  Business", "Business Overview" — see "Known failure modes" below for why
  this list deliberately excludes generic headings like "Overview".
- **20-F**: "Item 4, B. Business Overview", through the next lettered
  subsection or Item 4A/5.
- **40-F**: the primary EDGAR document is just a cover-page wrapper — the
  real narrative lives in a separately-filed exhibit, the Annual
  Information Form (conventionally `EX-99.1`, though this varies by filer —
  see below). That exhibit's "GENERAL DEVELOPMENT OF THE BUSINESS" section
  (through the next major ALL-CAPS section) is extracted instead.

`output.csv` records which of the six form types was actually used per
company (`filing_type` column), so you can see how a given row was
produced.

## Setup

```bash
cd quant/edgar_scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

SEC requires a descriptive `User-Agent` header (`AppName contact@email.com`)
on every request. Set it before running — the script refuses to start
without it:

```bash
export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"
```

Optional env vars (defaults shown):

```bash
export EDGAR_DATA_DIR=./data              # cache, checkpoint db, output CSVs
export EDGAR_REQUESTS_PER_SECOND=10       # shared throttle across all requests
export EDGAR_MAX_WORKERS=8                # concurrent worker threads
```

## Running

```bash
# this first
export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"
# smoke-test on a couple of tickers first
.venv/bin/python -m edgar_scraper --tickers AAPL,MSFT

# the full ~10,000+ company run (long-running, see "Timing" below)
.venv/bin/python -m edgar_scraper

# useful flags
.venv/bin/python -m edgar_scraper --limit 500       # only process the first 500 pending companies
.venv/bin/python -m edgar_scraper --retry-failed     # also re-attempt previously failed companies
.venv/bin/python -m edgar_scraper --verbose          # debug logging incl. every HTTP request
```

**Stopping and resuming**: the script checkpoints per-company outcomes to
SQLite as it goes (`data/checkpoint.sqlite3`). Ctrl-C or kill it any time —
rerunning the same command skips every ticker already resolved (`done` or
`failed`), and continues from where it left off. Downloaded filings are also
cached to disk, so a resumed run never re-downloads a document it already
has, even for a ticker whose extraction hadn't finished. Use `--retry-failed`
to re-attempt tickers that failed on a previous run (e.g. after a bug fix).

## Output

- `data/output.csv` — `stock_ticker,name,filing_type,description`, one row
  per company whose business-description section was successfully
  extracted. `filing_type` is whichever of 10-K/20-F/40-F/10-K/A/20-F/A/40-F/A
  was actually used. Appended to incrementally as each company completes.
- `data/failure.csv` — `stock_ticker,cik,name,reason`, one row per company
  with no usable annual report of any of the six types, or that hit an
  error — with a machine-readable reason (see "Known failure modes" below).
- `data/checkpoint.sqlite3` — the resume/dedup state (one row per ticker,
  status `done` / `failed`).

**Heads up on CSV size**: descriptions are the *full* section text, not a
short excerpt, and 40-F Annual Information Forms in particular run much
longer than a 10-K's Item 1 (SNDL's AIF business section alone was ~130K
characters in testing). If you read `output.csv` with Python's `csv`
module, you may need `csv.field_size_limit(10_000_000)` or similar — the
default 128KB field limit is too small for some rows.

## Timing

10-K throughput in a live 300-company sample was roughly 1.3-1.5
companies/second with 8 worker threads (the bottleneck is per-company
request latency and the sequential submissions-fetch -> filing-fetch
dependency, not the 10 req/s cap). 20-F and 40-F are both slower per
company: 20-F documents are often enormous (ASML's was 24.8MB, HSBC's
57MB, vs. Apple's ~100KB 10-K) which makes parsing noticeably slower, and
40-F requires two extra fetches (the filing index, then the AIF exhibit)
before extraction can even start. Budget more than the 10-K-only estimate
of 1.5-2.5 hours for the full ~10,000+ company run, and run it in the
background (`nohup ... &`, tmux, etc.) rather than in a foreground shell you
might close — lean on the checkpointing to resume if it's interrupted.

## Local filing cache — shared layout for the Task 1 document downloader

`data/filings/{cik10}/{accession_no_dashes}/` holds every raw document this
script downloads — the primary filing, plus (for 40-F) the filing's index
page and the AIF exhibit — alongside a `metadata.json` per document. This
mirrors EDGAR's own CIK/accession-number URL structure rather than being
scoped to just one form type, so the separate document-database task (Task
1 of the broader PRD — downloading 8-K/10-K/10-Q filings for the team's
stock universe) can read from or write into the same tree without
re-fetching anything this script already pulled. `data/submissions/{cik10}.json`
similarly caches each company's full filing-history JSON.

## Tested against

Validated end-to-end against real EDGAR data for all six form types: a live
300+-company unfiltered 10-K sample (~92% extraction success among
companies that actually have a 10-K), targeted checks across 20-F (BABA,
ASML, HSBC, SAP, NVS, MUFG, ...) and 40-F (SNDL, RY, TD) filers, and
multiple broader unfiltered live batches (150+ companies each) after each
change below, scanning every successful result for silent quality problems
(descriptions starting with a lowercase letter — a signature of the
dropped-leading-word issue described below — and unusually large results'
tails, checked by hand for content that reads like it bled into an
unrelated section). None found outside the specific cases already
documented here.

Run `.venv/bin/python tests/test_extraction.py` to re-check the extraction
logic against 14 cached real filings (9 that should succeed, 5 documented
fragile cases that should correctly fail) without hitting the network.

Two matching refinements were added after the initial build, both found by
testing against real companies rather than anticipated in advance:

**Headings and boundaries can split across lines.** Several filers (Amazon
in its 10-K, Royal Bank of Canada in its AIF) render the real body heading
as two separate lines/table cells — "Item 1." then "Business" on the next
line — not one, and the same is true of section boundaries like "Item 1A.
Risk Factors". A naive single-line match misses these entirely (this is
what caused Amazon specifically to have no extractable 10-K description
before this was found and fixed). The heading/boundary matcher joins
windows of up to 3 consecutive lines before testing the regex, which
catches this pattern; a spurious window match just produces another
(usually short/low-quality) candidate section, which the trailing-content
and quality-ratio checks already have to filter out anyway.

**A found boundary can itself be untrustworthy.** Some filers (McDonald's)
print "Item 1A", "Item 1B", and "Item 2" only inside their cross-reference
table, all within a handful of lines of each other, nowhere else in the
document. Naively accepting the first "Item 1A" match as the boundary in
that case swept in everything from the real Item 1 content through risk
factors, financial statements, and the exhibit index — 240K characters
spanning nearly the entire filing, once the fallback heading list (below)
started finding a heading for McDonald's-style filers. The fix compares
each candidate boundary against others nearby: if a *different* item
number is found within 20 lines, that's almost certainly a cross-reference
table and the match is skipped in favor of a later, isolated one (or a
clean failure, if none exists). Comparing by identity rather than mere
proximity matters because some filers (Goldman Sachs) legitimately repeat
the *same* boundary heading as a running page header for hundreds of lines
through the real Risk Factors section — that repetition alone isn't a
cross-reference table and would have been wrongly rejected by a
proximity-only check.

## Known failure modes

`reason` in `failure.csv` is one of:

- **`no_annual_report_found`** — the company has never filed any of 10-K,
  20-F, 40-F, or their amended variants. Expected for entities in
  `company_tickers.json` that aren't operating companies with a standard
  annual report at all (e.g. certain funds/trusts), or file under a
  different regime entirely (e.g. 6-K-only foreign issuers with no 20-F on
  file).
- **`business_section_not_found (<form>)`** — the company has a filing of
  that type, but its business-description section couldn't be reliably
  located or its content couldn't be reliably extracted, so the script
  fails rather than guessing. Three distinct patterns showed up in testing:
  - **Fragmented HTML** (40-F AIF: Toronto-Dominion/TD): the filing agent
    renders body text with one `<div>`/table cell per *word* rather than
    per paragraph. The heading itself may still be found, but the
    surviving content after boilerplate-filtering is a disconnected
    patchwork with words silently missing mid-sentence — worse than an
    outright failure, since it reads as plausible prose. A quality gate
    rejects this: if less than 70% of a matched section's characters
    survive boilerplate filtering (a well-formed section keeps ~98%+), the
    result is discarded rather than returned. Note this gate is a
    *whole-section average* — SunPower's 10-K has this same word-per-div
    issue concentrated in just its first two sentences (each is missing its
    leading word/subject, e.g. "mission is to deliver..." instead of "Our
    mission is to deliver...") but the other ~99% of that section is
    normal prose, so the average stays well above 70% and the result is
    kept with that one cosmetic blemish rather than discarded outright.
    This was a deliberate tradeoff after finding it only affected one
    filer in testing (confirmed by scanning many other live results for the
    same "starts with a lowercase letter" signature and finding none):
    rejecting the whole 24K-character section over two slightly-off opening
    sentences seemed like the wrong call.
  - **No formal heading anywhere in the body** (10-K: Intel, GE, Citigroup,
    Morgan Stanley, ConocoPhillips; 20-F: ASML, HSBC, SAP, Novartis, Shell,
    Toyota, BHP, TotalEnergies, Rio Tinto, Sumitomo Mitsui, Deutsche
    Telekom, AstraZeneca, Advantest, Arm): these filers reorder their
    annual report entirely under their own headings (e.g. "Overview", "Our
    Business", "At a glance") and only map those back to the formal item
    numbers in a cross-reference table at the end, which SEC/regulatory
    rules permit — including for headings on the conservative fallback list
    below. Nothing in the body says any recognized heading, so the search
    correctly finds nothing rather than guessing which custom heading is
    the right one. This disproportionately affects large, well-known names.
  - **A heading is found, but only a cross-reference-table boundary exists**
    (10-K: McDonald's): the fallback heading list below does find an early,
    legitimate-looking heading, but every "Item 1A"-style match in the rest
    of the document turns out to be part of the same cross-reference table
    (see the clustering note above) — so there's no genuine boundary to
    stop at, and the candidate is rejected rather than run to end-of-file.
  Two things widen 10-K coverage beyond the strict "Item 1. Business"
  match, both used only when that strict match fails anywhere in the
  document: a small, conservative fallback heading list — "General
  Development of Business", "Description of Business", "Nature of
  Business", "Business Overview" — tried only in the first half of the
  document (Item 1 always precedes Item 7/MD&A and any trailing
  cross-reference table), and requiring a genuine boundary to be found at
  all before trusting the result. The fallback list deliberately excludes
  generic single-word headings like "Overview" or "Our Business": those
  also commonly appear as subheadings inside the unrelated MD&A section
  later in the same document, and a false match there would silently
  return the wrong content instead of a clean failure — this is why
  Intel/GE/Citigroup-style filers (which use exactly those generic
  headings, not the more specific fallback phrases) still correctly fail.
- **`40f_aif_exhibit_not_found`** — the 40-F's filing index doesn't have an
  exhibit that looks like an Annual Information Form (matched by exhibit
  description text containing "annual information form", or by a set of
  common exhibit-number conventions: `EX-99.1`, `EX-1`, and a few variants).
  Filers occasionally use a different convention entirely.
- **`submissions_fetch_failed:` / `filing_fetch_failed:` / `40f_index_fetch_failed:`
  / `extraction_error:` / `unexpected:`** — network error, malformed filing,
  or an unexpected exception; these should be rare. Each includes the
  underlying error message so a spot check is possible.

If closing the remaining cross-reference-table coverage gap matters later,
the next step would be parsing each filer's own cross-reference table and
following it to their custom heading — not implemented here because a
fuzzy match against generic headings like "Overview" risks false positives
(that heading is also extremely common inside the unrelated MD&A section),
which would silently produce a wrong description instead of a clean,
reviewable failure.
