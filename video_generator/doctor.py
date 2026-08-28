"""Diagnostica locale non distruttiva per la pipeline video."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def run_checks() -> list[dict]:
    load_dotenv(ROOT / ".env")
    checks = []

    def add(name: str, ok: bool, detail: str, required: bool = True):
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail})

    add("agy", shutil.which("agy") is not None, "CLI locale per generazione editoriale")
    add("ffmpeg", bool(getattr(__import__("config"), "_FFMPEG_BIN", None)), "encoder video")
    add("voice_sample", (ROOT / "assets/voices/mia_voce.wav").exists(), "campione XTTS")
    add("instagram_credentials", bool(os.getenv("IG_USER_ID") and os.getenv("IG_ACCESS_TOKEN")), "IG_USER_ID + IG_ACCESS_TOKEN", required=False)
    add("public_media_host", bool(os.getenv("PUBLIC_MEDIA_HOST")), "raccomandato; fallback VPS ancora attivo", required=False)
    add("tiktok_session", (ROOT / "chrome_profile").exists() or (ROOT / "cookies.txt").exists(), "profilo o cookie", required=False)
    add("conscia_mente", Path("/home/ubuntu/conscia-mente/scripts/generate-article.mjs").exists(), "integrazione sito", required=False)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = run_checks()
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            mark = "OK" if check["ok"] else ("WARN" if not check["required"] else "FAIL")
            print(f"[{mark}] {check['name']}: {check['detail']}")
    return 1 if any(not c["ok"] and c["required"] for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
