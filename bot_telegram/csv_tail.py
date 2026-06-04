"""
csv_tail.py — tail incrementale dei CSV con offset persistente (anti-repost).
Legge solo le righe COMPLETE (terminate da newline) aggiunte dopo l'ultimo offset.
Al primo avvio salta lo storico (seek a fine file) per non spammare i segnali vecchi.
Lavora in modalità binaria: gli offset sono byte → coerenti anche con UTF-8.
"""
from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Iterator

import store

log = logging.getLogger("csv_tail")

_OFFSETS_FILE = "offsets.json"


def _header_byte_len(path: Path) -> int:
    with open(path, "rb") as f:
        return len(f.readline())


class CsvTailer:
    """Tail di un singolo CSV. Mantiene offset byte + header."""

    def __init__(self, path: Path, key: str, skip_backlog: bool = True):
        self.path = Path(path)
        self.key = key
        self.skip_backlog = skip_backlog
        self._header: list[str] | None = None

    def _offsets(self) -> dict:
        return store.load(_OFFSETS_FILE, {})

    def _get_offset(self):
        return self._offsets().get(self.key)

    def _set_offset(self, value: int) -> None:
        data = self._offsets()
        data[self.key] = int(value)
        store.save(_OFFSETS_FILE, data)

    def _read_header(self) -> list[str] | None:
        if self._header is not None:
            return self._header
        if not self.path.exists():
            return None
        try:
            with open(self.path, "rb") as f:
                first = f.readline().decode("utf-8", "replace")
            if first:
                self._header = next(csv.reader(io.StringIO(first)))
        except (OSError, StopIteration):
            return None
        return self._header

    def new_rows(self) -> Iterator[dict]:
        """Yield dei nuovi record come dict {colonna: valore}."""
        if not self.path.exists():
            return
        header = self._read_header()
        if not header:
            return

        size = self.path.stat().st_size
        offset = self._get_offset()

        # primo avvio: salta il backlog (parti da fine file) oppure da dopo l'header
        if offset is None:
            self._set_offset(size if self.skip_backlog else _header_byte_len(self.path))
            return

        # file troncato/ruotato → riparti da dopo l'header
        if offset > size:
            offset = _header_byte_len(self.path)

        if offset >= size:
            return

        with open(self.path, "rb") as f:
            f.seek(offset)
            raw = f.read()

        # consuma solo righe complete (l'ultima senza \n è parziale → la lascio)
        last_nl = raw.rfind(b"\n")
        if last_nl == -1:
            return
        complete = raw[: last_nl + 1]
        text = complete.decode("utf-8", "replace")

        for parts in csv.reader(io.StringIO(text)):
            if not parts or len(parts) < 2:
                continue
            if parts[0] == header[0]:          # eventuale header ripetuto
                continue
            yield dict(zip(header, parts))

        self._set_offset(offset + len(complete))
