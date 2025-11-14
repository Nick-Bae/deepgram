#!/usr/bin/env python3
"""
Utility to produce curated few-shot examples from translation_examples.jsonl.

Usage:
    python -m app.utils.export_translation_examples --source ko --target en --max 6
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_LOG = DATA_DIR / "translation_examples.jsonl"
DEFAULT_OUTPUT = DATA_DIR / "fewshot_examples.json"


def iter_examples(log_path: Path) -> Iterable[dict[str, Any]]:
    if not log_path.exists():
        return []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="source language code (e.g., ko)")
    parser.add_argument("--target", required=True, help="target language code (e.g., en)")
    parser.add_argument("--max", type=int, default=6, help="max examples to export")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="path to translation_examples.jsonl")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output JSON file")
    parser.add_argument("--include-auto", action="store_true", help="allow auto translations if no corrections exist")
    args = parser.parse_args()

    corrected: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for record in iter_examples(args.log):
        if record.get("source_lang") != args.source or record.get("target_lang") != args.target:
            continue
        pair = {
            "source_lang": record.get("source_lang"),
            "target_lang": record.get("target_lang"),
            "stt_text": record.get("stt_text"),
            "final_translation": record.get("final_translation") or record.get("auto_translation"),
            "auto_translation": record.get("auto_translation"),
            "corrected": bool(record.get("corrected")),
        }
        if not pair["stt_text"] or not pair["final_translation"]:
            continue
        if pair["corrected"]:
            corrected.append(pair)
        else:
            fallback.append(pair)

    examples = corrected[-args.max :]
    if len(examples) < args.max and args.include_auto:
        needed = args.max - len(examples)
        examples = corrected + fallback[-needed:]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        json.dump(examples[-args.max :], fh, ensure_ascii=False, indent=2)

    print(f"Exported {len(examples[-args.max :])} examples to {args.output}")


if __name__ == "__main__":
    main()

