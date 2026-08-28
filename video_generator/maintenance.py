"""Retention sicura degli artefatti. Senza --apply mostra soltanto cosa farebbe."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def old_files(directory: Path, days: int):
    cutoff = time.time() - days * 86400
    if not directory.exists():
        return []
    return [p for p in directory.rglob("*") if p.is_file() and p.stat().st_mtime < cutoff]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Retention dei file temporanei")
    parser.add_argument("--output-days", type=int, default=30, help="Retention dei video in output")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.days < 7 or args.output_days < 7:
        parser.error("La retention minima consentita è 7 giorni")

    groups = [
        ("temporanei", ROOT / "temp", args.days),
        ("output", ROOT / "output", args.output_days),
    ]
    for label, directory, days in groups:
        targets = old_files(directory, days)
        total_bytes = sum(path.stat().st_size for path in targets)
        print(
            f"Artefatti {label} oltre {days} giorni: {len(targets)} "
            f"({total_bytes / 1024 / 1024:.1f} MiB)"
        )
        for path in targets:
            print(path.relative_to(ROOT))
            if args.apply:
                path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
