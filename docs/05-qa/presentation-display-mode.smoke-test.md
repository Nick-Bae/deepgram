# presentation-display-mode — Manual Smoke Test

> **Purpose**: Verify the host upload + display + reconnect flows that aren't covered by the L1 backend tests in `backend/tests/test_slides_routes.py`. Run before merging or before each production deploy.
>
> **Scope**: Module 2 (display) + Module 3 (host UI) + WS broadcast end-to-end.
>
> **Prerequisite environment**: Pillow + python-multipart installed; Firebase Storage bucket reachable; Firestore rules + Storage rules deployed.

---

## Prep

```bash
# Backend deps (one-time, after pulling this branch)
cd backend
.venv/bin/python -m pip install -r requirements.txt   # adds pillow + python-multipart

# Verify env vars
echo $GOOGLE_CLOUD_PROJECT $FIREBASE_STORAGE_BUCKET   # both should be set
# (FIREBASE_STORAGE_BUCKET defaults to {project}.appspot.com if unset)

# Optional caps for testing the limits
export MAX_SLIDE_IMAGE_BYTES=10485760     # default 10MB
export MAX_SLIDES_PER_SERVICE=50          # default 50
export SLIDE_SIGNED_URL_TTL_SEC=86400     # default 24h

# Frontend (no extra deps)
cd ../frontend
npm run dev-all
```

Then deploy the new rules (one-time in each environment):

```bash
firebase deploy --only firestore:rules
# Storage rules path is backend/firestore/storage.rules.
# Add to firebase.json under storage.rules if not already present:
#   "storage": { "rules": "backend/firestore/storage.rules" }
firebase deploy --only storage:rules
```

---

## Test 1 — Host upload + display split layout (golden path)

**Roles**: One host browser tab, one display browser tab (incognito ok).

