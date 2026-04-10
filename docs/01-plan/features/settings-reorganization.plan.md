## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Church Settings mixes unrelated concerns (user profile, church identity, translation config, billing snapshot, service management) in a flat grid with no clear grouping, making it hard to navigate as features grow |
| **Solution** | Introduce a sub-navigation bar within the Settings tab that groups related settings into three focused sections: General, Services, and Translation |
| **UX Effect** | Users immediately see only the settings relevant to their task; cognitive load drops; admin-only controls are still visible but clearly scoped |
| **Core Value** | A well-organized settings page reduces support burden and makes the product feel production-ready as church count and features grow |

---

# Plan: Church Settings Reorganization

**Feature**: `settings-reorganization`
**Phase**: Plan
**Date**: 2026-04-10

---

## 1. Problem Statement

The current Church Settings tab has 5 heterogeneous sections rendered in a flat layout:

| Section | Category | Notes |
|---|---|---|
| Your Profile | User account | Belongs to the user, not the church |
| Church Name & URL | Church identity | Core church config |
| STT Keywords | Translation quality | Admin/owner only |
| Billing Snapshot | Billing | Redundant — Billing tab exists |
| Service Schedule | Operations | Contains unrelated Prompt/Sermon Prep shortcuts |

Pain points:
- **No grouping**: Profile lives next to billing lives next to services — no logical flow
- **Billing duplication**: Billing Snapshot in Settings repeats what's already on the Billing tab
- **Buried shortcuts**: "Open Prompt Settings" and "Open Sermon Prep" buttons are inside the Service Schedule section header, implying they're service-level operations when they're translation-level tools
- **Flat grid breaks down**: As more settings are added, the 2-column grid becomes harder to scan

---

## 2. Proposed Structure

Add a **settings sub-nav** (pill tabs) inside the Settings tab with three sections:

### Section A — General
- Your Profile (display name)
- Church Name & URL (name, slug, public path)

### Section B — Services
- Service Schedule (add, list, delete services, download log)
- *(Remove the Prompt Settings and Sermon Prep buttons from here)*

### Section C — Translation
- STT Keywords (Deepgram vocabulary)
- Links/cards for Prompt Settings and Sermon Prep (these are translation quality tools, not service tools)

### Remove
- **Billing Snapshot** card: Removed from Settings entirely. Users who need billing info already have the Billing tab. A small "Go to Billing →" link in the General section footer is sufficient.

---

## 3. Sub-nav Design

```
[Settings tab]
  ↓
  Sub-nav pill bar: [General] [Services] [Translation]
  ↓
  Section content area (replaces current flat grid)
```

- Default section on tab open: **General**
- Sub-nav state is not persisted (resets to General on tab switch)
- Sub-nav is only visible when `canManageServices` is true (same gate as today)
- Translation section only visible when `canManageInvites` is true (same gate as STT Keywords today — admin/owner)

---

## 4. Scope

### In Scope
- Add sub-nav pill bar component (inline, no new file)
- Move existing sections into the three buckets without changing their internal logic
- Move Prompt Settings + Sermon Prep buttons from Service Schedule header → Translation section as styled link cards
- Remove Billing Snapshot card from Settings
- Add a small "Billing →" shortcut link at the bottom of the General section

### Out of Scope
- Changing any backend API
- Redesigning individual section internals (inputs, save logic unchanged)
- Responsive/mobile layout changes
- Permission model changes

---

## 5. State Required

Add one new state variable:
```tsx
const [settingsSection, setSettingsSection] = useState<"general" | "services" | "translation">("general");
```

Reset to `"general"` when `activeTab` changes away from `"settings"`.

---

## 6. Files Changed

| File | Change |
|---|---|
| `frontend/pages/host/c/[churchSlug].tsx` | Only file changed — add sub-nav state + reorganize JSX within the settings block (lines ~2542–2890) |

---

## 7. Acceptance Criteria

- [ ] Settings tab shows a 3-pill sub-nav (General / Services / Translation)
- [ ] General: Profile + Church Name/URL + small Billing link
- [ ] Services: Service schedule only (no Prompt/Sermon Prep buttons)
- [ ] Translation: STT Keywords + Prompt Settings card + Sermon Prep card
- [ ] Translation section hidden for `host` and `viewer` roles (only admin/owner)
- [ ] Billing Snapshot card removed from Settings
- [ ] No changes to save handlers, API calls, or billing tab
- [ ] `npm run lint` passes
