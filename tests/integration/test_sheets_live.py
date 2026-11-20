"""Live Google Sheets round trip (P6 exit). Skipped unless a TEST spreadsheet is configured:

    PM_TEST_SPREADSHEET_ID=<a throwaway sheet shared with the service account>
    PM_GOOGLE_SERVICE_ACCOUNT_FILE=<path to the JSON key>

Never point PM_TEST_SPREADSHEET_ID at the real coordinators' sheet.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from promise_monitor.sheets.backend import GSpreadBackend

pytestmark = pytest.mark.integration

SHEET_ID = os.environ.get("PM_TEST_SPREADSHEET_ID")
KEY = os.environ.get("PM_GOOGLE_SERVICE_ACCOUNT_FILE")


@pytest.mark.skipif(not (SHEET_ID and KEY), reason="test spreadsheet not configured")
def test_round_trip_and_raw_writes() -> None:
    assert SHEET_ID and KEY
    backend = GSpreadBackend(SHEET_ID, Path(KEY))
    title = f"pytest-{uuid.uuid4().hex[:8]}"
    try:
        backend.create_tab(title, ["article_id", "body_markdown", "decision"])
        backend.append(title, [["a1", "=1+1", ""]])
        backend.write_cells(title, [(2, 3, "approve")])
        rows = backend.read(title)
        assert rows[0] == ["article_id", "body_markdown", "decision"]
        assert rows[1] == ["a1", "=1+1", "approve"]  # stored as text, not evaluated to 2
        assert title in backend.tab_titles()
    finally:
        ws = backend._ws.get(title)
        if ws is not None:
            backend._sh.del_worksheet(ws)
