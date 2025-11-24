#!/usr/bin/env python3
"""Small helper to validate, deduplicate, and trim translation_examples.jsonl.

Usage examples:
    # Validate only (default)
    python -m app.utils.maintain_translation_examples

    # Validate + dedupe + keep last 400 rows (in place)
    python -m app.utils.maintain_translation_examples --dedupe --keep 400

    # Write cleaned copy to a new file
    python -m app.utils.maintain_translation_examples --out /tmp/clean.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable, Tuple


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_LOG = DATA_DIR / "translation_examples.jsonl"


def _load_examples(path: Path) -> Tuple[List[dict[str, Any]], int]:
    """Return (records, invalid_count). Keeps line order."""
    records: list[dict[str, Any]] = []
    invalid = 0
    if not path.exists():
        return records, invalid

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue

            stt = (rec.get("stt_text") or "").strip()
            final = (rec.get("final_translation") or rec.get("auto_translation") or "").strip()
            if not stt or not final:
                invalid += 1
                continue

            records.append(rec)

    return records, invalid


def _dedupe(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop earlier duplicates by (stt_text, final_translation), keep newest."""
    seen = set()
    unique_reversed: list[dict[str, Any]] = []
    for rec in reversed(list(records)):
        key = (
            (rec.get("stt_text") or "").strip(),
            (rec.get("final_translation") or rec.get("auto_translation") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_reversed.append(rec)
    return list(reversed(unique_reversed))


def _trim_tail(records: list[dict[str, Any]], keep: int | None) -> list[dict[str, Any]]:
    if keep is None or keep <= 0:
        return records
    return records[-keep:]


def _write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="input translation_examples.jsonl")
    parser.add_argument("--out", type=Path, default=None, help="output path (defaults to in-place)")
    parser.add_argument("--dedupe", action="store_true", help="remove duplicates by stt_text + final_translation")
    parser.add_argument("--keep", type=int, default=None, help="keep only the last N rows after cleaning")
    args = parser.parse_args()

    out_path = args.out or args.log

    records, invalid = _load_examples(args.log)
    before = len(records)

    if args.dedupe:
        records = _dedupe(records)

    records = _trim_tail(records, args.keep)

    _write_jsonl(records, out_path)

    print(
        f"Input rows (valid): {before}\n"
        f"Invalid rows dropped: {invalid}\n"
        f"After dedupe/trim: {len(records)}\n"
        f"Written to: {out_path}"
    )


if __name__ == "__main__":
    main()
