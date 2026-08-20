"""Local filing cache, keyed by CIK + accession number.

Layout mirrors EDGAR's own URL structure so it can be reused/merged with the
separate document-database task (Task 1 of the broader PRD), which will also
be pulling 8-K/10-K/10-Q filings by CIK + accession number for the team's
stock universe:

    data/
      company_tickers.json                        # raw SEC ticker/CIK/title list
      submissions/{cik10}.json                     # cached filing history per company
      filings/{cik10}/{accession_no_dashes}/
          metadata.json                            # form, filingDate, primaryDocument, ticker(s)...
          {primaryDocument}                        # the raw filing document as downloaded

A second consumer can walk `filings/` for any form type it cares about
(8-K, 10-Q, ...) without touching this script, and new downloads just add
more accession-number subdirectories under the same CIK.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config


class FilingCache:
    def __init__(self, config: Config):
        self.config = config
        self.config.submissions_dir.mkdir(parents=True, exist_ok=True)
        self.config.filings_dir.mkdir(parents=True, exist_ok=True)

    # -- company_tickers.json -------------------------------------------------

    def load_company_tickers(self) -> dict[str, Any] | None:
        path = self.config.tickers_json_path
        if path.exists():
            return json.loads(path.read_text())
        return None

    def save_company_tickers(self, data: dict[str, Any]) -> None:
        self.config.tickers_json_path.write_text(json.dumps(data))

    # -- per-CIK submissions history -------------------------------------------

    def submissions_path(self, cik10: str) -> Path:
        return self.config.submissions_dir / f"{cik10}.json"

    def load_submissions(self, cik10: str) -> dict[str, Any] | None:
        path = self.submissions_path(cik10)
        if path.exists():
            return json.loads(path.read_text())
        return None

    def save_submissions(self, cik10: str, data: dict[str, Any]) -> None:
        self.submissions_path(cik10).write_text(json.dumps(data))

    # -- individual filings ----------------------------------------------------

    def filing_dir(self, cik10: str, accession_no_dashes: str) -> Path:
        return self.config.filings_dir / cik10 / accession_no_dashes

    def load_filing_document(self, cik10: str, accession_no_dashes: str, primary_document: str) -> bytes | None:
        path = self.filing_dir(cik10, accession_no_dashes) / primary_document
        if path.exists():
            return path.read_bytes()
        return None

    def save_filing_document(
        self,
        cik10: str,
        accession_no_dashes: str,
        primary_document: str,
        content: bytes,
        metadata: dict[str, Any],
    ) -> None:
        directory = self.filing_dir(cik10, accession_no_dashes)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / primary_document).write_bytes(content)
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2))