1. Log in as a user with `host`/`admin`/`owner` role on a real church.
2. Open `/host/c/{your-slug}/slides` in tab 1.
3. **Expect**: Slides tab pill is visible between "Live Broadcast" and "Church Settings". Empty-state message says "No slides uploaded yet."
4. Drag 3-5 PNG/JPEG files (real PowerPoint exports, mixed 16:9 and 4:3 aspect ratios) into the dotted upload zone.
5. **Expect**: Upload progress shown briefly, then thumbnails appear in order. "Uploaded N slides." green message flashes.
6. **Expect**: First thumbnail is highlighted in green with "LIVE" label only after you click it (it's not auto-promoted on upload).
7. In tab 2, navigate to `/display`.
8. **Expect**: Subtitle-only mode (no slide visible) since no `currentSlideIndex` is set yet.
9. In tab 1, click thumbnail #2.
10. **Expect within 500ms in tab 2**: Display switches to presentation mode automatically. Slide #2 fills the top ~70% of viewport. Subtitle area at bottom still empty (no live audio yet).
11. In tab 1, press → key.
12. **Expect within 500ms in tab 2**: Display advances to slide #3. Cross-fade animation smooth.
13. Press ← key in tab 1.
14. **Expect**: Display goes back to slide #2.

**PASS criteria**: All 14 steps work without console errors. Slide-change roundtrip < 500ms each.

---

## Test 2 — Aspect ratio handling

1. Upload a tall 9:16 image (e.g. 1080×1920).
2. Upload a wide 21:9 image (e.g. 2560×1080).
3. Upload a square image (1080×1080).
4. Click each in turn.
5. **Expect**: Each renders centered with letterboxing on the axis that doesn't fit (`object-fit: contain`). No squashing, no cropping.

---

## Test 3 — Cap enforcement

1. Upload slides until you hit `MAX_SLIDES_PER_SERVICE` (default 50). Use small files for speed.
2. Try to upload one more.
3. **Expect**: Red error message "Service slide limit reached (50)." Upload button shows "limit reached" hint.

4. Try to upload a >10MB file.
5. **Expect**: Red error "Image must be under 10MB."

6. Try to upload a `.pdf` (or rename a PDF to `.png` — magic bytes catch it).
7. **Expect**: Red error "Only PNG or JPEG images are accepted."

---

## Test 4 — Reorder

1. Have ≥3 slides uploaded.
2. Drag thumbnail #1 onto thumbnail #3.
3. **Expect**: Order updates immediately in the strip (optimistic). Drag indicator showed during the drag.
4. Reload the page.
5. **Expect**: New order persists.

---

## Test 5 — Delete

1. Click × on any thumbnail.
2. Confirm dialog → click OK.
3. **Expect**: Thumbnail removed from strip. Order indices renumber.
4. Open the Firebase Console → Storage → confirm the slide image file is also gone from `orgs/{orgId}/services/{serviceKey}/slides/`.

---

## Test 6 — Reconnect mid-service

1. Have host tab + display tab open with current slide set to #2.
2. In the display tab, open DevTools → Network → set throttling to "Offline" for 5 seconds, then back to "Online".
3. **Expect**: Display reconnects automatically (existing WS reconnect logic). Within ~2 seconds, slide_state arrives and display lands on slide #2 (or whatever is current).
4. Try changing slides on the host while the display is offline — when it reconnects, it should land on the *latest* slide, not whatever was current before disconnect.

---

## Test 7 — Presentation toggle (`p` key)

1. Display is in presentation mode with a slide visible.
2. Press `p`.
3. **Expect**: Display switches to subtitle-only mode. Slide hidden.
4. Press `p` again.
5. **Expect**: Returns to presentation mode with the current slide.
6. Press `f`.
7. **Expect**: Toggles to/from full-screen subtitle mode (existing behavior, unchanged).

---

## Test 8 — Auth boundary

1. Log out and try to visit `/host/c/{slug}/slides` directly.
2. **Expect**: Redirected to login (existing host page guard).
3. Log in as a `viewer` role user (membership but not host).
4. Visit `/host/c/{slug}/slides`.
5. **Expect**: Slides tab loads but uploads/deletes/advance return 403 with "FORBIDDEN_ROLE" error message inline.
6. The display page (`/display`) remains public — no auth required.

---

## Test 9 — Late-joiner display

1. Host has a deck loaded and is on slide #5.
2. Open `/display` in a fresh incognito tab (simulates a viewer joining mid-service).
3. **Expect**: Display lands on slide #5 immediately via the `slide_state` message emitted on WS connect (Module 1 backend).

---

## Test 10 — No-deck regression

1. Find a service with no slides uploaded.
2. Open `/display`.
3. **Expect**: Subtitle-only mode (or full-screen via `f`) — exactly as before this feature was added.
4. The "Slides" toggle button at top-right is **not visible**.
5. The `f` key still toggles between subtitle and full-screen modes.

---

## Pass / Fail tracking template

```
Date: ____________
Tester: ____________
Branch: ____________

Test 1  Golden path                [ ] PASS  [ ] FAIL  Notes:
Test 2  Aspect ratios              [ ] PASS  [ ] FAIL  Notes:
Test 3  Cap enforcement            [ ] PASS  [ ] FAIL  Notes:
Test 4  Reorder                    [ ] PASS  [ ] FAIL  Notes:
Test 5  Delete                     [ ] PASS  [ ] FAIL  Notes:
Test 6  Reconnect mid-service      [ ] PASS  [ ] FAIL  Notes:
Test 7  Presentation toggle (p)    [ ] PASS  [ ] FAIL  Notes:
Test 8  Auth boundary              [ ] PASS  [ ] FAIL  Notes:
Test 9  Late-joiner display        [ ] PASS  [ ] FAIL  Notes:
Test 10 No-deck regression         [ ] PASS  [ ] FAIL  Notes:
```

---

## Known limitations (out of scope for this feature)

- No automated Playwright/E2E tests — the repo has no Playwright infrastructure today. Adding it is a separate initiative.
- No PPT/PPTX auto-conversion — host must export to PNG/JPEG manually.
- No caption editor UI yet (the API supports it; the UI affordance is a future iteration).
- No viewer-count surfacing on the Slides tab (broadcast tab still owns viewer state).
