---
title: "Web UI Redesign - Plan"
type: feat
date: "2026-08-06"
topic: "web-ui-redesign"
artifact_contract: "ce-unified-plan/v1"
artifact_readiness: "implementation-ready"
product_contract_source: "ce-plan-bootstrap"
execution: code
---

# Web UI Redesign - Plan

## Goal Capsule

- **Objective:** Give the `web/` frontend (currently bare, unstyled HTML) a polished, intuitive visual design using Tailwind CSS, covering the shared navigation shell and all three pages (Review Queue, Settings, Onboarding), without changing any existing backend contract or frontend behavior.
- **Product authority:** This plan owns visual design and layout only — component structure, styling, and navigation chrome. It does not add, remove, or change any API call, business rule, or data shape.
- **Open blockers:** None. Styling approach, scope, visual direction, and navigation shape were all settled with the user this session (see Key Decisions).
- **Execution profile:** Six implementation units (U1–U6): Tailwind setup, shared layout/nav shell, shared component restyle (banners + BPM attribution), then the three pages in dependency order.
- **Stop conditions:** None identified — this is a self-contained frontend styling change with no external integration risk.
- **Tail ownership:** The implementer runs the existing Vitest suite plus a manual visual check in the browser (`npm run dev`) after each unit — Tailwind class mistakes (typos, wrong breakpoint) are visual, not something the test suite catches.
- **Product Contract preservation:** N/A — greenfield within this plan (`product_contract_source: ce-plan-bootstrap`), no origin document to preserve against.

---

## Product Contract

### Summary

The backend and core product loop (review queue, onboarding, YouTube auth) already work end-to-end but the frontend has zero styling — bare HTML elements, no visual hierarchy, no feedback affordances beyond raw text. This plan adds Tailwind CSS and redesigns the whole app in one pass: a shared top-navbar layout shell, and card-based, iOS-influenced visual treatment (rounded corners, soft shadows, pill-shaped controls, system font stack, a single indigo accent color) applied consistently across Review Queue, Settings, and Onboarding.

### Requirements

- R1. Tailwind CSS is installed and configured in the Vite project, building successfully in dev and production (`npm run build`).
- R2. A shared layout shell renders a top navbar (app name, links to Review Queue / Onboarding / Settings, connection-health indicator dots) and wraps all three pages consistently.
- R3. Review Queue, Settings, and Onboarding are visually redesigned — consistent spacing, typography, color palette, card-based grouping, styled buttons and forms — while preserving every existing behavior.
- R4. `ConnectionHealthBanners` and `BpmAttribution` are restyled to match the new visual language (status-colored alert banners; a small attribution badge) without changing when/whether they render.
- R5. All existing `data-testid` attributes, ARIA roles (`role="alert"`, `role="status"`), and exact visible text asserted by `web/tests/ReviewQueue.test.tsx` are preserved so the existing suite passes unmodified.
- R6. The visual language follows the settled direction: clean minimal light theme, single accent color, iOS-influenced treatment (rounded-2xl cards, soft shadows, pill buttons, system font stack, segmented-control-style nav).

### Scope Boundaries

- No new pages, routes, or features — this is a styling and layout pass over the existing three pages.
- No React Router adoption — `App.tsx`'s existing manual page-state switching (`useState<Page>`) is kept as-is; only its rendering is restyled.
- No dark mode / theme toggle (explicitly deferred — user chose light-only for this pass).
- No component library (e.g., Mantine, shadcn) — Tailwind utility classes only, per the settled styling-approach decision.
- No backend changes of any kind.

#### Deferred to Follow-Up Work

- Dark mode support (the user chose light-only now; a toggle was considered but deferred).
- A left-sidebar navigation alternative (top navbar was chosen; revisit only if the app grows enough pages that a navbar gets crowded).
- Replacing the Review Queue's native `window.prompt()` Move interaction with a styled in-app playlist picker (U4 keeps the existing mechanic for this styling-only pass; revisit if the native dialog's visual clash proves worse in practice than expected).

### Key Decisions

