"""Configurazione condivisa per le chiamate alla Meta Graph API."""

from __future__ import annotations

import os
import re


def graph_version() -> str:
    value = os.getenv("META_GRAPH_VERSION", "v24.0").strip()
    if not re.fullmatch(r"v\d+\.\d+", value):
        raise ValueError("META_GRAPH_VERSION deve avere formato vNN.N")
    return value


def graph_url(resource: str) -> str:
    return f"https://graph.facebook.com/{graph_version()}/{resource.lstrip('/')}"
