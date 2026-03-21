## Executive Summary

| Item | Detail |
|------|--------|
| Feature | translation-log-download |
| Date | 2026-03-21 |
| Phase | Plan |

### Value Delivered (4 Perspectives)

| Perspective | Content |
|-------------|---------|
| **Problem** | After a live worship service, there is no way for the host or admin to review what was translated — the only log is a pooled, backend-local file inaccessible to users. |
| **Solution** | Persist each final Korean→English segment to Firestore during the live session, then expose a CSV download endpoint accessible from the host console after the service ends. |
| **Function UX Effect** | Host/admin can click "Download Translation Log" on any ended room and get a CSV with all Korean/English pairs, timestamps, and source mode — ready to open in Excel/Sheets for review. |
| **Core Value** | Closes the quality feedback loop: reviewers can identify translation errors, awkward phrasing, or glossary gaps from real services and use that to improve the system over time. |

---

# Plan: translation-log-download

## 1. Background & Motivation

The real-time translation pipeline converts Korean speech to English text and broadcasts it to listeners via WebSocket. Currently:

- **No per-session log is persisted**: segments are broadcast and discarded
- **`translation_examples.jsonl`** exists on the backend server but is pooled across all orgs, deduplicated automatically, and not accessible to org-level users
- **`finalTranscript`** in Firestore only stores a plain Korean string, no English pairs

**User need**: After a Sunday service, the admin wants to download a structured Korean/English log, review it offline (Excel/Sheets), identify translation errors or awkward phrasing, and use that insight to improve prompts, glossary, or sermon prep scripts.

## 2. Goals

- Persist each final translated segment (Korean + English) to Firestore during live session
- Expose a CSV download endpoint for ended rooms, accessible by org owners/admins/hosts
- Show a "Download Translation Log" button in the host console for ended rooms
- Zero impact on real-time latency (async, non-blocking write)

## 3. Non-Goals

- In-app correction editor (deferred — download-only for now)
- Feeding corrections back into few-shot examples automatically (deferred)
- Real-time log streaming to the UI during a live session
- Storing partial/interim segments (final segments only)

## 4. User Stories

| # | As a... | I want to... | So that... |
|---|---------|--------------|------------|
| US-1 | Host/Admin | Download a CSV of the translation log after service ends | I can review Korean↔English pairs offline |
| US-2 | Admin | See which segments came from pre-loaded sermon script vs live AI | I can evaluate script coverage |
| US-3 | Admin | Have timestamped segments in order | I can correlate with recording or sermon notes |

## 5. Scope

### 5.1 Backend

**A. Segment persistence (during live session)**
- In `main.py` WebSocket handler (`/ws/stt_deepgram`), after a **final** segment is translated and broadcast, write a segment document to Firestore
- Subcollection path: `organizations/{orgId}/rooms/{roomId}/segments/{seq}`
- Fields: `seq`, `timestamp`, `koreanText`, `englishText`, `mode` (`"live"` or `"pre"`), `matchScore` (if pre-script match)
- Write must be **async and non-blocking** — failure should log a warning, not interrupt the stream

**B. CSV export endpoint**
- New route: `GET /org/{orgId}/room/{roomId}/segments/export`
- Auth: org member with role `owner | admin | host`
- Queries `segments` subcollection, ordered by `seq`
- Returns `Content-Type: text/csv` with `Content-Disposition: attachment; filename="{orgId}_{roomId}_translation.csv"`
- CSV columns: `seq`, `timestamp`, `mode`, `korean_text`, `english_text`

**C. Firestore security rules**
- Allow org members (owner/admin/host/viewer) to read `rooms/{roomId}/segments/{segId}`

### 5.2 Frontend

**A. Download button in host console**
- Location: host console service page (`/host/c/[churchSlug]/[section]`) — shown when `room.status === "ended"`
- Also accessible from admin dashboard room history if applicable
- Button: "Download Translation Log (CSV)"
- On click: fetch from export endpoint with auth token → trigger browser file download
- Show loading state during fetch; show error if endpoint fails

### 5.3 Firestore Data Model

```
organizations/{orgId}/
  rooms/{roomId}/
    segments/                    ← new subcollection
      {seq}/
        seq: number              ← segment order (1, 2, 3...)
        timestamp: string        ← ISO 8601 UTC
        koreanText: string       ← raw STT output
        englishText: string      ← translated output
        mode: "live" | "pre"     ← live AI translation or pre-script match
        matchScore: number?      ← only when mode = "pre"
```

## 6. Technical Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Storage | Firestore subcollection under room | Fits existing data model; org/room scoping natural |
| Segment filter | Final segments only (no partials) | Partials are noisy and revision-heavy; final = committed translation |
| Write strategy | Fire-and-forget async task | Translation latency must not increase |
| Export format | CSV | Directly opens in Excel/Sheets without conversion |
| Auth for export | Org-level role check (owner/admin/host) | Same pattern as other room endpoints |
| Firestore cost | ~$0.00006/service (100 writes × $0.06/100K) | Negligible |

## 7. Implementation Order

1. **Firestore rules** — add read permission for segments subcollection
2. **Backend: segment write** — add async Firestore write in `main.py` after final broadcast
3. **Backend: export route** — add `GET /org/{orgId}/room/{roomId}/segments/export` in `multichurch.py`
4. **Frontend: download button** — add to host console service page for ended rooms

## 8. Acceptance Criteria

- [ ] Final Korean→English segments are saved to Firestore during a live session
- [ ] No measurable latency increase on the STT→translation pipeline
- [ ] CSV download returns segments in order with correct columns
- [ ] Download is accessible to org owner/admin/host; blocked for viewer and unauthenticated
- [ ] Segment `mode` correctly reflects `"live"` vs `"pre"` (sermon script match)
- [ ] Download button appears only when room status is `ended`
- [ ] Empty rooms (0 segments) return a valid CSV with header row only

## 9. Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Firestore write failure during live session | Low | Fire-and-forget with error logging; does not affect translation stream |
| Large rooms (3h service) producing many segments | Low | ~180 segments max; well within Firestore limits |
| Missing segments if server restarts mid-session | Medium | Already an existing risk for room state; out of scope for v1 |
