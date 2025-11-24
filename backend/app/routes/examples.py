import json
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_PATH = DATA_DIR / "translation_examples.jsonl"
FEWSHOT_PATH = DATA_DIR / "fewshot_examples.json"

router = APIRouter(prefix="/examples", tags=["examples"])


class UpdatePayload(BaseModel):
    timestamp: str = Field(..., description="timestamp of the record to update")
    final_translation: str
    corrected: Optional[bool] = True


class DeletePayload(BaseModel):
    timestamp: str


class ExportPayload(BaseModel):
    source: str
    target: str
    max: int = 6
    include_auto: bool = False


class CleanPayload(BaseModel):
    dedupe: bool = True
    keep: Optional[int] = 400


def _load_records() -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    invalid = 0
    if not LOG_PATH.exists():
        return records, invalid

    with LOG_PATH.open(encoding="utf-8") as fh:
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


def _write_records(records: List[dict[str, Any]]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(LOG_PATH)


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    unique_reversed: list[dict[str, Any]] = []
    for rec in reversed(records):
        key = (
            (rec.get("stt_text") or "").strip(),
            (rec.get("final_translation") or rec.get("auto_translation") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_reversed.append(rec)
    return list(reversed(unique_reversed))


@router.get("")
def list_examples(
    source: Optional[str] = None,
    target: Optional[str] = None,
    corrected: Optional[bool] = None,
    search: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    sort: str = "desc",
):
    records, invalid = _load_records()

    def matches(rec: dict[str, Any]) -> bool:
        if source and rec.get("source_lang") != source:
            return False
        if target and rec.get("target_lang") != target:
            return False
        if corrected is not None and bool(rec.get("corrected")) != corrected:
            return False
        if search:
            needle = search.lower()
            hay = " ".join(
                [
                    str(rec.get("stt_text", "")),
                    str(rec.get("auto_translation", "")),
                    str(rec.get("final_translation", "")),
                ]
            ).lower()
            if needle not in hay:
                return False
        return True

    filtered = [r for r in records if matches(r)]
    if sort == "asc":
        filtered.sort(key=lambda r: r.get("timestamp", ""))
    else:
        filtered.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    total = len(filtered)
    items = filtered[offset : offset + limit]
    return {
        "total": total,
        "invalid": invalid,
        "items": items,
    }


@router.post("/update")
def update_example(payload: UpdatePayload):
    records, invalid = _load_records()
    updated = False
    for rec in records:
        if rec.get("timestamp") == payload.timestamp:
            rec["final_translation"] = payload.final_translation
            rec["corrected"] = True if payload.corrected is None else payload.corrected
            updated = True
            break
    if not updated:
        raise HTTPException(status_code=404, detail="Record not found")
    _write_records(records)
    return {"updated": True, "invalid": invalid}


@router.delete("")
def delete_example(payload: DeletePayload):
    records, invalid = _load_records()
    before = len(records)
    records = [r for r in records if r.get("timestamp") != payload.timestamp]
    if len(records) == before:
        raise HTTPException(status_code=404, detail="Record not found")
    _write_records(records)
    return {"deleted": before - len(records), "remaining": len(records), "invalid": invalid}


@router.post("/export")
def export_examples(payload: ExportPayload):
    from app.utils.export_translation_examples import iter_examples

    corrected: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    for record in iter_examples(LOG_PATH):
        if record.get("source_lang") != payload.source or record.get("target_lang") != payload.target:
            continue
        final = record.get("final_translation") or record.get("auto_translation")
        if not final:
            continue
        if record.get("corrected"):
            corrected.append(record)
        else:
            fallback.append(record)

    examples = corrected[-payload.max :]
    if len(examples) < payload.max and payload.include_auto:
        needed = payload.max - len(examples)
        examples = corrected + fallback[-needed:]

    FEWSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FEWSHOT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(examples[-payload.max :], fh, ensure_ascii=False, indent=2)

    return {
        "exported": len(examples[-payload.max :]),
        "output": str(FEWSHOT_PATH),
        "items": examples[-payload.max :],
    }


@router.post("/clean")
def clean_examples(payload: CleanPayload):
    records, invalid = _load_records()
    before = len(records)
    if payload.dedupe:
        records = _dedupe(records)
    if payload.keep and payload.keep > 0:
        records = records[-payload.keep :]

    _write_records(records)
    return {
        "before": before,
        "after": len(records),
        "invalid_dropped": invalid,
        "deduped": payload.dedupe,
        "kept": payload.keep,
    }