- Styling approach: **Tailwind CSS**, no component library (session-settled: user-directed — chosen over a component library like Mantine/shadcn to avoid an added dependency and learning surface, and over hand-written CSS to avoid slower, less-consistent styling as the app grows; resolved via AskUserQuestion this session).
- Redesign scope: **whole app in one pass** (session-settled: user-directed — chosen over a Review-Queue-only first pass, since the user wants a consistent look across the app now rather than a staged rollout; resolved via AskUserQuestion this session).
- Visual direction: **clean minimal light theme** (session-settled: user-directed — chosen over a dark theme or a light/dark toggle; toggle was rejected specifically for its added implementation and testing surface for a single-user personal tool; resolved via AskUserQuestion this session).
- Navigation shape: **top navbar** (session-settled: user-directed — chosen over a left sidebar; the app has exactly 3 pages today and a slim top bar matches that scope better than a sidebar built for future growth; resolved via AskUserQuestion this session).
- Additional stylistic direction volunteered mid-session by the user: apply **iOS-style elements where applicable** (rounded corners, pill-shaped buttons/segmented controls, soft shadows) — folded into KTD2 below as part of the visual language, not a separate settled decision requiring its own confirmation (a styling refinement within the already-settled "clean minimal light theme" direction, not a scope or approach fork).

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Tailwind CSS v3 via PostCSS + Autoprefixer, configured through Vite's standard PostCSS pipeline** (session-settled: user-directed, see Key Decisions) — the standard, documented integration path for Vite + Tailwind; no Vite plugin variant needed since PostCSS already fits this project's existing Vite/TS toolchain. The `tailwindcss`/`postcss`/`autoprefixer` versions must be pinned to major v3/v8/v10 respectively when installed (an unpinned install resolves to Tailwind v4, a different config model — no `tailwind.config.ts`, no `@tailwind` directives — incompatible with this KTD's file layout). `postcss.config.js` must use `.cjs` (or ESM `export default` syntax) since `web/package.json` sets `"type": "module"`, under which a plain `module.exports`-style `postcss.config.js` fails to load. Governs R1.
- KTD2. **iOS-influenced visual language layered on top of the "clean minimal light theme" decision**: system font stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif`), `rounded-2xl` cards with a soft shadow (`shadow-sm` + a 1px `slate-200` border) rather than heavy borders, pill-shaped (`rounded-full`) primary buttons and the nav's active-tab indicator, and a single accent color (`indigo-600`) for links/primary actions/active states. Chosen over a flatter Material-style language because it directly answers the user's own "iOS style elements" steer and pairs naturally with the "clean minimal" choice. Governs R3, R6.
- KTD3. **Status-color convention, one consistent mapping app-wide**: `ok` → green (emerald-500 dot/text), `needs_reconnect`/error → red (rose-600, alert banner), `degraded` → amber (amber-500). Used for connection-health dots in the navbar and Settings, and for `ConnectionHealthBanners`. Every dot also carries an accessible text alternative (`aria-label` or adjacent `sr-only` text naming the path and status) since color alone conveys nothing to a screen-reader user, and connection health gates the app's core approve/reject/move actions. Governs R2, R4.
- KTD4. **Test-preserving restyle discipline**: every element carrying a `data-testid`, `role="alert"`, or `role="status"` keeps that exact attribute; button text stays exactly `"Approve"`, `"Reject"`, `"Move"`, `"Check now"` etc. (no icon glyphs injected into the same text node as these labels, since `@testing-library`'s `getByText` in `web/tests/ReviewQueue.test.tsx` matches on an element's full text content and a sibling icon inside the same button would break that exact-text match). Icons, when used, go in their own `aria-hidden` element visually separate from the interactive label's text-bearing element, or are omitted entirely for the three test-asserted buttons. Governs R5.
- KTD5. **`App.tsx`'s manual page-state switch (`useState<Page>`) is kept unchanged** — only wrapped in a new shared `Layout` component for the navbar chrome. No React Router adoption (see Scope Boundaries) — out of proportion for a 3-page personal tool, and `web/tests/ReviewQueue.test.tsx` already renders `<ReviewQueue />` directly rather than through `<App />`, so this refactor has no test-coupling risk either way. Governs R2.

---

### Output Structure

New files this plan creates (all other files are edits to existing ones):

```text
web/
├── tailwind.config.ts       (new, U1)
├── postcss.config.cjs       (new, U1 — .cjs since package.json sets "type": "module")
├── src/
│   ├── index.css            (new, U1 — Tailwind directives + base styles)
│   └── components/
│       └── Layout.tsx       (new, U2 — navbar shell wrapping all pages)
```

---

### Implementation Units

### U1. Add and configure Tailwind CSS

**Goal:** Get Tailwind CSS building correctly in this Vite + React + TS project, in both dev and production builds, with no visual changes yet (infrastructure only).

**Requirements:** R1. KTD1.

**Dependencies:** None — this is the foundation every other unit builds on.

**Files:**
- `web/package.json` (add `tailwindcss@^3`, `postcss@^8`, `autoprefixer@^10` as devDependencies — pinned per KTD1, not an unqualified install)
- `web/tailwind.config.ts` (new)
- `web/postcss.config.cjs` (new — `.cjs` extension per KTD1, since `web/package.json` sets `"type": "module"`)
- `web/src/index.css` (new — `@tailwind base; @tailwind components; @tailwind utilities;` plus the system-font-stack base style from KTD2)
- `web/src/main.tsx` (import `./index.css`)

**Approach:**
1. Add `tailwindcss@^3`, `postcss@^8`, `autoprefixer@^10` to `devDependencies` (pinned major versions per KTD1); Vite auto-detects `postcss.config.cjs` with no separate Vite plugin needed.
2. `tailwind.config.ts` `content` globs `./index.html` and `./src/**/*.{ts,tsx}` so class names in every page/component are scanned.
3. Extend the Tailwind theme with the accent color and font stack from KTD2 rather than hardcoding raw color/font values in every component, so later palette tweaks are a one-line config change.
4. `index.css` imports the three Tailwind layers and sets the system font stack + base background/text color on `body`.

**Test scenarios:**
- Test expectation: none — pure build tooling, no runtime behavior to unit test.
- Manual verification: `npm run build` completes with no PostCSS/Tailwind errors, and a Tailwind utility class (e.g. `bg-red-500`) applied to any element visibly renders in `npm run dev`.

**Verification:** `npm run build` and `npm run dev` both succeed; a throwaway Tailwind class renders visually; existing `npm test` suite still passes unchanged (no source files besides `main.tsx`'s new CSS import are touched).

---

### U2. Shared layout shell (top navbar)

**Goal:** Replace `App.tsx`'s bare `<nav>` of unstyled buttons with a styled top-navbar `Layout` component: app name, active-state-aware links for the three pages, and connection-health indicator dots.

**Requirements:** R2, R6. KTD2, KTD3, KTD5.

**Dependencies:** U1.

**Files:**
- `web/src/components/Layout.tsx` (new)
- `web/src/App.tsx` (modify — render pages through `Layout`, keep the existing `useState<Page>` switch)

**Approach:**
1. `Layout` takes the current page, a page-change callback, and children (the active page's content) as props — `App.tsx` keeps owning the `Page` state exactly as today (KTD5), just delegating the chrome to `Layout`.
2. Navbar: app name/logo on the left, a pill group of the 3 page links in the center or right with a static background/border marking the active link (per KTD2) — an animated sliding indicator is optional polish, not required; a plain static active-state style fully satisfies KTD2 for a fixed 3-item nav — plus connection-health dots on the far right. The dots are a lightweight, always-visible summary covering all three KTD3 states (green/amber/red, not just green/red) with an accessible text alternative per KTD3's accessibility note. Full reconnect detail stays in `ConnectionHealthBanners` on the pages that already render it; do not duplicate banner text in the navbar.
3. Fetching `AuthStatus` for the navbar dots: `Layout` (or `App.tsx`) makes its **own independent** `fetchAuthStatus()` call to drive the dots — do **not** lift or replace the fetch each page already makes for its own `ConnectionHealthBanners`/status display. `web/tests/ReviewQueue.test.tsx` renders `<ReviewQueue />` standalone and relies on `ReviewQueue`'s own `fetchAuthStatus()` call to populate its banners in the test; removing that call to "share" a lifted fetch would leave the banners unpopulated in that test, breaking R5. `Settings.tsx` additionally issues its own extra `fetchAuthStatus()` refetch after a `youtube_connect=success` OAuth redirect — the navbar's separate fetch must not interfere with or replace that either. Accept the small duplicate-request cost as the price of leaving existing data flow untouched (out of scope for a styling-only pass).
4. Page content area gets consistent max-width, centering, and padding so all three pages share one content rhythm.

**Patterns to follow:** `web/src/components/ConnectionHealthBanners.tsx`'s existing `AuthStatus`-driven conditional rendering for the two-banner independence rule (KTD17 in the product plan) — the navbar dots must summarize both `write_path` and `detection_path` without blending them into one indicator (e.g., two separate dots, not one merged dot).

**Test scenarios:**
- Test expectation: none new for `Layout` itself (no dedicated test file) — it's a presentational wrapper with no business logic; existing `ReviewQueue.test.tsx` renders `<ReviewQueue />` directly (not through `App`/`Layout`), so this unit needs no test changes there, **provided** `ReviewQueue`'s own `fetchAuthStatus()` call is left in place per Approach step 3.
- Manual verification: clicking each of the 3 nav links switches pages exactly as before; the active page's link is visually distinguished; connection-health dots reflect `needs_reconnect` AND `degraded` states correctly (not just `needs_reconnect`) when manually forced, without merging write/detection status into one dot; each dot's accessible text alternative is present (inspect via devtools accessibility tree).

**Verification:** All 3 pages still reachable via nav exactly as before; navbar renders app name, 3 links with an active-state indicator, and 2 independent status dots; `npm test` still passes.

---

### U3. Restyle shared components (`ConnectionHealthBanners`, `BpmAttribution`)

**Goal:** Give the two shared components the new visual language before the pages that use them are redesigned, so U4–U6 build on an already-restyled foundation rather than restyling these twice.

**Requirements:** R4, R6. KTD2, KTD3, KTD4.

**Dependencies:** U1.

**Files:**
- `web/src/components/ConnectionHealthBanners.tsx` (modify)
- `web/src/components/BpmAttribution.tsx` (modify)

**Approach:**
1. `ConnectionHealthBanners`: style each banner as a rounded, colored alert box (amber/rose background per KTD3, per KTD2's `rounded-2xl` treatment) with a small icon — keep `role="alert"` and `data-testid="write-path-banner"` / `data-testid="detection-path-banner"` exactly as-is (KTD4), and keep the two banners as visually and structurally separate elements (never merged into one combined banner, preserving the product plan's KTD17 independent-signals rule at the UI layer).
2. `BpmAttribution`: style as a small, subtle badge/pill (per KTD2) with the GetSongBPM link — keep `data-testid="bpm-attribution"` and the link's exact text/href unchanged (KTD9's attribution requirement in the product plan is about content, not styling, so the credit text must not be reworded).

**Test scenarios:**
- Test expectation: none new — no dedicated test file for either component; both are exercised indirectly through `ReviewQueue.test.tsx`'s existing banner/attribution assertions (R5), which this unit must keep passing unchanged.
- Manual verification: forcing each `AuthStatus` field to `needs_reconnect` in turn shows the correct single banner styled per KTD3; a `bpm_source: "measured"` item shows the styled attribution badge.

**Verification:** `npm test` passes unchanged (`ReviewQueue.test.tsx`'s banner and attribution assertions are the direct proof); both components visually match the new language in the browser.

---

### U4. Redesign Review Queue page

**Goal:** Replace the bare grouped `<ul>`/`<li>` list with a card-based layout: one card per playlist group, one row/card per song with title, artist, confidence, explanation, and styled action buttons.

**Requirements:** R3, R5, R6. KTD2, KTD3, KTD4.

**Dependencies:** U1, U2, U3.

**Files:**
- `web/src/pages/ReviewQueue.tsx` (modify)
- `web/tests/ReviewQueue.test.tsx` (no changes expected — existing suite is the acceptance check for this unit)

**Approach:**
1. Each playlist group (`groupByPlaylist`'s output) becomes a `rounded-2xl` card (KTD2) with the playlist heading, containing its song rows. Song rows themselves use a flatter treatment inside the group card (a divider line or subtle background alternation) rather than their own nested `rounded-2xl`/shadowed card — this is a triage list meant for fast repeated approve/reject/move, not a stack of heavy nested cards.
2. Each song row keeps its existing `data-testid={`queue-item-${item.id}`}` (KTD4) and shows title/artist prominently, a confidence value as a small colored badge using explicit thresholds (`>= 0.8` emerald/green, `0.5–0.79` amber, `< 0.5` rose; `confidence: null` renders a neutral gray "—" badge) — implement the color mapping as a lookup returning complete literal Tailwind class strings per bucket (e.g. `"bg-emerald-100 text-emerald-700"`), never string-interpolated class names, since Tailwind's build-time class scanner cannot detect a dynamically-constructed class name and would silently drop the color in production. Also show the explanation as muted secondary text, and the BPM attribution badge (U3) inline where applicable.
3. Approve/Reject/Move buttons: styled as small pill buttons (KTD2) — green-tinted Approve, neutral/rose-tinted Reject, neutral Move — but per KTD4, their visible text stays exactly `"Approve"` / `"Reject"` / `"Move"` with no icon sharing the same text node, since `ReviewQueue.test.tsx` selects them via `screen.getByText("Approve")` etc. Move keeps its existing `window.prompt("Move to playlist id:")` mechanic unchanged for this pass (a styled playlist picker is a UI-mechanism change, out of scope for a styling-only plan — see Deferred to Follow-Up Work) — the native dialog's visual clash with the rest of the page is an accepted, explicitly-scoped tradeoff, not an oversight.
4. A brief loading state (e.g. a simple centered spinner or skeleton) renders while the initial `fetchReviewQueue()`/`fetchAuthStatus()` call is in flight, so the styled empty-state message never flashes before the real data arrives. Empty state (`items.length === 0 && !error`) and error state (`role="alert"` for the fetch error) get simple centered, styled treatments instead of bare `<p>` tags — the `&& !error` guard keeps the two states mutually exclusive, since today both conditions can independently be true on a fetch failure (`items` stays `[]` while `error` is also set) and rendering both together would show contradictory messages once each gets attention-grabbing styling. Keep the error `<p role="alert">` role intact.

**Patterns to follow:** `web/src/components/ConnectionHealthBanners.tsx` (U3) for banner placement at the top of the page; KTD3's status-color convention for the confidence badge.

**Test scenarios:**
- Covers all of `web/tests/ReviewQueue.test.tsx`'s existing 9 scenarios unchanged (title/artist display, approve/reject/move removing the item, banner independence, BPM attribution) — no new test scenarios are needed for this unit since it is a pure restyle; the existing suite is the regression net.
- Manual verification: visually confirm card grouping, confidence badges (including the null-confidence and each color-bucket case) render with correct colors in a production build (`npm run build` + preview, not just `npm run dev`, to catch any Tailwind class-purging regression), the loading state appears briefly before data loads, and empty/error states never render simultaneously.

**Verification:** `npm test` passes unchanged (all `ReviewQueue.test.tsx` cases green); page visually matches the new card-based language in the browser against real backend data.

---

### U5. Redesign Settings page

**Goal:** Restyle the connection-health list, the "Connect Google account" affordance, the check-for-new-songs action, and the create-playlist-by-description form into the new visual language.

**Requirements:** R3, R6. KTD2, KTD3.

**Dependencies:** U1, U2, U3.

**Files:**
- `web/src/pages/Settings.tsx` (modify)

**Approach:**
1. Connection health: replace the bare `<ul>` of status text with a card containing a row per status (write path, detection path, last ingestion check, 3 LLM signals, Last.fm, GetSongBPM), each row showing a colored status dot (KTD3, including the accessible text alternative and the `degraded`/amber case) plus label.
2. "Connect Google account" link: restyle as a proper pill-shaped primary button (KTD2) — keep it as an `<a>` tag with the same `href="/api/v1/auth/youtube/authorize"` (the existing comment in `Settings.tsx` explains why this must stay a real navigation, not a `fetch` call — do not change that mechanic).
3. "Check now" button and the create-playlist form: styled button and form inputs (rounded borders, focus states) consistent with KTD2; the status `message` (`role="status"`, per existing `<p role="status">`) gets a styled persistent banner treatment — it stays visible until replaced by the next action exactly as today, not an auto-dismissing "toast" (that would be a new behavior, out of scope here) — keeping the role attribute.
4. Section grouping: each of the three sections (Connection health / Check for new songs / Create a playlist) becomes its own `rounded-2xl` card (KTD2) for visual separation.

**Test scenarios:**
- Test expectation: none — `Settings.tsx` has no dedicated test file today; this unit is a pure restyle with no behavioral change to verify beyond what manual verification covers.
- Manual verification: connection-health rows show correct dot colors (including `degraded`/amber) for all `AuthStatus` states, each with its accessible text alternative present; clicking "Connect Google account" still navigates to the OAuth authorize route; "Check now" and the create-playlist form still submit and show their existing status messages; after a real Google OAuth connect/reconnect round trip, confirm the `youtube_connect` result message still displays correctly and the query param is still stripped from the URL (this restyle touches the same effect block that drives that message).

**Verification:** Manual browser check confirms all existing Settings behaviors (OAuth connect link, check-now, create-playlist form, status messages) work unchanged with the new styling; `npm test` unaffected (no Settings tests exist to regress).

---

### U6. Redesign Onboarding page

**Goal:** Restyle the existing-playlists list, AI-suggested-playlist checkboxes, custom-playlist form, and finish button into the new visual language.

**Requirements:** R3, R6. KTD2, KTD3.

**Dependencies:** U1, U2, U3.

**Files:**
- `web/src/pages/Onboarding.tsx` (modify)

**Approach:**
1. "Your existing playlists" and "Suggested new playlists" become `rounded-2xl` cards (KTD2); each suggested-playlist checkbox row is restyled as a selectable card/row (checked state visually distinct via the accent color) rather than a bare `<input type="checkbox">` + label line.
2. Custom-playlist add form: styled inputs/textarea and an "Add" button consistent with KTD2; added custom playlists render as small pill/tag chips rather than a bare `<ul>`.
3. "Finish setup" button: styled as the page's primary pill-shaped action (KTD2), visually distinguished from the "Add" button as the primary vs. secondary action.
4. The post-finish `role="status"` message keeps its role and text, restyled as a centered confirmation card.
5. A brief loading state (spinner or skeleton) renders while the initial `fetchOnboardingAnalysis()` call is in flight, so the styled "No new-playlist suggestions found." empty-state copy never flashes before the real proposals arrive.

**Test scenarios:**
- Test expectation: none — `Onboarding.tsx` has no dedicated test file today; pure restyle with no behavioral change beyond what manual verification covers.
- Manual verification: toggling proposal checkboxes still updates the `accepted` set correctly (visually confirmed via the selected-state styling); adding a custom playlist still appends it to the list; "Finish setup" still calls `submitOnboardingSelection` and shows the completion message.

**Verification:** Manual browser check confirms the full onboarding flow (view proposals, accept some, add a custom one, finish) still works end-to-end with the new styling; `npm test` unaffected (no Onboarding tests exist to regress).

---

## Verification Contract

- `npm test` (Vitest) passes with zero changes required to `web/tests/ReviewQueue.test.tsx` — this is the primary automated proof that the restyle preserved behavior, `data-testid`s, ARIA roles, and exact button/text content (R5).
- `npm run build` succeeds (TypeScript + Vite production build), proving the Tailwind integration doesn't break the build (R1).
- Manual browser walkthrough of all three pages against the real running backend (`docker compose up`), confirming: nav works, connection-health dots/banners reflect real auth status, review-queue approve/reject/move still perform real actions, Settings' OAuth connect / check-now / create-playlist still work, Onboarding's full flow still completes.

## Definition of Done

- Tailwind CSS is installed, configured, and used across all restyled files (R1).
- Shared `Layout` component renders the top navbar with working nav and independent connection-health indicators (R2).
- Review Queue, Settings, and Onboarding are visually redesigned per the settled clean-minimal-light + iOS-influenced language (R3, R6).
- `ConnectionHealthBanners` and `BpmAttribution` are restyled without changing their render conditions (R4).
- `npm test` passes unmodified; all existing `data-testid`/role/text hooks are intact (R5).
- `npm run build` succeeds.
