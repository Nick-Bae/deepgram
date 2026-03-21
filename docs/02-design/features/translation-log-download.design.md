# Design: translation-log-download

> Reference: `docs/01-plan/features/translation-log-download.plan.md`

## 1. Overview

Persist final Korean→English translation segments to Firestore during a live room session, then expose a CSV download endpoint accessible from the host console after the service ends.

Two new surfaces:
1. **Backend write path** — async Firestore write after each final segment broadcast
2. **Backend export route** — `GET /org/{org_id}/room/{room_id}/segments/export` → CSV
3. **Frontend button** — "Download Translation Log" in the service card for ended rooms

---

## 2. Firestore Schema

### New subcollection: `segments`

```
organizations/{orgId}/rooms/{roomId}/segments/{seq}/
  seq:          number    # Segment sequence number (1, 2, 3...)
  timestamp:    string    # ISO 8601 UTC (e.g. "2026-03-16T11:04:23.412Z")
  koreanText:   string    # Raw STT text (after preprocessing)
  englishText:  string    # Translated output
  mode:         string    # "live" | "pre" | "scripture"
  matchScore:   number?   # Only present when mode = "pre" (0.0–1.0)
```

### Firestore security rules addition

```firestore
match /rooms/{roomId} {
  allow read: if true;
  allow create, update, delete: if false;

  match /finalTranscript/{docId} {
    allow read: if isOrgMember(orgId);
    allow create, update, delete: if false;
  }

  // NEW
  match /segments/{segId} {
    allow read: if isOrgMember(orgId);
    allow create, update, delete: if false;
  }
}
```

---

## 3. Backend Changes

### 3.1 `multichurch_store.py` — `FirestoreMultiChurchStore`

#### 3.1.1 New method: `append_translation_segment()`

```python
def append_translation_segment(
    self,
    org_id: str,
    room_id: str,
    *,
    seq: int,
    korean_text: str,
    english_text: str,
    mode: str,           # "live" | "pre" | "scripture"
    match_score: float | None = None,
) -> None:
    """
    Write a single final segment to rooms/{roomId}/segments/{seq}.
    Called fire-and-forget from main.py — must never raise to caller.
    """
```

- Path: `self._room_ref(org_id, room_id).collection("segments").document(str(seq))`
- Uses `gcf_firestore.SERVER_TIMESTAMP` is NOT used here — timestamp is recorded at translation time as an ISO string to avoid server-side latency
- Document fields: `seq`, `timestamp`, `koreanText`, `englishText`, `mode`, and optionally `matchScore`
- **No-op** in `InMemoryMultiChurchStore` (just returns immediately — development mode does not persist)

#### 3.1.2 New method: `export_room_segments()`

```python
def export_room_segments(
    self,
    org_id: str,
    room_id: str,
    *,
    requested_by_uid: str,
) -> list[dict]:
    """
    Return all segments for a room, ordered by seq.
    Raises PermissionError if user is not owner/admin/host.
    Raises ValueError("room_not_found") if room does not exist.
    """
```

- Auth: check member role via `self._member_role(org_id, requested_by_uid)` — require `owner | admin | host`
- Verify room document exists
- Query `segments` subcollection, `order_by("seq")`, return list of dicts
- Returns `[]` if no segments (valid: room started but no final translations yet)
- **InMemoryMultiChurchStore**: raise `NotImplementedError` (caller handles with 501)

#### 3.1.3 Modify `list_services()` — `FirestoreMultiChurchStore` (line 2696)

Add `lastRoomId` to each service row:

```python
rows.append({
    "serviceKey": service_key,
    "title": service.get("title") or service_key,
    "timezone": service.get("timezone") or "UTC",
    "activeRoomId": active_room_id,
    "lastRoomId": service.get("lastRoomId"),   # ← ADD THIS
    "roomStatus": room_status,
    "defaultLanguagePair": service.get("defaultLanguagePair") or {"source": "ko", "target": "en"},
})
```

Also apply the same change to `InMemoryMultiChurchStore.list_services()` (line 791).

---

### 3.2 `main.py` — Deepgram STT WebSocket handler

**Location**: Inside the `send_translation()` nested function (line ~1360), in the `not partial` branch, after the broadcast calls (`manager.broadcast_room(...)` at line ~1524).

Add a fire-and-forget async task after broadcast succeeds:

```python
# After broadcast block (line ~1536), inside `not partial` branch:
if not partial and org_id and room_id:
    _seg_mode = live_mode          # "live", "pre", or "scripture"
    _seg_score = meta_payload.get("match_score") if _seg_mode == "pre" else None
    _seg_ts = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _safe_append_segment(
            org_id, room_id, seq,
            clean_src, translated, _seg_mode, _seg_score, _seg_ts,
        ),
    )
```

Where `_safe_append_segment` is a module-level helper:

```python
def _safe_append_segment(
    org_id: str,
    room_id: str,
    seq: int,
    korean_text: str,
    english_text: str,
    mode: str,
    match_score: float | None,
    timestamp: str,
) -> None:
    try:
        multichurch_store.append_translation_segment(
            org_id, room_id,
            seq=seq,
            korean_text=korean_text,
            english_text=english_text,
            mode=mode,
            match_score=match_score,
            timestamp=timestamp,
        )
    except Exception as exc:
        print(f"[SEG] Failed to save segment org={org_id} room={room_id} seq={seq}: {exc}")
```

**Note**: The "producer" WebSocket path (line ~940-976) uses a different structure. Apply the same fire-and-forget pattern there too, after the `manager.broadcast_room()` call at line ~962, for `not is_partial` segments.

---

