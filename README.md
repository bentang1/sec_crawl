# EDGAR company description scraper

For every company in SEC EDGAR's `company_tickers.json`, locates its most
recent annual report and extracts the full text of its business-description
section into `data/output/`. The annual report is located in **strict
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
heading conventions:

- **10-K**: "Item 1. Business" (or "Items 1 and 2. Business and Properties",
  the standard combined heading for oil & gas companies and REITs), through
  the Item 1A/1B/1C/2 boundary.
- **20-F**: "Item 4, B. Business Overview", through the next lettered
  subsection or Item 4A/5. Filers write the label every possible way —
  "B. Business Overview", "B.Business Overview", "Item 4.B. Business
  Overview" — and where none of them appears, the whole of Item 4
  ("Information on the Company") is taken instead: a superset, but one whose
  A/B/C/D subsections always contain the business description.
- **40-F**: the primary EDGAR document is just a cover-page wrapper — the
  real narrative lives in a separately-filed exhibit, the Annual
  Information Form (conventionally `EX-99.1`, though this varies by filer —
  see below). That exhibit's "General Development of the Business" section
  is extracted instead, through the next major AIF section. Filers routinely
  splice their own name into that heading ("GENERAL DEVELOPMENT OF **OR
  ROYALTIES\u2019** BUSINESS") or prefix it with NI 51-102F2\u2019s own item
  numbering ("ITEM 3 - GENERAL DEVELOPMENT OF THE BUSINESS").

### Heading tiers

Roughly a third of filers never print the formal heading in the body at all.
They reorder the report under their own wording and map it back to item
numbers only in a cross-reference index, which the SEC permits. So each form
has a **list of heading tiers**, from its canonical heading down to
progressively more generic ones — "Business Overview", "Description of the
Business", "Our Business", "About our Business", "Overview". Tiers are tried
in order and the first that yields a usable section wins, so a generic
heading is never preferred over the canonical one in a filing that has both.

Generic tiers carry extra guards, because their patterns also match ordinary
prose and sub-headings elsewhere in the document:

- they are searched only in the first half of the document, ahead of MD&A
  and any trailing cross-reference index;
- a match must be *shaped* like a heading — either a short line, or a longer
  one where the text after the match starts a new sentence. Plenty of filers
  run the heading into the first sentence ("Item 1. Business. Cars.com Inc.
  (NYSE:CARS) is a trusted audience-powered..."), which is why the rule is
  not a flat length limit. It is what stops "^our business" from matching a
  risk-factor headline ("Our business could be harmed if we fail to...") or
  a forward-looking-statements bullet ("our business plans and strategies,");
- among generic matches the **earliest** substantial one wins, not the
  largest. Citigroup\u2019s 10-K has "Overview" a dozen times over — under Capital
  Resources, under Managing Global Risk — and its risk-management "Overview"
  runs to 240K characters, so a size contest picks exactly the wrong one.

### Where a section ends

The section runs to the first boundary with at least a couple of hundred
characters of content in front of it. Requiring content in front is what
skips a cross-reference table sitting immediately after the heading, which
lists several item numbers with nothing but page references between them.

If no structural boundary follows the heading — or the nearest one is so far
away that the section is implausibly long, which happens when a filer\u2019s only
"Item 1A" line is in an index at the very end of the document — a looser
list of title-based boundaries is consulted ("Risk Factors", "Properties",
"Management\u2019s Discussion and Analysis"), and failing that the section runs
to a character cap. Best-effort is only reached after every heading tier has
been tried with a real boundary required, so a filing with a clean
"Business Overview" is never served a truncated "Overview".

`data/output/` records which of the six form types was actually used per
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

Set this once per shell — every command below needs it, and both programs
refuse to start without it:

```bash
export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"
```

### The four runs

**Annual descriptions, every company** (~10,000+; long-running, see "Timing"):

```bash
.venv/bin/python -m edgar_scraper
```

**Annual descriptions, first 500:**

```bash
.venv/bin/python -m edgar_scraper --limit 500
```

**Quarterly reports, every company** — `--index-only` records where every
report is without downloading it (~2MB). Drop the flag to also download the
documents, but read "Sizing a full download" first: that needs ~10GB:

```bash
.venv/bin/python -m edgar_scraper.quarterly --index-only
```

**Quarterly reports, first 50:**

```bash
.venv/bin/python -m edgar_scraper.quarterly --limit 50
```

### Flags

Both programs take the same ones:

| flag | |
| --- | --- |
| `--limit N` | only process the first N **pending** companies |
| `--tickers AAPL,MSFT` | restrict the run to specific tickers |
| `--retry-failed` | also re-attempt companies previously marked failed |
| `--verbose` | debug logging, including every HTTP request |

`edgar_scraper` additionally takes `--export-parquet` (rebuild `data/output/`
from the checkpoint, no network); `edgar_scraper.quarterly` additionally
takes `--index-only`.

`--limit N` counts *pending* companies, not the first N in the file — already
resolved companies are skipped before the limit applies. So running
`--limit 500` twice processes 1,000 distinct companies, not the same 500
twice. Smoke-test either program on a couple of tickers first:

```bash
.venv/bin/python -m edgar_scraper --tickers AAPL,MSFT
```

**Stopping and resuming**: the script checkpoints per-company outcomes to
SQLite as it goes (`data/checkpoint.sqlite3`). Ctrl-C or kill it any time —
rerunning the same command skips every ticker already resolved (`done` or
`failed`), and continues from where it left off. Downloaded filings are also
cached to disk, so a resumed run never re-downloads a document it already
has, even for a ticker whose extraction hadn't finished. Use `--retry-failed`
to re-attempt tickers that failed on a previous run (e.g. after a bug fix).

## Output

- `data/output/` — a directory of Parquet part files with columns
  `stock_ticker,name,filing_type,description`, one row per company whose
  business-description section was successfully extracted. `filing_type` is
  whichever of 10-K/20-F/40-F/10-K/A/20-F/A/40-F/A was actually used. Read
  the whole directory as one table:

  ```python
  import pandas as pd
  df = pd.read_parquet("data/output")                       # everything
  df = pd.read_parquet("data/output", columns=["stock_ticker", "filing_type"])
  ```

  The second form does not read the description pages at all, which is the
  difference between a 4.6-second load and an instant one.
- `data/failure.csv` — `stock_ticker,cik,name,reason`, one row per company
  with no usable annual report of any of the six types, or that hit an
  error — with a machine-readable reason (see "Known failure modes" below).
- `data/checkpoint.sqlite3` — the resume/dedup state (one row per ticker,
  status `done` / `failed`).

**Heads up on size**: descriptions are the *full* section text, not a short
excerpt, and 40-F Annual Information Forms in particular run much longer
than a 10-K's Item 1 (SNDL's AIF business section alone was ~130K characters
in testing). Across ~5,700 companies that is ~376MB of prose.

### Why Parquet, and why compressed

Measured on the real dataset (5,679 rows, 376MB of description text):

| format | size | vs CSV |
| --- | --- | --- |
| `output.csv` | 377.5 MB | 1.00x |
| Parquet, **uncompressed** | 377.5 MB | **1.00x** |
| Parquet, snappy | 179.1 MB | 2.11x |
| Parquet, zstd-3 *(default)* | 98.4 MB | 3.83x |
| Parquet, zstd-9 | 82.4 MB | 4.58x |
| Parquet, brotli | 77.7 MB | 4.86x |

Uncompressed Parquet saves nothing here. Columnar formats get their savings
from structure — dictionary encoding, run-length encoding, delta encoding —
and this table is one long, unique string per company, so there is no
structure to exploit. Every byte of the saving comes from the codec.

That is worth being explicit about, because "compressed" here does not mean
"archived": nothing has to be unpacked before use. `pd.read_parquet()` reads
a zstd file directly, and a query that skips the `description` column never
decompresses those pages at all. It is not comparable to a `.csv.gz`, which
has to be decompressed in full and cannot be scanned by column.

zstd-3 is the default because it gives most of the available saving for
~1.3s of write time; zstd-9 costs ~4x that for another 16MB. Override with:

```bash
export EDGAR_PARQUET_COMPRESSION=zstd     # zstd | snappy | brotli | gzip | none
export EDGAR_PARQUET_COMPRESSION_LEVEL=3
export EDGAR_OUTPUT_BATCH_SIZE=500        # rows per part file
```

### Why a directory of parts, not one file

Parquet has no append — a file must be closed before it can be read — so a
single file could only be written once, at the end of a run. These runs get
interrupted, so that would mean an interrupted run producing nothing. Each
part is closed as it is written (under a temporary name, then renamed, so a
kill mid-write cannot leave a truncated file), and a resumed run adds new
parts rather than rewriting existing ones. Batching is close to free: 12
parts of 500 rows measured 98.2MB against 98.4MB for a single file.

A company is marked `done` in the checkpoint only *after* the part holding
its description is on disk. The checkpoint is what a resumed run skips on,
so the other order would let a company be marked done whose row never
reached disk — permanently skipped and silently missing. In this order the
worst case is a company extracted twice, which costs nothing, because the
filing is already cached and the retry never touches the network.

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

## Quarterly reports (`edgar_scraper.quarterly`)

A separate program that locates each company's most recent quarterly report.
It shares this one's throttled client, cached filing histories and
`data/filings/` layout, but keeps its own checkpoint table, so the two can be
run and resumed independently.

```bash
export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"

.venv/bin/python -m edgar_scraper.quarterly --limit 2      # smoke test
.venv/bin/python -m edgar_scraper.quarterly --index-only   # locate everything, store nothing
.venv/bin/python -m edgar_scraper.quarterly                # also download each document
```

Form priority mirrors the annual pipeline:

```
10-Q -> 6-K -> 10-Q/A -> 6-K/A -> failure
```

Foreign private issuers and Canadian MJDS filers never file a 10-Q — 6-K is
how they disclose interim results. Across the 7,290 cached filing histories
that gives 86.7% coverage: 5,087 companies on 10-Q, 1,233 on 6-K, 970 with
neither in the ~1,000-filing `recent` window.

**Read the 6-K rows with care.** A 6-K is a generic wrapper for anything a
foreign issuer discloses abroad, so the most recent one is often a press
release or a shareholder notice rather than interim financials — Alibaba's
was 26KB, Royal Bank of Canada's 53KB, against ~1MB for a real 10-Q. For
those companies the row means "most recent interim filing", not "quarterly
report". `primary_doc_description` and `submission_bytes` are both carried
through so a row can be judged without fetching it.

### Output

`data/quarterly/` — Parquet parts, one row per company:

| column | |
| --- | --- |
| `stock_ticker`, `cik`, `name` | the company |
| `form`, `filing_date`, `report_date` | which filing, filed when, covering which period end |
| `accession_number`, `primary_document` | EDGAR identifiers |
| `primary_doc_description` | the filer's own label for the document |
| `document_url` | direct link, usable whether or not the document was downloaded |
| `document_bytes` | what was written to disk; `0` for an `--index-only` run |
| `submission_bytes` | EDGAR's size for the whole submission, always present |

### Sizing a full download before you commit to one

`submission_bytes` is what makes this answerable without downloading
anything. Summed over the 6,320 companies that have a quarterly filing, the
submissions come to **59.5 GB**. The primary document is only part of each
submission — measured at 16% on Apple's and NVIDIA's 10-Qs — which puts a
full document download at roughly **10 GB**, on top of the 20 GB
`data/filings/` already holds.

`--index-only` avoids all of it. The index itself is a couple of MB for the
whole universe, and every row still carries `document_url`, so any individual
document can be fetched on demand later.

A download run also stops cleanly when free space falls below
`EDGAR_MIN_FREE_GB` (default 5):

```bash
export EDGAR_MIN_FREE_GB=5
```

This is not a theoretical guard. Running out mid-download leaves zero-byte
files in the shared filing cache, and `load_filing_document` used to treat
those as cache hits — so the affected companies failed on every subsequent
run and never re-downloaded. That has now been fixed in both directions: an
empty cached file is treated as a miss and re-fetched, and a run stops before
it can create one.

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

Every extraction change is measured by re-running the extractor over the
whole local filing cache (~6,100 cached annual reports) and diffing the
result against the previous run's `checkpoint.sqlite3`, which records the
description each ticker produced last time. That gives three numbers per
change — how many previously-failing companies now extract, how many
previously-succeeding ones broke, and how many changed text — and the
changed-text list is read by hand, since a change that swaps a good section
for a plausible-looking wrong one shows up nowhere else.

The rewrite described above was scored that way. See "Re-running extraction
over the cache" below for how to reproduce it.

Run `.venv/bin/python tests/test_extraction.py` to re-check the extraction
logic against real cached filings without hitting the network. Each fixture
is a filing that broke the extractor at some point, kept as the case that
pins the fix, and each asserts on the *opening words* of the result rather
than merely that something came back — every regression these caught
produced a plausible-looking string anchored on the wrong heading, which a
bare non-None check would have waved through.

Three matching refinements were added after the initial build, all found by
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
checks already have to filter out anyway.

**Boundaries close together are usually genuine, not a cross-reference
table.** An earlier version rejected any run of item numbers found within
20 lines of each other, on the theory that a real Item 1 → Item 1A gap is
pages, not lines. That was wrong, and it was the single largest source of
extraction failures: a smaller reporting company whose Item 1A reads "Not
applicable." and whose Item 1B reads "None" has a genuinely tight run, and
so does a 20-F whose Item 4.C is a short list of subsidiaries followed by an
Item 4A of "None". Roughly 45% of all extraction failures were filings whose
real boundary was found and then thrown away by this rule. The replacement
achieves the same thing positionally: take the first boundary with at least
`_MIN_SECTION_CHARS` of content in front of it. A cross-reference table
immediately after the heading has nothing in front of it and is skipped; a
real Item 1A at the end of a long Item 1 has pages in front of it and is
kept.

**Inline-XBRL fact sets and word-per-cell HTML both wreck the flattened
text.** An iXBRL filing carries a machine-readable fact set in the document
alongside the readable text; left in, that is thousands of lines of context
ids and taxonomy references ("us-gaap:SubsequentEventMember", "2026-02-26")
sitting ahead of the real body, which for Golden Minerals displaced the
document entirely as far as line-based heading detection was concerned.
Those blocks are now stripped before flattening. Separately, a few filing
agents (Toronto-Dominion\u2019s AIF) render body text with one `<div>`/table cell
per *word*, so the flattened text is a column of one- and two-word lines;
the section survives boilerplate filtering as a patchwork with words missing
mid-sentence, which reads as plausible prose and is worse than an outright
failure. Those fragments are re-joined — but only in a document that is
shredded throughout (measured as the share of characters sitting in lines
too short to be prose: ~0.47 for these filings, under 0.12 for everything
else). An earlier version merged any run of short lines wherever it appeared
and swallowed real body headings — "Item 1. Business" is itself only three
short words — costing filings that had extracted cleanly before.

## Known failure modes

`reason` in `failure.csv` is one of:

- **`no_annual_report_found`** — the company has never filed any of 10-K,
  20-F, 40-F, or their amended variants. Expected for entities in
  `company_tickers.json` that aren't operating companies with a standard
  annual report at all (e.g. certain funds/trusts), or file under a
  different regime entirely (e.g. 6-K-only foreign issuers with no 20-F on
  file).
- **`business_section_not_found (<form>)`** — the company has a filing of
  that type, but no heading from any tier could be found in it, so the
  script fails rather than guessing. What is left after the rewrite is a
  small residue (~0.7% of cached filings), mostly:
  - **20-F filers whose narrative is incorporated by reference.** The Form
    20-F itself is a few pages of cross-references pointing at a separately
    filed annual report or an exhibit ("The information set forth under
    Section 3: Our Business of the 2025 Annual Report is incorporated
    herein by reference"). There is no business section in the document to
    find. Following those references into the exhibit would be the next
    step here, and is the same mechanism 40-F already uses.
  - **40-F filings whose AIF is not among the first few exhibits tried**,
    or whose exhibit is a scanned/paper submission.
  - **Filings whose primary document is not the annual report** — a handful
    of submissions list a 10-Q or an unrelated document as
    `primaryDocument` in the submissions JSON.

  Note the deliberate change in policy here: where a heading *is* found but
  no trustworthy boundary follows it, the section is now taken on a
  best-effort basis (looser boundaries, then a character cap) rather than
  discarded. That is what recovers the Intel/Citigroup/McDonald's class of
  filer, which reorders the report under its own headings and maps back to
  item numbers only in a cross-reference index. The trade is that a
  best-effort description can open with front matter before reaching the
  narrative — ASML's opens with its forward-looking-statements note — and
  can run longer than the section proper. `description` is the only signal
  of this; there is no separate confidence column.
- **`40f_aif_exhibit_not_found`** — the 40-F's filing index doesn't have an
  exhibit that looks like an Annual Information Form. Candidates are ranked
  by exhibit description first (an explicit "Annual Information Form" wins
  outright; a description naming something else — financial statements,
  MD&A, a consent, a certification — is ruled out, which is what stops
  Ballard Power's financial statements being handed to the extractor
  because they happen to sit at `EX-99.1`), then by exhibit-number
  convention (`EX-99.1`, `EX-1`, and variants). The top few are fetched in
  order and the first one a business section can be read out of is kept.
- **`submissions_fetch_failed:` / `filing_fetch_failed:` / `40f_index_fetch_failed:`
  / `extraction_error:` / `unexpected:`** — network error, malformed filing,
  or an unexpected exception; these should be rare. Each includes the
  underlying error message so a spot check is possible.

The largest remaining gap is not extraction at all but
`no_annual_report_found`: the SEC submissions JSON lists only the ~1,000
most recent filings inline, and older 10-K/20-F/40-Fs live in paginated
files under `filings.files[]` that this script never reads. Companies that
file infrequently, or that file a high volume of other forms, therefore look
like they have no annual report. Closing that means following those pages,
which needs fresh SEC requests rather than the local cache.

## Re-running extraction over the cache

Extraction failures re-run entirely from `data/filings/` — the document is
already downloaded, only the parse failed — so retrying them costs no SEC
requests:

```bash
export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"
.venv/bin/python -m edgar_scraper --retry-failed
```

This re-attempts every ticker currently marked `failed`, including the
`no_annual_report_found` ones (which do hit the network, since there is
nothing cached for them). Companies already marked `done` are skipped, so
an improved extractor does **not** revisit them by itself. To re-extract
everything — the right move after a change like the one above, since it also
improves descriptions for companies that were already succeeding — clear the
`done` rows first, keeping the downloaded filings:

```bash
cp data/checkpoint.sqlite3 data/checkpoint.previous.sqlite3   # keep the old descriptions
mv data/output data/output.previous
sqlite3 data/checkpoint.sqlite3 "DELETE FROM tickers"
.venv/bin/python -m edgar_scraper
```

Keep `data/output.previous/`: diffing the new descriptions against the old
ones per ticker is how the "Tested against" numbers above are produced, and
it is the only way a change that silently swaps a good section for a wrong
one gets caught.

To rebuild `data/output/` from the checkpoint without re-reading a single
filing — after an extraction change, or to migrate a run that predates the
Parquet output:

```bash
.venv/bin/python -m edgar_scraper --export-parquet
```

## Reclaiming the duplicated 376MB

The description text is currently stored twice: once in `data/output/` and
once in the `description` column of `checkpoint.sqlite3`, which is why that
database is 365MB for what is otherwise a few thousand rows of status. The
duplication is deliberate for now — the checkpoint is the durable per-company
record and the only thing `--export-parquet` can rebuild from — but nothing
in the scraper *reads* that column back, so it can be dropped once you are
satisfied the Parquet output is good:

```bash
.venv/bin/python -m edgar_scraper --export-parquet   # confirm it is current first
sqlite3 data/checkpoint.sqlite3 "UPDATE tickers SET description = NULL; VACUUM;"
```

That takes the checkpoint from 365MB to a few MB and leaves resume, retry
and `--tickers` working exactly as before. It does mean `--export-parquet`
can no longer rebuild the output, so do it only with `data/output/` backed
up. The old `data/output.csv` (360MB) is superseded once you have verified
the Parquet — it holds nothing the Parquet does not, and is in fact missing
two rows (`LNZAW`, `CTAAR`) that the checkpoint recorded but the CSV append
lost during a disk-full event.
