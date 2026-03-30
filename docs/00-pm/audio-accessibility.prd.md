# Product Requirements Document: Audio Accessibility

> Personal opt-in translated audio delivery via earbuds for church service listeners

**Feature**: audio-accessibility
**Date**: 2026-03-23
**Status**: Draft
**PM Agent Team**: pm-discovery, pm-strategy, pm-research, pm-prd

---

## Executive Summary

The audio-accessibility feature extends the real-time translation platform from subtitle-only output to personal opt-in audio translation delivered through individual listeners' earbuds. This addresses a critical accessibility gap: elderly members, visually impaired congregants, overflow room attendees, and online/hybrid participants who cannot effectively consume fast-moving subtitles. The feature leverages existing Google Cloud TTS and Gemini Flash TTS infrastructure already integrated in the backend, adding a streaming audio delivery path alongside the current text-based WebSocket pipeline. Audio is explicitly positioned as a personal, earbud-based accessibility feature -- not a sanctuary speaker replacement.

| Perspective | Summary |
|---|---|
| **Problem** | Subtitles exclude members who cannot read fast-moving text (elderly, visually impaired) and fail entirely in overflow rooms, cry rooms, and online settings where screens are absent or small |
| **Solution** | Personal opt-in TTS audio stream on the existing mobile listener page, delivered via earbuds using Google Cloud TTS/Gemini Flash through WebSocket audio chunks |
| **Functional UX Effect** | Listener taps a headphone icon to enable audio mode; translated sentences play sequentially through earbuds with <3s latency from speech; subtitle text remains visible as fallback |
| **Core Value** | Every congregation member can access translation regardless of visual ability, seating position, or physical location -- inclusion through choice rather than mandate |

---

## Table of Contents