### 3.3 `routes/multichurch.py` — New export endpoint

```python
import csv
import io
from fastapi.responses import StreamingResponse

@router.get("/org/{org_id}/room/{room_id}/segments/export")
def export_room_segments(
    *,
    org_id: str = Path(pattern=validators.ORG_ID),
    room_id: str = Path(pattern=validators.ORG_ID),
    current_user: AuthenticatedUser = Depends(get_current_user_required),
):
    try:
        segments = multichurch_store.export_room_segments(
            org_id,
            room_id,
            requested_by_uid=current_user.uid,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="host_auth_failed")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="not_supported_in_dev_mode")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["seq", "timestamp", "mode", "korean_text", "english_text"])
    for seg in segments:
        writer.writerow([
            seg.get("seq", ""),
            seg.get("timestamp", ""),
            seg.get("mode", ""),
            seg.get("koreanText", ""),
            seg.get("englishText", ""),
        ])

    filename = f"{org_id}_{room_id}_translation.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

---

## 4. Frontend Changes

### 4.1 `frontend/pages/host/c/[churchSlug].tsx`

#### 4.1.1 Update `ServiceRow` type

```typescript
type ServiceRow = {
  serviceKey: string;
  title: string;
  timezone?: string;
  activeRoomId?: string | null;
  lastRoomId?: string | null;    // ← ADD
  roomStatus?: string;
  defaultLanguagePair?: { source?: string; target?: string };
};
```

#### 4.1.2 Add `downloadTranslationLog()` helper function

```typescript
async function downloadTranslationLog(
  orgId: string,
  roomId: string,
  getToken: () => Promise<string>,
): Promise<void> {
  const token = await getToken();
  const res = await fetch(
    `${API_URL}/org/${orgId}/room/${roomId}/segments/export`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!res.ok) throw new Error(`Export failed: ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `translation_${roomId}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
```

#### 4.1.3 Add download button in service card

In the JSX where service cards are rendered, after the "Start Service" / "End Service" button, add:

```tsx
{svc.lastRoomId && svc.roomStatus !== "live" && (
  <button
    onClick={async () => {
      try {
        setDownloadingRoom(svc.lastRoomId!);
        const token = await user!.getIdToken();
        await downloadTranslationLog(orgId, svc.lastRoomId!, async () => token);
      } catch {
        // show error inline or via setMessage
      } finally {
        setDownloadingRoom(null);
      }
    }}
    disabled={downloadingRoom === svc.lastRoomId}
    style={{ /* secondary button styles consistent with existing UI */ }}
  >
    {downloadingRoom === svc.lastRoomId ? "Downloading…" : "Download Translation Log"}
  </button>
)}
```

Add `downloadingRoom` state: `const [downloadingRoom, setDownloadingRoom] = useState<string | null>(null);`

---

## 5. Implementation Order

| Step | File | Change |
|------|------|--------|
| 1 | `backend/firestore/firestore.rules` | Add `segments/{segId}` read rule under `rooms/{roomId}` |
| 2 | `backend/app/services/multichurch_store.py` | Add `append_translation_segment()` to both stores (no-op in InMemory) |
| 3 | `backend/app/services/multichurch_store.py` | Add `export_room_segments()` to `FirestoreMultiChurchStore`; add `NotImplementedError` stub in `InMemoryMultiChurchStore` |
| 4 | `backend/app/services/multichurch_store.py` | Add `lastRoomId` to `list_services()` in both stores |
| 5 | `backend/app/main.py` | Add `_safe_append_segment()` helper; wire fire-and-forget calls in both `send_translation()` and producer commit path |
| 6 | `backend/app/routes/multichurch.py` | Add `GET /org/{org_id}/room/{room_id}/segments/export` route |
| 7 | `frontend/pages/host/c/[churchSlug].tsx` | Update `ServiceRow` type, add `downloadTranslationLog()`, add download button in service card |

---

## 6. CSV Output Format

```
seq,timestamp,mode,korean_text,english_text
1,2026-03-16T11:04:23.412Z,live,하나님은 사랑이십니다,God is love
2,2026-03-16T11:04:45.001Z,pre,예수님께서 말씀하셨습니다,Jesus said
3,2026-03-16T11:05:12.876Z,scripture,요한복음 3장 16절,John 3:16
```

---

## 7. Error Handling

| Scenario | Backend response | Frontend behavior |
|----------|-----------------|-------------------|
| User is viewer (not host/admin/owner) | 403 `host_auth_failed` | Show error message |
| Room not found | 404 `room_not_found` | Show error message |
| Dev mode (InMemory store) | 501 `not_supported_in_dev_mode` | Show error message |
| No segments in room | 200, CSV with header row only | Browser downloads empty CSV |
| Network error | — | Show error message, re-enable button |

---

## 8. Acceptance Criteria

- [ ] Each final Korean→English segment is written to `rooms/{roomId}/segments/{seq}` in Firestore during live session
- [ ] Partial segments are never written to Firestore
- [ ] `mode` correctly reflects `"live"`, `"pre"`, or `"scripture"`
- [ ] `matchScore` is present only for `"pre"` mode segments
- [ ] `append_translation_segment()` failure does not interrupt the translation broadcast
- [ ] `GET /org/{orgId}/room/{roomId}/segments/export` returns a valid CSV
- [ ] CSV rows are ordered by `seq`
- [ ] Download is blocked for `viewer` role (403)
- [ ] `list_services` response includes `lastRoomId` for each service
- [ ] Download button appears only when `lastRoomId` exists and room is not live
- [ ] Button shows loading state during download
- [ ] Empty sessions return CSV with header row only
