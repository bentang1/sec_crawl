"""Batched Parquet writer for the extracted descriptions.

`output.csv` was 377MB for 376MB of description text - CSV stores long prose
at essentially its raw size, and the same text was also being kept in
`checkpoint.sqlite3`, so the run held two full copies. Parquet with zstd
brings the same 5,679 rows to 98MB (measured, 3.8x) while staying directly
readable - `pd.read_parquet("data/output/")` needs no decompression step,
and a query that doesn't touch `description` never reads those pages at all.

Note that *uncompressed* Parquet saves nothing here (measured at 377.5MB,
byte for byte the same as the CSV): the payload is one long, unique string
per company, so there is no dictionary or run-length structure for columnar
encoding to exploit. All of the saving comes from the codec.

Output is a directory of numbered part files rather than one file, because
Parquet has no append: a file has to be closed to be readable, so a single
file could only be written once at the end of a run, and a run that was
interrupted - which these runs are, routinely - would leave nothing behind.
Each part is closed as it is written, so an interrupted run keeps every part
it finished. Batching costs almost nothing: 12 parts of 500 rows measured
98.2MB against 98.4MB for one file.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# filing_type is one of six values across the whole dataset, so it is stored
# as a dictionary; description is large_string because a single 20-F business
# section can exceed the 2GB offset range plain string uses for a whole column.
SCHEMA = pa.schema(
    [
        pa.field("stock_ticker", pa.string()),
        pa.field("name", pa.string()),
        pa.field("filing_type", pa.dictionary(pa.int8(), pa.string())),
        pa.field("description", pa.large_string()),
    ]
)

# The quarterly index (edgar_scraper/quarterly.py) is one short row per
# company rather than a wall of prose, so every column here is small and
# highly repetitive - form, dates and the URL prefix all dictionary-encode
# down to near nothing.
QUARTERLY_SCHEMA = pa.schema(
    [
        pa.field("stock_ticker", pa.string()),
        pa.field("cik", pa.string()),
        pa.field("name", pa.string()),
        pa.field("form", pa.dictionary(pa.int8(), pa.string())),
        pa.field("filing_date", pa.string()),
        pa.field("report_date", pa.string()),
        pa.field("accession_number", pa.string()),
        pa.field("primary_document", pa.string()),
        pa.field("primary_doc_description", pa.string()),
        pa.field("document_url", pa.string()),
        pa.field("document_bytes", pa.int64()),
        pa.field("submission_bytes", pa.int64()),
        pa.field("stored_path", pa.string()),
    ]
)

PART_GLOB = "part-*.parquet"


class ParquetOutput:
    """Buffers completed companies and writes them out a part at a time.

    `flush()` returns the rows it wrote so the caller can checkpoint them
    only once they are durable - see `ScraperRun._handle_one` for why that
    ordering matters.
    """

    def __init__(
        self,
        directory: Path,
        batch_size: int,
        compression: str,
        compression_level: int | None,
        schema: pa.Schema = SCHEMA,
    ):
        self.schema = schema
        self.directory = directory
        self.batch_size = batch_size
        self.compression = compression
        self.compression_level = compression_level
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._buffer: list[dict[str, Any]] = []
        self._next_part = self._first_unused_part()

    def _first_unused_part(self) -> int:
        """Numbering continues past whatever a previous run left behind, so a
        resumed run adds parts instead of overwriting them."""
        existing = [p.stem for p in self.directory.glob(PART_GLOB)]
        numbers = [int(stem.rsplit("-", 1)[-1]) for stem in existing if stem.rsplit("-", 1)[-1].isdigit()]
        return max(numbers) + 1 if numbers else 0

    def add(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Buffers one row; returns the rows written if this filled a part.

        A row may carry keys the schema does not have - the annual writer
        passes `cik` so the caller can checkpoint the company once the part is
        durable, without `cik` becoming an output column.
        """
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) < self.batch_size:
                return []
        return self.flush()

    def flush(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self._buffer:
                return []
            rows, self._buffer = self._buffer, []
            path = self.directory / f"part-{self._next_part:05d}.parquet"
            self._next_part += 1

        # Written under a temporary name and renamed, so a kill mid-write
        # cannot leave a truncated part that later reads would choke on.
        table = pa.Table.from_arrays(
            [pa.array([row[field.name] for row in rows], field.type) for field in self.schema],
            schema=self.schema,
        )
        temporary = path.with_suffix(".parquet.partial")
        pq.write_table(
            table,
            temporary,
            compression=self.compression,
            compression_level=self.compression_level,
        )
        temporary.rename(path)
        return rows


def read_output(directory: Path, schema: pa.Schema = SCHEMA) -> pa.Table:
    """Every part file as one table. Equivalent to `pd.read_parquet(directory)`."""
    if not sorted(directory.glob(PART_GLOB)):
        return schema.empty_table()
    return pq.read_table(directory, schema=schema)
