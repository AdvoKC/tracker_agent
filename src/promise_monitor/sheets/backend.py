"""Spreadsheet access behind a tiny interface: real Google Sheets (gspread) or in-memory.

Writes always use RAW input, so text a coordinator or the Writer produced (e.g. a body
starting with "=") is stored as text and never evaluated as a formula.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from gspread.utils import ValueInputOption

Cell = tuple[int, int, str]  # (row, col) 1-based, value


class SheetsBackend(Protocol):
    def tab_titles(self) -> list[str]: ...
    def create_tab(self, title: str, header: Sequence[str]) -> None: ...
    def read(self, title: str) -> list[list[str]]: ...
    def append(self, title: str, rows: Sequence[Sequence[str]]) -> None: ...
    def write_cells(self, title: str, cells: Sequence[Cell]) -> None: ...


def a1(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


class MemorySheets:
    """In-memory spreadsheet for tests and dry runs."""

    def __init__(self) -> None:
        self.tabs: dict[str, list[list[str]]] = {}
        self.write_calls = 0

    def tab_titles(self) -> list[str]:
        return list(self.tabs)

    def create_tab(self, title: str, header: Sequence[str]) -> None:
        self.tabs.setdefault(title, [list(header)])

    def read(self, title: str) -> list[list[str]]:
        return [list(r) for r in self.tabs[title]]

    def append(self, title: str, rows: Sequence[Sequence[str]]) -> None:
        self.write_calls += 1
        self.tabs[title].extend([list(r) for r in rows])

    def write_cells(self, title: str, cells: Sequence[Cell]) -> None:
        self.write_calls += 1
        grid = self.tabs[title]
        for row, col, value in cells:
            while len(grid) < row:
                grid.append([])
            line = grid[row - 1]
            while len(line) < col:
                line.append("")
            line[col - 1] = value


class GSpreadBackend:
    """Google Sheets via a service account. One spreadsheet; tabs = worksheets."""

    def __init__(self, spreadsheet_id: str, service_account_file: Path) -> None:
        import gspread

        client = gspread.service_account(filename=str(service_account_file))
        self._sh = client.open_by_key(spreadsheet_id)
        self._ws = {ws.title: ws for ws in self._sh.worksheets()}

    def tab_titles(self) -> list[str]:
        return list(self._ws)

    def create_tab(self, title: str, header: Sequence[str]) -> None:
        if title in self._ws:
            return
        ws = self._sh.add_worksheet(title=title, rows=200, cols=max(len(header), 10))
        ws.update([list(header)], "A1", value_input_option=ValueInputOption.raw)
        ws.freeze(rows=1)
        self._ws[title] = ws

    def read(self, title: str) -> list[list[str]]:
        return [[str(c) for c in row] for row in self._ws[title].get_all_values()]

    def append(self, title: str, rows: Sequence[Sequence[str]]) -> None:
        if rows:
            self._ws[title].append_rows(
                [list(r) for r in rows], value_input_option=ValueInputOption.raw
            )

    def write_cells(self, title: str, cells: Sequence[Cell]) -> None:
        if cells:
            self._ws[title].batch_update(
                [{"range": a1(r, c), "values": [[v]]} for r, c, v in cells],
                value_input_option=ValueInputOption.raw,
            )
