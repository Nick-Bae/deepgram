from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.script_store import script_store

router = APIRouter(prefix="/script", tags=["script"])


class Pair(BaseModel):
    source: str = Field(..., min_length=1, description="Korean source text")
    target: str = Field(..., min_length=1, description="English target text")


class UploadPayload(BaseModel):
    payload: dict
    cfg: dict | None = None


@router.get("")
def script_status():
    count, threshold, version = script_store.stats()
    return {"count": count, "threshold": threshold, "version": version}


@router.post("/upload")
def upload_script(body: UploadPayload):
    pairs_raw = (body.payload or {}).get("pairs")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise HTTPException(status_code=400, detail="payload.pairs must be a non-empty list")

    # Validate + normalize pairs
    pairs: list[dict[str, str]] = []
    for item in pairs_raw:
        src = (item.get("source") or "").strip()
        tgt = (item.get("target") or "").strip()
        if not src or not tgt:
            continue
        pairs.append({"source": src, "target": tgt})

    if not pairs:
        raise HTTPException(status_code=400, detail="No valid source/target pairs provided")

    threshold = None
    if body.cfg and "threshold" in body.cfg:
        try:
            threshold = float(body.cfg["threshold"])
        except Exception as exc:  # pragma: no cover - defensive
            raise HTTPException(status_code=400, detail=f"Invalid threshold: {exc}")

    loaded, used_threshold, version = script_store.load(pairs, threshold)
    return {"loaded": loaded, "threshold": used_threshold, "version": version}


@router.delete("")
def clear_script():
    removed, version = script_store.clear()
    return {"cleared": True, "removed": removed, "version": version}
