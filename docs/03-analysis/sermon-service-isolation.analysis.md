# sermon-service-isolation — Gap Analysis Report

> **Feature**: sermon-service-isolation
> **Design Doc**: `docs/02-design/features/sermon-service-isolation.design.md`
> **Analysis Date**: 2026-03-27
> **Analyst**: gap-detector agent

---

## Overall Result

| Category | Score | Status |
|----------|:-----:|:------:|
| Design Match | 100% | PASS |
| Architecture Compliance | 100% | PASS |
| Convention Compliance | 100% | PASS |
| **Overall Match Rate** | **100%** | **PASS** |

---

## Detailed Verification (14 / 14 items)

| # | Requirement | Pass | Location |
|---|-------------|:----:|----------|
| 1 | `script_store._org_key` priority: `room_id` > `org+svc+date` > `org` | ✅ | `script_store.py:119–139` |
| 2 | All 9 public methods accept `room_id` kwarg (`load`, `clear`, `stats`, `match`, `match_with_examples`, `get_keyword_glossary`, `get_vocab_set`, `save_sermon`, `get_sermon`) | ✅ | `script_store.py:141–431` |
| 3 | 6 new methods on both InMemoryMultiChurchStore + FirestoreMultiChurchStore (`save_sermon_draft`, `get_sermon_draft`, `publish_sermon`, `get_published_sermon`, `list_sermon_drafts`, `list_services_by_org_id`) | ✅ | `multichurch_store.py` — InMemory stubs + Firestore implementations |
| 4 | Firestore sermon path: `organizations/{orgId}/services/{serviceKey}/sermons/{serviceDate}` | ✅ | `multichurch_store.py` — `_sermon_ref()` helper |
| 5 | `publish_sermon` sets `status="published"`, updates `service.publishedSermonDate` | ✅ | `multichurch_store.py` — `publish_sermon()` |
| 6 | 6 new service-scoped endpoints in `routes/script.py` (draft, GET, PUT, publish, unpublish, list) | ✅ | `routes/script.py:472, 517, 532, 568, 612, 647` |
| 7 | `routes/multichurch.py`: `GET /org/{orgId}/services` exists, `require_org_role` imported, `script_store.clear(room_id=room_id)` on room end | ✅ | `routes/multichurch.py:105–114, 10, 269` |
| 8 | `_try_reload_sermon` accepts `room_id`, `service_key`, `service_date`; tries service-specific first, falls back to org-latest; loads into script_store with `room_id` key | ✅ | `main.py:590–649` |
| 9 | `/ws/stt/deepgram` uses `room_id` for all script_store calls (`stats`, `get_keyword_glossary`, `get_vocab_set`, `match_with_examples`) | ✅ | `main.py:1576–1592` |
| 10 | `/ws/translate` producer path uses `room_id` for script_store calls | ✅ | `main.py:1013–1028` |
| 11 | Firestore rules: `services/{serviceKey}/sermons/{serviceDate}` subcollection allows `isOrgMember` read, writes blocked | ✅ | `firestore.rules:35–38` |
| 12 | `SermonPrep.tsx`: `serviceKey` prop, `serviceDate`/`busyPublish`/`published` state, `onSaveDraft`/`onPublish` handlers, date picker when `serviceKey` set, Save Draft + Publish buttons | ✅ | `SermonPrep.tsx:17–19, 46–56, 236–280, 341–365, 436–468` |
| 13 | `sermon-prep.tsx`: `services` state, `selectedServiceKey` state, `fetchOrgServices` call, service selector UI rendered when `services.length > 0` | ✅ | `sermon-prep.tsx:28–29, 120–143, 206–222` |
| 14 | `backendAuth.ts`: `draftServiceSermon`, `saveServiceSermon`, `publishServiceSermon`, `fetchOrgServices` functions | ✅ | `backendAuth.ts:710–790` |

---

## Gaps Found

**None.** All 14 design requirements are fully implemented.

---

## Architecture Compliance Notes

- **Dependency direction**: Routes → Stores → Firestore (correct). Frontend → `backendAuth` API layer (correct). No cross-layer violations.
- **Room-id isolation**: Consistently applied in both WebSocket handlers (`/ws/stt/deepgram`, `/ws/translate`) and room lifecycle (`end_room` → `script_store.clear(room_id=room_id)`).
- **Firestore rules**: Client writes blocked (`allow create, update, delete: if false`); reads gated on `isOrgMember(orgId)` — matches server-write-only architecture from CLAUDE.md.
- **Pre-warm on publish**: `routes/script.py` loads pairs into script_store with `service_key + service_date + org_id` key immediately on publish, matching design Section 4.2 side effect #3 (`preWarmed: true`).
- **Legacy compatibility**: Old endpoints (`/org/{orgId}/sermon/draft`, `/org/{orgId}/sermon/finalize`) preserved unchanged. Fallback in `_try_reload_sermon` to `get_latest_sermon_pairs` ensures single-service orgs continue working.

---

## Test Scenario Coverage

| Scenario | Design § | Implemented |
|----------|----------|:-----------:|
| T1: Publish pre-warms `org::svc::date` key | §8.1 | ✅ |
| T2: Room start loads published sermon into `room::{roomId}` | §8.1 | ✅ |
| T3: Two Sunday services run independently | §8.1 | ✅ |
| T4: Publishing 11 AM doesn't affect 9 AM in-flight | §8.1 | ✅ |
| T5: Room end clears memory | §8.1 | ✅ |
| T6: No published sermon → falls back to org-latest | §8.1 | ✅ |
| T7: Server restart reloads from `room.sermonDate` | §8.1 | ✅ |
| T8: Legacy org with no service_key works unchanged | §8.1 | ✅ |

---

## Conclusion

**Match Rate: 100% (14/14)** — Implementation fully satisfies the design document.
Ready for `/pdca report sermon-service-isolation`.
