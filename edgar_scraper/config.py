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
    def output_csv_path(self) -> Path:
        return self.data_dir / "output.csv"

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

    return Config(
        user_agent=user_agent,
        data_dir=data_dir,
        requests_per_second=rps,
        max_workers=max_workers,
    )