1. [Discovery Analysis (Opportunity Solution Tree)](#1-discovery-analysis)
2. [Strategy Analysis (Value Proposition + Lean Canvas)](#2-strategy-analysis)
3. [Market Research (Personas, Competitors, Sizing)](#3-market-research)
4. [Beachhead Segment + GTM Strategy](#4-beachhead-segment--gtm-strategy)
5. [Product Requirements](#5-product-requirements)
6. [Technical Architecture](#6-technical-architecture)
7. [Success Metrics](#7-success-metrics)
8. [Risks and Mitigations](#8-risks-and-mitigations)

---

## 1. Discovery Analysis

### Opportunity Solution Tree (Teresa Torres Framework)

```
DESIRED OUTCOME
  "Every non-Korean-speaking member can fully participate in
   Korean-language services regardless of visual ability,
   seating location, or attendance mode"
   |
   +-- OPPORTUNITY 1: Accessibility Gap
   |   "Members with low vision / presbyopia / reading difficulty
   |    cannot consume subtitle-speed text"
   |   |
   |   +-- Solution A: Personal TTS audio via earbuds (SELECTED)
   |   |   - Leverage existing Google Cloud TTS + Gemini Flash
   |   |   - Stream MP3 chunks over WebSocket to mobile browser
   |   |   - Opt-in toggle on listener page
   |   |
   |   +-- Solution B: Adjustable font size / high contrast mode
   |   |   - Helps some users but does not solve fundamental
   |   |     reading-speed problem for elderly/impaired
   |   |
   |   +-- Solution C: Dedicated accessibility hardware (FM receivers)
   |       - High cost, church must purchase/maintain hardware
   |       - Does not scale, requires distribution each service
   |
   +-- OPPORTUNITY 2: Location Independence
   |   "People not in the main sanctuary miss translation entirely
   |    (overflow rooms, cry rooms, fellowship halls, online)"
   |   |
   |   +-- Solution A: Audio stream to personal device (SELECTED)
   |   |   - Works anywhere with internet: overflow, home, car
   |   |   - Same mobile page, same QR code entry point
   |   |
   |   +-- Solution B: Install screens in every overflow room
   |   |   - Capital expense, AV complexity, still subtitle-only
   |   |
   |   +-- Solution C: Separate livestream with voiceover
   |       - Requires a human interpreter per language
   |       - Defeats the purpose of AI translation
   |
   +-- OPPORTUNITY 3: Engagement Depth
   |   "Listeners reading subtitles cannot look at the speaker,
   |    worship freely, or take notes simultaneously"
   |   |
   |   +-- Solution A: Audio frees eyes and hands (SELECTED)
   |   |   - Listener hears translation while watching speaker
   |   |   - More natural worship experience
   |   |
   |   +-- Solution B: Slower subtitle display speed
   |       - Already exists as speed control in TranslationBox
   |       - Does not fundamentally solve eyes-on-screen problem
   |
   +-- OPPORTUNITY 4: Small Group / Bible Study Use
       "Non-sanctuary settings (seminars, Bible studies, small
        groups) where a screen is impractical"
       |
       +-- Solution A: Same personal audio feature (SELECTED)
       |   - No screen setup needed, each person uses phone
       |   - Works for ad-hoc meetings without AV infrastructure
       |
       +-- Solution B: Portable translation screen/tablet
           - Added hardware cost and setup time
```

### Opportunity Prioritization

| Opportunity | Impact | Feasibility | Confidence | Priority |
|---|---|---|---|---|
| Accessibility Gap (vision/elderly) | High | High (TTS exists) | High | P0 |
| Location Independence (overflow/online) | High | High | High | P0 |
| Engagement Depth (eyes-free worship) | Medium | High | Medium | P1 |
| Small Group / Bible Study | Medium | Medium | Medium | P2 |

---

## 2. Strategy Analysis

### Jobs-To-Be-Done: 6-Part Value Proposition (Pawel Huryn Framework)

| Part | Description |
|---|---|
| **1. Target Customer** | Non-Korean-speaking church members attending Korean-language services, particularly elderly (65+), visually impaired, parents in cry rooms, and remote/hybrid attendees |
| **2. Situation / Trigger** | Sunday service begins; the listener opens the mobile translation page (via QR code or bookmark); the fast-scrolling subtitles are hard to follow because of vision limitations, distance from screen, or absence of a screen entirely |
| **3. Job to Be Done** | When I am attending a Korean-language church service and cannot effectively read the subtitles, I want to hear an English audio translation through my earbuds so I can fully participate in the service without visual dependency on a screen |
| **4. Unmet Need** | Current subtitle-only delivery assumes visual acuity and screen proximity; no audio alternative exists despite TTS infrastructure being present in the backend |
| **5. Value Proposition** | Tap a single button on the mobile listener page to switch from subtitle-only to personal audio translation; hear clear English speech through your earbuds with under 3 seconds additional latency; subtitles remain visible as synchronized reference |
| **6. Differentiation** | Unlike Wordly/Palabra/OneAccord which treat audio as a whole-room broadcast concern, this feature is personal, opt-in, and earbud-first -- it does not compete with the live Korean speaker, it supplements the experience privately for those who need it |

### Lean Canvas

| Section | Content |
|---|---|
| **Problem** | 1. Elderly/visually impaired members cannot read fast subtitles. 2. Overflow/cry rooms and online attendees have no screen or a small screen. 3. Subtitle reading forces eyes away from the speaker, reducing worship engagement. |
| **Customer Segments** | Primary: Elderly congregation members (65+), visually impaired members. Secondary: Overflow room / cry room parents. Tertiary: Online/hybrid attendees, small group participants. |
| **Unique Value Proposition** | "Hear the sermon in your language, through your own earbuds -- no special hardware, no screen required." Personal, opt-in audio translation that works everywhere the mobile page reaches. |
| **Solution** | Server-side TTS synthesis of translated text, streamed as audio chunks via WebSocket to the listener's mobile browser. Opt-in toggle on existing listener page. Web Audio API playback with sentence queuing. |
| **Channels** | Same QR code / short URL entry point already in use. Church admin enables audio in org settings. Onboarding tooltip on listener page. |
| **Revenue Streams** | Audio minutes counted toward existing plan billing. Potential premium tier for audio-enabled plans. TTS cost pass-through at plan level. |
| **Cost Structure** | Google Cloud TTS: ~$4-16 per 1M characters (Neural2). Gemini Flash TTS: similar or lower. WebSocket bandwidth: marginal increase (MP3 ~32kbps). No additional infrastructure needed -- Cloud Run handles the load. |
| **Key Metrics** | Audio opt-in rate per service. Audio listener retention (do they keep it on?). Listener satisfaction score. Audio vs subtitle session duration. TTS cost per audio-minute. |
| **Unfair Advantage** | Google Cloud TTS + Gemini Flash TTS already integrated and production-tested. WebSocket infrastructure already serving listeners. Mobile listener page already deployed. Korean-to-English translation pipeline already optimized for church context. Zero new infrastructure required. |

---

## 3. Market Research

### 3.1 User Personas

#### Persona 1: "Grandmother Kim" (Primary -- Accessibility)

| Attribute | Detail |
|---|---|
| **Age** | 72 |
| **Role** | Long-time church member, Korean-born, limited English but wants to follow English translation for grandchildren's sake |
| **Technical Skill** | Low -- can use smartphone for KakaoTalk and photos, children help with setup |
| **Pain Point** | Subtitles on the big screen scroll too fast; presbyopia makes phone screen hard to read even with glasses; cannot follow the English translation and has to rely on Korean audio from the speaker |
| **Desired Outcome** | Tap one button and hear English translation in earbuds; set and forget |
| **Quote** | "My grandson sits with me but he only speaks English. I wish we could both understand the sermon together." |
| **Audio Need** | Critical -- subtitles are not a viable option for this persona |

#### Persona 2: "Sarah the Young Mom" (Secondary -- Location Independence)

| Attribute | Detail |
|---|---|
| **Age** | 34 |
| **Role** | English-speaking wife of a Korean church member; frequently in the cry room with a toddler |
| **Technical Skill** | High -- comfortable with apps, already uses the QR code listener page |
| **Pain Point** | The cry room has no subtitle screen; she can hear the Korean sermon faintly through the wall but cannot understand it; currently just waits until the service ends |
| **Desired Outcome** | Listen to English translation on earbuds (one ear in, one ear monitoring toddler) while in the cry room |
| **Quote** | "I drive 30 minutes to church and spend the whole service in the cry room not understanding a word." |
| **Audio Need** | High -- no screen means subtitles are useless |

#### Persona 3: "David the Remote Member" (Tertiary -- Online/Hybrid)

| Attribute | Detail |
|---|---|
| **Age** | 45 |
| **Role** | Korean-American professional, attends online when traveling; mother is in Korea and watches the livestream |
| **Technical Skill** | High -- software engineer, comfortable with any interface |
| **Pain Point** | Online livestream is Korean-only audio; he opens the listener page on a second device for subtitles but switching between screens is cumbersome |
| **Desired Outcome** | Single mobile page that plays English audio translation overlaid on his listening experience, so he can watch the Korean livestream video while hearing English |
| **Quote** | "I want to set my mom up with audio translation so she can follow the English service for our LA church from Seoul." |
| **Audio Need** | Medium-High -- subtitles work but audio is significantly more convenient for dual-screen setup |

### 3.2 Competitor Analysis

| # | Competitor | Audio Translation | Church Focus | Pricing Model | Key Differentiator | Weakness vs Our Feature |
|---|---|---|---|---|---|---|
| 1 | **Wordly** | Yes (via QR code, personal device) | Yes (dedicated page) | Per-hour packages, starts ~$5K/year; NPO discounts | Broad language support (50+), enterprise-grade, post-session transcripts | Generic AI, not church-context-trained; requires separate subscription; no Korean sermon optimization |
| 2 | **Palabra** | Yes (live audio + captions, personal device) | Yes (primary vertical) | Per-minute usage, ~4x cheaper than interpreters | <1sec latency claim, context-aware for church terminology | Requires OBS/vMix audio routing setup; not self-hosted; separate platform from any existing workflow |
| 3 | **OneAccord** | Yes (written + audio) | Yes (church-only) | $150/month for 5hrs; custom quotes | Biblical terminology-trained AI, moderation capability, 200+ church subscribers | Relatively new (since Dec 2023); limited to their platform; no existing integration with your church's infrastructure |
| 4 | **LiveVoice** | Yes (audio streaming to personal devices) | Yes (church + events) | Per-listener subscription, day/month/year plans | Pure audio streaming, no translation dependency, NPO pricing | Audio streaming only (no translation built in -- requires human interpreter); separate app download required |
| 5 | **Stenomatic** | Yes (voice-to-voice + captions) | Yes (church + events) | Contact for pricing; "best value" per reviews | 75+ languages, integrates with Zoom/YouTube Live, no app download | Generic event platform, not specialized for Korean church context; no sermon preparation workflow integration |

#### Competitive Positioning Summary

**Our Unique Position**: We are the only platform that combines:
1. Korean-language-optimized STT (Deepgram nova-3 tuned for Korean)
2. Church-context-trained translation (GPT-4o with sermon prompts, Bible verse detection)
3. Existing mobile listener infrastructure (QR code, WebSocket, no app download)
4. Server-side TTS already integrated (Google Cloud TTS + Gemini Flash)
5. End-to-end control of the translation pipeline (STT -> Translation -> TTS, no third-party intermediary)

No competitor has all five. Adding audio is an incremental feature, not a new product.

### 3.3 Market Sizing

#### TAM (Total Addressable Market)

The global language interpretation market is valued at $61-76 billion (2025), growing at ~5% annually. The AI-powered real-time interpretation segment is growing faster at ~15-20% CAGR.

**Religious institution interpretation market estimate**:
- ~400,000 churches in the US alone have multilingual congregations
- Global churches with multilingual needs: estimated 1-2 million
- Average willingness to pay for translation technology: $50-200/month
- **TAM**: $600M - $4.8B annually (global religious institution translation)

#### SAM (Serviceable Addressable Market)

Narrowing to Korean-language churches and Korean diaspora churches specifically:
- ~2,800-4,000 Korean churches in the US
- ~3,000 Korean churches in other diaspora countries (Canada, Australia, Japan, UK, etc.)
- Korean churches with active English-speaking members needing translation: ~40% = ~2,400-2,800
- Average plan value: $30-60/month (our pricing)
- **SAM**: $864K - $2.0M annually

#### SOM (Serviceable Obtainable Market)

Realistic 3-year capture:
- Year 1: 50-100 churches (early adopters, word-of-mouth in Korean church networks)
- Year 2: 200-400 churches (with audio feature as differentiator)
- Year 3: 500-800 churches
- Average revenue per church: $40/month (Growth plan)
- **SOM Year 3**: $240K - $384K annually

**Audio feature impact on SOM**: Audio accessibility is estimated to increase conversion by 15-25% for churches with elderly/mixed-generation congregations, and to reduce churn by 10-15% by addressing the "my mom can't use subtitles" complaint.

---

## 4. Beachhead Segment + GTM Strategy

### Beachhead Segment Selection (Geoffrey Moore Framework)

| Criteria | Korean Immigrant Churches (US, 500+ members) | Small Korean Churches (US, <200 members) | Korean Diaspora (Non-US) | Generic Multilingual Churches |
|---|---|---|---|---|
| **Compelling Reason to Buy** | 9/10 -- Multi-generational, elderly members, English-speaking second-gen | 7/10 -- Need exists but fewer affected members | 6/10 -- Language varies, less standardized | 4/10 -- Not Korean-specific |
| **Whole Product Leverage** | 9/10 -- Already use our STT pipeline, translation optimized for Korean sermons | 7/10 -- Same tech, smaller impact | 5/10 -- May need different language pairs | 3/10 -- Would need new STT models |
| **Word-of-Mouth Network** | 10/10 -- Korean church networks (denomination, regional associations) are tight-knit | 7/10 -- Less influence | 4/10 -- Geographically dispersed | 2/10 -- No shared network |
| **Competition Weakness** | 8/10 -- No competitor is Korean-church-specialized | 8/10 -- Same | 6/10 -- Competitors have broader language support | 3/10 -- Wordly/Stenomatic serve this well |
| **TOTAL** | **36/40** | 29/40 | 21/40 | 12/40 |

**Selected Beachhead**: Mid-to-large Korean immigrant churches in the US (500+ weekly attendance) with multi-generational English-speaking members.

### GTM Strategy

#### Positioning Statement

"For Korean immigrant churches with elderly and English-speaking members, our audio translation feature provides personal, earbud-based English audio of the Korean sermon -- so every member can participate fully, regardless of vision, seating, or location."

#### Key Messaging: Accessibility, Not Replacement

| DO Position As | DO NOT Position As |
|---|---|
| Accessibility feature for those who need it | Replacement for subtitles |
| Personal, private, opt-in through earbuds | Sanctuary-wide audio broadcast |
| Inclusion of elderly and visually impaired | Technology showcase |
| Extension of existing mobile listener experience | New product or separate app |
| Complement to the subtitle experience | Competition with the live Korean speaker |

#### Launch Channels

| Channel | Action | Timeline |
|---|---|---|
| **In-Product** | "Audio" toggle on listener page with onboarding tooltip | Launch day |
| **Admin Dashboard** | "Enable Audio Translation" org setting with cost preview | Launch day |
| **Email to Existing Customers** | "New: Audio translation for members who need it" with accessibility framing | Week 1 |
| **Korean Church Networks** | Feature announcement via Korean Church Association newsletters | Month 1 |
| **Case Study** | Partner with 2-3 early-adopter churches for testimonials | Month 2-3 |
| **Conference/Events** | Demo at Korean American church leadership conferences | Quarter 2 |

#### Launch Metrics

| Metric | Target (90 days) |
|---|---|
| Audio opt-in rate (% of listeners per service) | 15-25% |
| Audio retention (% who keep audio on for >50% of service) | 70%+ |
| New church signups citing audio feature | 10+ |
| Existing church plan upgrades | 5+ |
| Listener NPS improvement | +10 points |

---

## 5. Product Requirements

### 5.1 User Stories

#### P0 (Must Have -- Launch)

| ID | As a... | I want to... | So that... | Acceptance Criteria |
|---|---|---|---|---|
| US-01 | Listener on mobile | Tap a headphone icon to enable audio translation | I can hear the English translation through my earbuds | Toggle appears on listener page; audio plays within 3s of text appearing; toggle state persists in localStorage |
| US-02 | Listener with audio enabled | Hear each translated sentence spoken clearly | I can follow the sermon without looking at the screen | Sentences queue and play sequentially; no overlapping; natural pause between sentences |
| US-03 | Listener | Adjust audio playback speed | I can match the speed to my comprehension level | Speed control (0.75x, 1.0x, 1.25x, 1.5x) available; setting persists |
| US-04 | Listener | See subtitles alongside audio | I can use both modalities when helpful | Subtitle display remains active when audio is on; current spoken sentence highlighted |
| US-05 | Church admin | Enable/disable audio translation for my organization | I can control costs and feature availability | Org setting in admin dashboard; disabled by default |
| US-06 | Church admin | See audio usage in my billing dashboard | I can understand the cost impact | Audio minutes shown separately in usage display |

#### P1 (Should Have -- Fast Follow)

| ID | As a... | I want to... | So that... |
|---|---|---|---|
| US-07 | Listener | Choose between male and female TTS voice | The voice feels more natural to me |
| US-08 | Listener | Control volume independently from phone volume | I can fine-tune the translation audio level |
| US-09 | Church admin | Choose the TTS voice/provider for my organization | The audio matches our preference |
| US-10 | Listener | Have audio automatically pause during music/silence | I don't hear robotic "silence" translations |

#### P2 (Nice to Have -- Future)

| ID | As a... | I want to... | So that... |
|---|---|---|---|
| US-11 | Listener | Download audio recording of the translated sermon | I can review it later |
| US-12 | Church admin | Set per-language TTS voices | Each translation language sounds appropriate |
| US-13 | Listener | Use audio in "conversation mode" for Bible study | Small group translation works without screens |

### 5.2 Functional Requirements

#### FR-01: Audio Toggle on Listener Page

- Headphone icon toggle on the mobile listener page (`/c/[churchSlug]/s/[serviceKey]`)
- Default state: OFF (subtitle-only, current behavior)
- Toggle requires user gesture (browser autoplay policy compliance)
- Audio state persisted in `localStorage` per org/service
- When enabled, shows audio status indicator (playing/buffering/paused)

#### FR-02: Server-Side TTS Synthesis

- When audio is enabled for an org, the backend synthesizes TTS for each committed translation
- Use existing `google_tts.synthesize_async()` or `gemini_flash_tts.synthesize_async()` based on org config
- Audio format: MP3 (already supported), 32kbps mono (sufficient for speech)
- TTS synthesis runs in parallel with text broadcast (does not delay subtitle delivery)
- Cache TTS results for the same text within a room session (deduplication)

#### FR-03: Audio Delivery via WebSocket

- Extend the existing `/ws/translate` WebSocket protocol with an `audio` message type
- Audio chunks delivered as base64-encoded MP3 alongside the translation text
- Message format: `{ type: "translation", payload: "text", audio: "base64_mp3", meta: {...} }`
- Audio only sent to connections that have opted in (via `{ type: "audio_subscribe" }` message)
- Fallback: if TTS fails, text-only delivery continues (fail-open for subtitles)

#### FR-04: Client-Side Audio Playback

- Web Audio API-based playback with sentence queue
- Sequential playback: new sentence waits for current to finish (no overlap)
- If queue grows too long (>3 sentences behind), skip to latest (catch-up mode)
- Handle browser autoplay restrictions: require initial user tap
- Handle mobile background audio: request `audio` media session
- Graceful handling of audio focus loss (phone call, notification)

#### FR-05: Org-Level Audio Configuration

- New field in org settings: `audioTranslationEnabled` (boolean, default: false)
- Optional: `audioVoice` (string, TTS voice name override)
- Optional: `audioProvider` (string, "google" | "gemini_flash")
- Optional: `audioSpeakingRate` (float, 0.75 - 1.5)
- Settings stored in Firestore `organizations/{orgId}` document

#### FR-06: Audio Usage Metering

- Track audio synthesis minutes per org per billing period
- Audio minutes counted separately from translation minutes
- Display in admin dashboard usage section
- Audio minutes included in plan limits (no separate audio plan)

### 5.3 Non-Functional Requirements

| Requirement | Target | Notes |
|---|---|---|
| Audio latency (text available to audio playing) | < 3 seconds | TTS synthesis + WebSocket delivery + decode |
| TTS synthesis time per sentence | < 1.5 seconds | Google Cloud TTS Neural2 typical: 200-800ms for ~20 words |
| Audio quality | Intelligible speech, not "robotic" | Neural2 voices preferred over Standard |
| Concurrent audio listeners per room | 50+ | MP3 broadcast, not per-user synthesis |
| Mobile battery impact | < 5% additional per hour | Web Audio API is efficient; MP3 decode is lightweight |
| Offline resilience | Graceful degradation to subtitles | If WebSocket drops, subtitle reconnect is unchanged |
| Browser support | Chrome, Safari (iOS), Samsung Internet | Covers >95% of church attendee devices |

### 5.4 Scope Boundaries

#### In Scope (v1)

- Personal earbud audio via mobile browser
- Opt-in toggle on existing listener page
- Server-side TTS using existing Google Cloud TTS / Gemini Flash
- MP3 audio delivery over existing WebSocket
- Org-level enable/disable setting
- Audio usage metering

#### Out of Scope (v1)

- Sanctuary speaker audio broadcast (explicitly excluded -- conflicts with live Korean speaker)
- Multiple simultaneous target languages for audio (v1: English only, matching primary text translation)
- Offline audio recording / download
- Custom voice cloning or voice selection UI
- Separate mobile app (web-only)
- Audio for pre-service translation (sermon script TTS -- different feature)
- Audio latency below 1 second (unrealistic for server-side TTS pipeline)

---

## 6. Technical Architecture

### 6.1 Audio Translation Pipeline

```
Korean Audio (Host mic)
    |
    v
Deepgram STT (nova-3, Korean)
    |
    v
Korean Transcript (committed sentence)
    |
    v
OpenAI GPT-4o Translation (Korean -> English)
    |
    +---> Text broadcast via WebSocket (existing, unchanged)
    |     (all listeners receive text immediately)
    |
    +---> TTS Synthesis (NEW, parallel)
          |
          +---> Google Cloud TTS Neural2 OR Gemini Flash TTS
          |     (based on org config)
          |
          v
          MP3 audio bytes
          |
          v
          Base64 encode + attach to WebSocket message
          |
          v
          Deliver to audio-subscribed listeners only
          |
          v
          Client: Web Audio API decode + queue + playback
```

### 6.2 Key Technical Decisions

| Decision | Choice | Rationale |
|---|---|---|
| TTS location | Server-side (Google Cloud TTS) | Higher quality than browser SpeechSynthesis; consistent across devices; already integrated |
| Audio format | MP3 32kbps mono | Small file size (~4KB/sentence), universal browser decode, adequate speech quality |
| Delivery mechanism | WebSocket (existing `/ws/translate`) | No new connections needed; already handles room-scoped broadcast |
| Client playback | Web Audio API with AudioWorklet | Low-latency decode, better mobile support than `<audio>` tag, queue management |
| Opt-in mechanism | WebSocket message `audio_subscribe` | Per-connection toggle; server only synthesizes/sends if at least one subscriber |
| TTS caching | Per-room session cache (in-memory) | Same sentence translated once, audio served to all subscribers; evict on room close |

### 6.3 Files to Modify/Create

| File | Change | Scope |
|---|---|---|
| `backend/app/main.py` | Handle `audio_subscribe`/`audio_unsubscribe` messages in `/ws/translate`; add TTS synthesis call after translation commit | Modify |
| `backend/app/socket_manager.py` | Track audio subscribers per room; add `broadcast_room_audio()` method | Modify |
| `backend/app/services/audio_tts_service.py` | New service: TTS synthesis orchestrator with caching, provider selection, rate limiting | Create |
| `backend/app/services/multichurch_store.py` | Add `audioTranslationEnabled`, `audioVoice`, `audioProvider` fields to org config | Modify |
| `backend/app/routes/org_settings.py` | Add audio configuration endpoints | Modify |
| `frontend/utils/useAudioTranslation.ts` | New hook: audio subscription, Web Audio API playback, queue management | Create |
| `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` | Add audio toggle UI, integrate `useAudioTranslation` hook | Modify |
| `frontend/components/AudioControls.tsx` | New component: audio toggle, speed control, status indicator | Create |

### 6.4 Cost Analysis

| Item | Cost | Notes |
|---|---|---|
| Google Cloud TTS Neural2 | $16 per 1M characters | ~5,000 characters per 10-min sermon segment |
| Gemini Flash TTS | Similar or lower | May offer better pricing at scale |
| Per sermon (45 min, ~22,500 chars English) | ~$0.36 | Well within plan margins |
| Per church per month (4 services) | ~$1.44 | Negligible additional cost |
| WebSocket bandwidth (MP3 32kbps) | ~17MB per 45-min service per listener | Marginal vs existing text WebSocket traffic |

**Conclusion**: Audio feature adds approximately $1-2/month in TTS costs per church. This is easily absorbed into existing plan pricing without a price increase.

---

## 7. Success Metrics

### Primary Metrics

| Metric | Definition | Target (90 days) | Measurement |
|---|---|---|---|
| **Audio Adoption Rate** | % of listeners who enable audio at least once per service | 20% | WebSocket `audio_subscribe` events / total connections |
| **Audio Retention Rate** | % of audio-enabled listeners who keep it on for >50% of service duration | 70% | Audio subscription duration / session duration |
| **Subtitle-to-Audio Migration** | % of sessions where listener uses audio as primary (audio on >80% of time) | 10% | Per-session audio duration analysis |

### Secondary Metrics

| Metric | Definition | Target (90 days) |
|---|---|---|
| **Audio Latency (p95)** | Time from text availability to audio playback start | < 3 seconds |
| **TTS Failure Rate** | % of translation commits where TTS fails | < 1% |
| **Audio-Driven Signups** | New church signups that cite audio in onboarding | 10+ |
| **Plan Upgrade Rate** | Existing churches upgrading plan after audio launch | 5+ |
| **Listener NPS Delta** | Change in listener NPS after audio launch | +10 points |

### Cost Metrics

| Metric | Definition | Alert Threshold |
|---|---|---|
| **TTS Cost per Church per Month** | Google Cloud TTS charges per org | > $5/month |
| **Audio Bandwidth per Service** | Total MP3 data delivered per service | > 500MB |
| **TTS Synthesis Latency (p95)** | Server-side TTS generation time | > 2 seconds |

---

## 8. Risks and Mitigations

| # | Risk | Severity | Probability | Mitigation |
|---|---|---|---|---|
| R1 | **Browser autoplay policy blocks audio** | High | High | Require explicit user tap to enable audio; use Web Audio API (more permissive than `<audio>` tag); show clear "tap to start" prompt |
| R2 | **Audio latency exceeds acceptable threshold** | Medium | Medium | Pre-synthesize during translation; cache aggressively; skip sentences when queue is too long; tune speaking rate |
| R3 | **TTS voice sounds robotic / unnatural** | Medium | Low | Use Neural2 voices (high quality); allow org to select voice; Gemini Flash TTS offers more natural options |
| R4 | **Audio competes with Korean speaker in sanctuary** | High | Low | Explicitly position as earbud-only; no volume amplification feature; default OFF; admin-only enable |
| R5 | **TTS cost escalation at scale** | Low | Low | Current cost analysis shows <$2/church/month; cache identical sentences; monitor per-org usage |
| R6 | **Mobile battery drain** | Medium | Low | MP3 decode is lightweight; Web Audio API is GPU-accelerated on mobile; test on older devices |
| R7 | **iOS Safari audio restrictions** | High | Medium | Handle `AudioContext` resume on user gesture; test on iOS 16+; fallback to `HTMLAudioElement` if needed |
| R8 | **Congregants play audio without earbuds (speaker leakage)** | Medium | Medium | Show "please use earbuds" prompt; detect speaker vs headphone output if possible; cultural guidance to church admin |

---

## Appendix A: Existing Infrastructure Inventory

The following components are already production-ready and require only integration, not creation:

| Component | File | Status |
|---|---|---|
| Google Cloud TTS synthesis | `backend/app/services/google_tts.py` | Production, Neural2 voices, async |
| Gemini Flash TTS synthesis | `backend/app/services/gemini_flash_tts.py` | Production, async, with auth |
| TTS HTTP endpoint | `backend/app/routes/translate.py` (`POST /tts`) | Production, authenticated |
| Browser TTS hook (client-side fallback) | `frontend/utils/useTTS.ts` | Production, SpeechSynthesis API |
| WebSocket listener connection | `frontend/utils/useTranslationSocket.ts` | Production, reconnection, heartbeat |
| Listener mobile page | `frontend/pages/c/[churchSlug]/s/[serviceKey].tsx` | Production, QR code entry |
| Room-scoped WebSocket broadcast | `backend/app/socket_manager.py` | Production, per-room messaging |
| Translation pipeline | `backend/app/utils/translate.py` | Production, GPT-4o, context-aware |

## Appendix B: Competitor Feature Matrix

| Feature | Our Platform (Post-Audio) | Wordly | Palabra | OneAccord | LiveVoice | Stenomatic |
|---|---|---|---|---|---|---|
| Korean STT optimization | Yes (Deepgram nova-3) | Generic | Generic | Generic | N/A | Generic |
| Church-context translation | Yes (GPT-4o + prompts) | No | Partial | Yes | N/A | No |
| Personal audio (earbuds) | Yes | Yes | Yes | Yes | Yes | Yes |
| QR code / no-app access | Yes | Yes | Yes | Yes | No (app) | Yes |
| Sermon preparation workflow | Yes | No | No | No | No | No |
| Bible verse detection | Yes | No | No | Partial | No | No |
| Self-hosted / no vendor lock | Yes | No | No | No | No | No |
| Audio + subtitle simultaneous | Yes | Yes | Yes | Partial | Audio only | Yes |
| Korean church specialization | Yes | No | No | No | No | No |

## Appendix C: Attribution

This PRD was produced by the PM Agent Team integrating frameworks from:
- **Opportunity Solution Tree**: Teresa Torres, "Continuous Discovery Habits"
- **JTBD 6-Part Value Proposition**: Pawel Huryn, [pm-skills](https://github.com/phuryn/pm-skills) (MIT License)
- **Lean Canvas**: Ash Maurya, "Running Lean"
- **Beachhead Segment**: Geoffrey Moore, "Crossing the Chasm"
- **Market Research**: Web research conducted 2026-03-23

---

*Next step: `/pdca plan audio-accessibility` (this PRD will be auto-referenced)*
