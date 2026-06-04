"""
store.py — persistenza JSON atomica per lo stato del bot (offset, abbonati, ecc.).
Scrittura atomica via file temporaneo + os.replace per evitare corruzione su crash.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from config import STATE_DIR

_LOCK = threading.RLock()


def _path(name: str) -> Path:
    return STATE_DIR / name


def load(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.exists():
        return default
    try:
        with _LOCK, open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save(name: str, data: Any) -> None:
    p = _path(name)
    with _LOCK:
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
