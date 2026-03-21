# Plan: Mobile Listener Access

## Executive Summary

| Perspective | Description |
|-------------|-------------|
| Problem | Church attendees have no easy way to join the live translation broadcast on their phones — the listener URL exists but there's no QR code to scan and no share mechanism in the host dashboard. |
| Solution | Add a QR code widget + Copy/Share buttons for the listener URL in the host broadcast tab, and polish the listener page for mobile browsers. |
| Function UX Effect | Host displays QR on screen → attendees scan → phone opens listener page showing live English captions instantly, no app install needed. |
| Core Value | Zero-friction mobile access to live Korean→English translation for every church attendee. |

---

## 1. Feature Overview

**Feature Name**: mobile-listener-access
**Priority**: High
**Scope**: Frontend only

### 1.1 Problem Statement

The listener page (`/c/[churchSlug]/s/[serviceKey]`) is fully functional but inaccessible in practice because:
- The host dashboard only shows the listener URL as a plain text link with no way to copy or share it
- There is no QR code for attendees to scan — this was advertised on the landing page but never built
- The listener page has minor mobile UX issues (keyboard shortcut hint `[F]` on touch devices)

### 1.2 Goals

1. QR code displayed in the host broadcast tab for the listener URL
2. "Copy URL" and "Share via..." buttons for the listener URL
3. Mobile UX polish on the listener page (hide keyboard hints on touch, touch-friendly toggle)
4. Proper mobile meta tags on the listener page (viewport, apple-mobile-web-app)

### 1.3 Non-Goals

- Native mobile app (React Native / Flutter)
- Push notifications
- Audio streaming to phone (TTS is out of scope)
- Changes to the backend

---

## 2. User Stories

| # | As a... | I want to... | So that... |
|---|---------|-------------|------------|
| US-01 | Host | See a QR code for my listener URL in the broadcast tab | Attendees can scan and join instantly |
| US-02 | Host | Copy the listener URL with one click | I can paste it in a bulletin or messaging app |
| US-03 | Host | Share the listener URL via the phone's share sheet | I can send it via text/email from my phone |
| US-04 | Attendee | Scan a QR code and see live translation | I don't need to type a URL |
| US-05 | Attendee | View the translation clearly on my phone screen | Text is readable without zooming |

---

## 3. Technical Approach

### 3.1 QR Code Library

Use `qrcode` npm package (browser-compatible canvas/SVG renderer). No `qrcode-terminal` — that's Node-only.

```bash
npm install qrcode
npm install --save-dev @types/qrcode
```

### 3.2 Host Dashboard Changes

File: `frontend/pages/host/c/[churchSlug].tsx`

- Replace the plain `displayUrl` link block with a richer "Listener Access" card:
  - QR code (SVG, ~200×200px) generated from `displayUrl`
  - URL text (truncated, click to open)
  - "Copy URL" button → `copyTextToClipboard(displayUrl)`
  - "Share via..." button → `navigator.share({ url: displayUrl })` with fallback to copy

### 3.3 Listener Page Changes

File: `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx`

- Hide `[F]` hint on touch devices (CSS `@media (hover: none)`)
- Add `apple-mobile-web-app-capable` and `apple-mobile-web-app-status-bar-style` meta tags via `_document.tsx` or `<Head>` in the page
- Verify viewport meta is set (it comes from `_document.tsx`)

---

## 4. Acceptance Criteria

| # | Criterion | Test |
|---|-----------|------|
| AC-01 | QR code appears in broadcast tab when a service is selected | Visual check |
| AC-02 | QR code encodes the full listener URL | Scan with phone → opens correct page |
| AC-03 | "Copy URL" copies to clipboard and shows confirmation | Click → toast/label changes |
| AC-04 | "Share via..." opens native share sheet on mobile | Test on iOS/Android |
| AC-05 | Listener page renders correctly on 375px-wide screen | DevTools responsive view |
| AC-06 | `[F]` keyboard hint hidden on touch devices | DevTools mobile emulation |
| AC-07 | `npm run lint` passes with no errors | CI check |
