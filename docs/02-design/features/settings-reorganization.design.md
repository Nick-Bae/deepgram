# Design: Church Settings Reorganization

**Feature**: `settings-reorganization`
**Phase**: Design
**Date**: 2026-04-10
**Reference Plan**: `docs/01-plan/features/settings-reorganization.plan.md`

---

## 1. Overview

Replace the flat 2-column grid in the Settings tab with a 3-section sub-nav layout:
**General → Services → Translation**

Single file change: `frontend/pages/host/c/[churchSlug].tsx`

---

## 2. New State

Add one state variable near the other tab/section states (around line ~320):

```tsx
const [settingsSection, setSettingsSection] = useState<"general" | "services" | "translation">("general");
```

Add a reset effect so switching away from settings resets it to `"general"`:

```tsx
useEffect(() => {
  if (activeTab !== "settings") setSettingsSection("general");
}, [activeTab]);
```

---

## 3. Sub-nav Bar Component (inline JSX)

Rendered at the top of the `settingsShellStyle` div, before the section content.
Reuses the same pill style as the main tab nav for visual consistency.

```tsx
{/* Settings sub-nav */}
<div style={{ display: "flex", gap: 6, flexWrap: "wrap" as const }}>
  {(["general", "services", "translation"] as const)
    .filter((s) => s !== "translation" || canManageInvites)
    .map((s) => {
      const labels = { general: "General", services: "Services", translation: "Translation" };
      const isActive = settingsSection === s;
      return (
        <button
          key={s}
          type="button"
          onClick={() => setSettingsSection(s)}
          style={{
            border: isActive ? "none" : `1px solid ${DC.border}`,
            background: isActive ? DC.navy : DC.white,
            color: isActive ? "#ffffff" : DC.charcoal,
            fontWeight: isActive ? 700 : 500,
            fontSize: 13,
            padding: "8px 18px",
            borderRadius: 999,
            cursor: "pointer",
            letterSpacing: "-0.01em",
          }}
        >
          {labels[s]}
        </button>
      );
    })}
</div>
```

**Note**: Translation pill is hidden when `!canManageInvites` (host/viewer roles).

---

## 4. Section Content Layout

Replace the current `settingsGridStyle` div + full-width `Service Schedule` section with
a conditional render based on `settingsSection`:

```
settingsSection === "general"     → General section content
settingsSection === "services"    → Services section content
settingsSection === "translation" → Translation section content (canManageInvites gate)
```

Each section wraps its cards in a `settingsGridStyle` div (same 2-column auto-fit grid as today).

---

## 5. Section Definitions

### 5A. General Section

Cards (2-column grid):
1. **Your Profile** card — unchanged from current (display name input + save)
2. **Church Name & URL** card — unchanged from current (church name, slug readonly, public path)

Below the grid, a small billing shortcut row:
```tsx
<div style={{ display: "flex", alignItems: "center", gap: 8 }}>
  <span style={{ fontSize: 13, color: DC.mid }}>Need to change your plan or review subscription?</span>
  <button
    type="button"
    onClick={() => navigateToTab("billing")}
    style={{ ...settingsButtonNeutralStyle, padding: "7px 14px", fontSize: 12 }}
  >
    Go to Billing →
  </button>
</div>
```

**Removed from General**: Billing Snapshot card (the plan pills + subscription period block).

---

### 5B. Services Section

Full-width (single column), no change to internal logic:
- The **Service Schedule** section card — unchanged content (add service inputs, service list rows with Download Log + Delete buttons)

**Removed from Services**: The two buttons "Open Prompt Settings" and "Open Sermon Prep" that were in the section header `div`'s flex row. The header becomes:

```tsx
<div style={{ display: "grid", gap: 8 }}>
  <p style={settingsSectionLabelStyle}>Operations</p>
  <h3 style={settingsTitleStyle}>Service Schedule</h3>
  <p style={settingsBodyTextStyle}>
    Add service times for this church. Added services appear in the dropdown for all members.
  </p>
</div>
```

