# Analysis: Security Hardening

> Reference: [Design](../02-design/features/security-hardening.design.md)

## Match Rate: 100%

---

## Acceptance Criteria Verification

| ID | Criterion | Status | Evidence |
|---|---|---|---|
| SH-01 | `GET /docs` returns 404 in production | PASS | `main.py:249` `docs_url=None if _IS_PRODUCTION else "/docs"` |
| SH-01 | `GET /redoc` returns 404 in production | PASS | `main.py:250` `redoc_url=None if _IS_PRODUCTION else "/redoc"` |
| SH-01 | `GET /openapi.json` returns 404 in production | PASS | `main.py:251` `openapi_url=None if _IS_PRODUCTION else "/openapi.json"` |
| SH-01 | All three endpoints return 200 in local dev | PASS | Same gate: `_IS_PRODUCTION = False` locally (no `K_SERVICE`) |
| SH-02 | `main.py.tmp.2869.1774109189155` does not exist | PASS | `ls backend/app/main.py.tmp*` → file not found |
| SH-03 | `*.tmp.*` pattern in `backend/.gitignore` | PASS | Both `*.tmp` and `*.tmp.*` added to `backend/.gitignore` |
| SH-04 | Cross-org audit table complete — all routes PASS | PASS | 12 routes audited, 11 PASS + 1 intentional public (listener WS) |
| SH-05 | `GUARDRAIL_DEBUG` env var controls print; default = no output | PASS | `openai_translator.py:10` flag + `:245` `if _GUARDRAIL_DEBUG:` gate |
| SH-06 | No `NEXT_PUBLIC_` variables contain secrets | PASS | 16 vars enumerated; no OpenAI/Stripe/Deepgram/webhook keys found |
| — | ESLint passes | PASS | `npm run lint` — 0 warnings, 0 errors |

---

## Gap Analysis

### No gaps found.

All 6 design items (SH-01 through SH-06) are fully implemented or documented as designed.

---

## Notes

### SH-05 verification detail

The grep `print.*ko.*raw_en` returns line 246 in `openai_translator.py`, which is:

```python
    if _GUARDRAIL_DEBUG:                                          # line 245
        print(debug_tag, "ko:", ko, "raw_en:", en, ...)          # line 246 — indented
```

The print is correctly inside the `if _GUARDRAIL_DEBUG:` block. No unconditional transcript print remains.

### SH-06 verification detail

One NEXT_PUBLIC_ variable not anticipated in the design audit: `NEXT_PUBLIC_TURNSTILE_SITE_KEY` (Cloudflare Turnstile). This is the **public site key** — by Cloudflare's design it is embedded in the browser page to render the CAPTCHA widget. The corresponding **secret key** is at `CLOUDFLARE_TURNSTILE_SECRET` (no `NEXT_PUBLIC_` prefix), used only in `frontend/pages/api/contact.ts` (a server-side API route). No issue.

### Accepted risks confirmed in place

| Risk | Documentation |
|---|---|
| Firestore `rooms/{roomId}` public read | Documented in design §9 as explicit accepted risk |
| `/ws/translate` unauthenticated | Documented as intentional — public church broadcasts |
| Firebase App Check not implemented | Deferred to future sprint |

---

## Conclusion

Match rate: **100%** (6/6 items complete, 0 gaps).

All code changes are live. No iteration required.

Next step: `/pdca report security-hardening`
