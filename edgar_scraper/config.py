"""Configuration for the EDGAR scraper, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    user_agent: str
    data_dir: Path
    requests_per_second: float
    max_workers: int
    parquet_compression: str
    parquet_compression_level: int | None
    output_batch_size: int
    min_free_gb: float

    @property
    def tickers_json_path(self) -> Path:
        return self.data_dir / "company_tickers.json"

    @property
    def submissions_dir(self) -> Path:
        return self.data_dir / "submissions"

    @property
    def filings_dir(self) -> Path:
        return self.data_dir / "filings"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoint.sqlite3"

    @property
    def quarterly_output_dir(self) -> Path:
        """Index written by edgar_scraper/quarterly.py."""
        return self.data_dir / "quarterly"

    @property
    def output_dir(self) -> Path:
        """Directory of Parquet part files - see edgar_scraper/output.py."""
        return self.data_dir / "output"

    @property
    def failure_csv_path(self) -> Path:
        return self.data_dir / "failure.csv"


def load_config() -> Config:
    user_agent = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError(
            "EDGAR_USER_AGENT is not set. SEC requires a descriptive User-Agent "
            'header of the form "AppName contact@email.com" on every request to '
            "sec.gov / data.sec.gov. Set it before running, e.g.:\n\n"
            '  export EDGAR_USER_AGENT="EdgarScraper tangarthur14@gmail.com"\n'
        )
    if "@" not in user_agent:
        raise RuntimeError(
            f'EDGAR_USER_AGENT="{user_agent}" does not look like it contains a '
            'contact email. SEC expects the form "AppName contact@email.com".'
        )

    data_dir = Path(os.environ.get("EDGAR_DATA_DIR", str(PACKAGE_ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    rps = float(os.environ.get("EDGAR_REQUESTS_PER_SECOND", "10"))
    max_workers = int(os.environ.get("EDGAR_MAX_WORKERS", "8"))

    # zstd at level 3 measured 3.8x smaller than the CSV on the real dataset
    # (377MB -> 98MB) for ~1.3s of write time; level 9 reaches 4.6x but takes
    # ~4x longer, and "none" saves nothing at all because the payload is one
    # long unique string per company. See edgar_scraper/output.py.
    compression = os.environ.get("EDGAR_PARQUET_COMPRESSION", "zstd").lower()
    level_setting = os.environ.get("EDGAR_PARQUET_COMPRESSION_LEVEL", "3")
    level = int(level_setting) if compression in {"zstd", "brotli", "gzip"} else None
    batch_size = int(os.environ.get("EDGAR_OUTPUT_BATCH_SIZE", "500"))

    # A run stops rather than filling the volume. Running out mid-download
    # leaves zero-byte files in the shared filing cache, and those read back
    # as cache hits, so the affected companies fail on every later run until
    # someone notices - which is exactly what happened once already.
    min_free_gb = float(os.environ.get("EDGAR_MIN_FREE_GB", "5"))

    return Config(
        user_agent=user_agent,
        data_dir=data_dir,
        requests_per_second=rps,
        max_workers=max_workers,
        parquet_compression=compression,
        parquet_compression_level=level,
        output_batch_size=batch_size,
        min_free_gb=min_free_gb,
    )