(The outer `display: flex, justifyContent: space-between` wrapper is removed since the buttons are gone.)

---

### 5C. Translation Section

Visible only when `canManageInvites` (admin/owner). Cards in 2-column grid:

1. **STT Keywords** card — unchanged from current (`SttKeytermsEditorLazy` component)

2. **Prompt Settings** card — new card replacing the old button:
```tsx
<section style={settingsCardStyle}>
  <p style={settingsSectionLabelStyle}>Translation</p>
  <h3 style={settingsTitleStyle}>Prompt Settings</h3>
  <p style={settingsBodyTextStyle}>
    Customize the translation instructions sent to the AI model — tone, formality, theological terms, and more.
  </p>
  <div>
    <button
      onClick={() => {
        const qs = new URLSearchParams();
        qs.set("orgId", resolvedOrgId);
        if (slug) qs.set("churchSlug", slug);
        void router.push(`/admin/prompt?${qs.toString()}`);
      }}
      style={settingsButtonNeutralStyle}
    >
      Open Prompt Settings
    </button>
  </div>
</section>
```

3. **Sermon Prep** card — new card replacing the old button:
```tsx
<section style={settingsCardStyle}>
  <p style={settingsSectionLabelStyle}>Translation</p>
  <h3 style={settingsTitleStyle}>Sermon Prep</h3>
  <p style={settingsBodyTextStyle}>
    Pre-load sermon scripts and vocabulary before the service to improve real-time translation accuracy.
  </p>
  <div>
    <button
      onClick={() => {
        const qs = new URLSearchParams();
        qs.set("orgId", resolvedOrgId);
        if (slug) qs.set("churchSlug", slug);
        const returnTo = (router.asPath || "").trim();
        if (returnTo.startsWith("/") && !returnTo.startsWith("//")) qs.set("returnTo", returnTo);
        void router.push(`/admin/sermon-prep?${qs.toString()}`);
      }}
      style={settingsButtonNeutralStyle}
    >
      Open Sermon Prep
    </button>
  </div>
</section>
```

---

## 6. Structural Diff Summary

| Before | After |
|---|---|
| `settingsGridStyle` (2-col) containing 4 cards | Sub-nav + conditional single section |
| Full-width Service Schedule below grid | Service Schedule in Services section |
| Billing Snapshot card in grid | Removed; replaced by small "Go to Billing" button in General |
| Prompt + Sermon Prep buttons in Service header | Moved to Translation section as proper cards |
| STT Keywords in grid (gated) | Moved to Translation section |

---

## 7. Implementation Order

1. Add `settingsSection` state + reset effect
2. Replace the settings block JSX (`lines ~2542–2890`) with:
   a. `settingsShellStyle` div
   b. Sub-nav bar
   c. `{settingsSection === "general" && <GeneralContent />}`
   d. `{settingsSection === "services" && <ServicesContent />}`
   e. `{settingsSection === "translation" && canManageInvites && <TranslationContent />}`
3. Build General: move Profile + Church Identity cards + add Billing shortcut row
4. Build Services: move Service Schedule (strip the Prompt/Sermon buttons from header)
5. Build Translation: move STT Keywords + add Prompt Settings card + Sermon Prep card
6. Delete Billing Snapshot card
7. Run `npm run lint`

---

## 8. Acceptance Criteria

- [ ] Sub-nav shows General / Services / Translation pills
- [ ] Translation pill hidden for host/viewer roles
- [ ] General: Your Profile + Church Name & URL + "Go to Billing →" button
- [ ] Services: Service Schedule only, no Prompt/Sermon buttons in header
- [ ] Translation: STT Keywords + Prompt Settings card + Sermon Prep card
- [ ] Billing Snapshot card not present anywhere in Settings
- [ ] All existing save handlers (saveAccountProfile, saveChurchProfile, addService, removeService, downloadTranslationLog) unchanged
- [ ] `npm run lint` passes with no new errors
