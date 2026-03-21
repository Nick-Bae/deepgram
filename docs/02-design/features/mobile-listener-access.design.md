# Design: Mobile Listener Access

## 1. Architecture Overview

Two files change:
1. `frontend/pages/host/c/[churchSlug].tsx` — host broadcast tab: add QR card + share buttons
2. `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` — listener page: mobile UX polish

No backend changes. No new pages.

---

## 2. Implementation Details

### 2.1 QR Code Generation

**Package**: `qrcode` (browser canvas/SVG, no Node dependency)

```ts
import QRCode from "qrcode";

// Generate a data URL for use in <img>
const dataUrl = await QRCode.toDataURL(url, {
  width: 200,
  margin: 1,
  color: { dark: "#0f172a", light: "#ffffff" },
});
```

Wrap in a `useEffect` that re-runs when `displayUrl` changes.

### 2.2 Host Dashboard — "Listener Access" Card

**Location**: replaces the existing `displayUrl` block (lines ~2221–2226 in `[churchSlug].tsx`)

**Layout**:
```
┌─────────────────────────────────────────┐
│  LISTENER ACCESS          [open ↗]      │
│                                         │
│   ┌──────────┐  yourdomain.com/c/       │
│   │  QR CODE │  church/s/sun-11am       │
│   │  200×200 │                          │
│   └──────────┘  [Copy URL] [Share via…] │
└─────────────────────────────────────────┘
```

**State additions** (in `HostChurchPage`):
```ts
const [qrDataUrl, setQrDataUrl] = useState("");
const [copyUrlBusy, setCopyUrlBusy] = useState(false);
const [copyUrlNotice, setCopyUrlNotice] = useState<string | null>(null);
const [shareUrlBusy, setShareUrlBusy] = useState(false);
```

**Effect**:
```ts
useEffect(() => {
  if (!displayUrl) { setQrDataUrl(""); return; }
  let cancelled = false;
  QRCode.toDataURL(displayUrl, { width: 200, margin: 1 }).then((url) => {
    if (!cancelled) setQrDataUrl(url);
  });
  return () => { cancelled = true; };
}, [displayUrl]);
```

**Copy handler**:
```ts
const copyListenerUrl = async () => {
  setCopyUrlBusy(true);
  setCopyUrlNotice(null);
  try {
    await copyTextToClipboard(displayUrl);
    setCopyUrlNotice("Copied!");
    setTimeout(() => setCopyUrlNotice(null), 2500);
  } catch {
    setCopyUrlNotice("Copy failed — select manually.");
  } finally {
    setCopyUrlBusy(false);
  }
};
```

**Share handler**:
```ts
const shareListenerUrl = async () => {
  if (!displayUrl) return;
  if (typeof navigator !== "undefined" && navigator.share) {
    setShareUrlBusy(true);
    try {
      await navigator.share({ url: displayUrl, title: "Join live translation" });
    } catch {
      // user cancelled or share failed — fallback to copy
      await copyListenerUrl();
    } finally {
      setShareUrlBusy(false);
    }
  } else {
    await copyListenerUrl();
  }
};
```

### 2.3 Listener Page — Mobile Polish

**File**: `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx`

**Change 1** — Hide `[F]` keyboard hint on touch devices:
```tsx
<span style={{ opacity: 0.55, fontWeight: 400 }} className="kb-hint">[F]</span>
```
Add CSS in the `<style>` block:
```css
@media (hover: none) { .kb-hint { display: none; } }
```

**Change 2** — Add mobile meta tags inside the page `<Head>` (import `Head` from `next/head`):
```tsx
<Head>
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
</Head>
```

---

## 3. File Change Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `frontend/pages/host/c/[churchSlug].tsx` | Modify | Replace plain displayUrl with QR card + copy/share |
| `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` | Modify | Mobile meta tags, hide [F] hint on touch |
| `frontend/package.json` | Modify | Add `qrcode` + `@types/qrcode` |

---

## 4. Dependencies

```bash
cd frontend
npm install qrcode
npm install --save-dev @types/qrcode
```

---

## 5. Implementation Order

1. Install `qrcode` package
2. Modify listener page (`[serviceKey].tsx`) — mobile meta + CSS fix
3. Modify host dashboard (`[churchSlug].tsx`) — QR card + copy/share buttons
4. Run `npm run lint` — fix any type errors
5. Test: open host dashboard, verify QR appears, scan with phone
