# SaveTicker API backend implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement task by task with tests, then independent review.

**Goal:** Add an opt-in, AI-free backend that collects confirmed same-origin API contracts while Chrome is hidden, preserving data and access-stop behavior.

**Architecture:** Native issues exact GET request contracts tied to expiring lane jobs. A separate isolated content script performs bounded same-origin reads without DOM activation; worker serializes jobs, saves the existing Store format, and persists cooldowns. Unknown ranking/regular/detail contracts remain explicitly unsupported and are never replaced with fabricated DOM proof.

**Tech Stack:** MV3 Chrome extension, Native Messaging, Python Store/Pydantic, Node tests, isolated Chromium.

**Spec:** docs/superpowers/specs/2026-10-30-saveticker-minimized-api-design.md

## Global constraints
- No AI/provider calls, credentials export, debugger permission, security bypass, main trading runtime restart, or live orders.
- Existing 20 streams stay in plan; unsupported API contracts must be visible and must not save success/empty captures.
- Preserve existing DOM backend; API is explicit opt-in and may remain unverified.
- Stop all host work on auth/challenge; persist 429 cooldown; never shorten current intervals.

## Review focus
- Late replies after stop/restart must not save or download.
- Redirects, duplicate query parameters, wrong job, mismatched basis and oversized response must be rejected.
- API errors must not overwrite prior valid data or imply whole-history completeness.
- Minimized tabs must never trigger focus/activation changes in API mode.
- Unconfirmed personal groupId/regular requests must never be guessed.

### Task 1: Native request/snapshot contract
Files: create src/trader/saveticker/api_contract.py; modify native.py; create tests/saveticker/test_api_contract.py.
Interfaces: descriptor(view)->dict|None; parse_snapshot(view, descriptor, envelope)->list[Item]. Native api_begin/api_snapshot/api_cancel reuse lane jobs and Store, separate from DOM selection.
- [x] Write failing tests for supported/unsupported stream mapping and wrong URL/status/size/basis/job.
- [x] Implement exact news/report/calendar/index/top5 contracts and projected data parsing; preserve previous news body/report attachment.
- [x] Run focused Python checks (39 passed).

### Task 2: Isolated fetch and worker mode
Files: create extensions/saveticker/api-content.js and api-runner.js; modify manifest/worker/popup; add Node tests.
Interfaces: api-content accepts api_fetch with same-extension sender, owner token, exact descriptor; returns bounded {url,status,content_type,data,retry_after}. Runner uses Native-issued jobs, rejects stale epoch, persists host cooldown, and never focuses tabs.
- [ ] Write failing fetch/runner tests including hidden, redirect, 429, stop, unsupported stream and auth.
- [x] Implement separate script and opt-in popup mode while stopped; unknown jobs surface unverified status.
- [x] Run Node suite and real isolated Chromium hidden/content integration.

### Task 3: Delivery and review
- [x] Document supported contracts, missing full-scope/PDF acceptance, no-AI execution, and activation steps.
- [x] Fresh independent review; fix findings; SaveTicker suite 172 passed, Ruff passed.
- [x] Package updated files while stopped; retain rollback. Do not start live requests while managed challenge persists.
- [x] Update draft PR with exact acceptance evidence and blockers. Do not claim operational completion or merge.

## Execution ledger
- User explicitly requested implementation after design/revalidation. Inline execution selected to continue authorized work without another permission loop.
- Ruling: confirmed subset is opt-in only; unconfirmed popular/groupId, news detail, and PDF contracts remain unsupported rather than guessed. Cost: cannot claim full 20-stream API operation until site discovery is possible.
- Native red: missing API contract module; then green 35 tests. Review regression red: four malformed/empty cases; fixed date/range/pagination checks, green 39 tests.
- Worker red: production worker did not route API mode; then green. Real browser red found legacy content listener replying to API messages; type filtering fixed it, real minimized/hidden request + actual Native validation/Store test passed.
- Independent review found empty validation, redirect handling, version mismatch; fixed and re-review found no additional P1/P2.
- Production pacing is 2 seconds per API request and existing plan intervals remain unchanged. 429 deadline is persisted before job cleanup and honored by both modes.

- Final checks: Node 113 passed; SaveTicker pytest 172 passed in 78.34s; Ruff and diff check passed. Frozen bundle smoke/selfcheck passed. Frozen Native hello/api_begin/api_snapshot saved one synthetic item and reported 10/20 API contracts. No site requests were made during implementation or installation. Chrome reload/live acceptance pending.

### Task 4: Remaining-contract implementation and acceptance
- [x] Isolated browser restart: real schedule interval/token/backend persist; automatic collection resumes; persisted hostBlocked suppresses requests. Full SaveTicker suite173 passed.
- [x] Normal UI confirms default group initialization, all live ranking query forms, news detail id correlation, report.pdf_url and PDF download request. Do not store personal group names or credential headers.
- [x] Extract API-only job state to api_jobs.py; initialize only unique default groupId from projected groups response.
- [x] Add Native-issued enrichment requests (max2 news details and max1 report detail), bind exact URLs/source IDs/base snapshot hash, and merge only correlated bodies.
- [x] Add read-only popular contracts. Regular equivalence is permitted only with response session=open and basis=live under the exact observed UI rule; other regular conditions remain explicitly unverified.
- [x] Add API PDF fetch from Native-verified report.pdf_url; bounded same-origin PDF blob handoff reuses returned download ID, issued filename and existing Native ticket/PDF validation. No new permissions or MAIN hooks.
- [x] Red/green protocol, ownership, stop/late-response, malformed/cross-ID, auth/429, download/PDF tests; real isolated hidden pipeline and independent review.
- [ ] Build/install while stopped, user manual reload only, then actual account acceptance with normal Chrome UI; genuine long-running and market-closed regular acceptance remain pending until demonstrated.

Ruling: per-task user approval is not repeated; the user explicitly authorized implementation and the remaining validation. Normal Chrome via native UI is used for observation without browser-driver attachment. DevTools Network is metadata/collection JSON only; authentication headers are not copied.

- 1.2.0 review found no P1/P2. Follow-up guard prevents stopped/old epochs from overwriting block diagnostics. Red test reproduced Native ping falsely reporting collecting while hostBlocked; block ticks now send waiting_verification instead.
- Isolated validation: Node120 passed. Hidden API/restart/real download + real Native attachment tests3 passed in60.04s after unlocking. Full suite while OS locked had182 pass/2 visibility failures; these are retained as failed evidence, not counted as hidden passes. Unlocked full suite184 passed in141.33s.
- Real 1.1.0 no-driver/no-DevTools minimized run resumed03:14:28 KST; last news03:20:23 and calendar03:20:28. Native UI subsequently confirmed re-verification screen and hostBlocked. Operator collector explicitly stopped; no repeated refresh or automatic resume. This is a failed sustained-operation acceptance, independent of synthetic success.
- Frozen Native synthetic proof supports20 contracts after projected default-group setup; correlated news body, open-session regular rank, PDF ticket and1-page attachment passed. Bundle smoke/selfcheck passed. Final package refresh pending after diagnostic guard change.

- Final delivery:184 Python/120 Node passed; final macOS build/smoke and frozen API proof passed. Installed1.2.0 extension and dedicated Native app while stopped, preserving extension-before-1.2.0 and Trader-before-1.2.0.app. Native UI one normal refresh recovered authenticated report list. Manual Chrome reload pending; actual1.2.0 acceptance not yet started.

### Task 5: Observed explicit regular basis
- [x] Separate normal Chrome UI confirms all5 regularpopular query forms and server basis=regular; no credential/header extraction, no driver on collection tab.
- [x] Red:5 Native URL expectations and2 Node allowlist/connection regressions. Green focused28 Python.
- [x] Add exact basis=regular descriptor/query allowlist; validate exact requested responsebasis. Historical session=open does not establish equivalence; reject live responses for regular and regular responses for live.
- [ ] Full checks/review/package1.2.1, preserve1.2.0 rollback, user reload, actual20 streams under minimized collection.

1.2.0 actual post-resume proof: dedicatedwindow minimized around11:30:35KST. All15 nonconditional streams refreshed including zero calendars. Native validated a secondPDF30pages/6118121bytes/12783textcharacters while minimized; popup reported saved. Earlier28page PDF was also present but its delayed reply had surfaced unconfirmed; do not infer the cause of the long execution gap. Latest CIac13a9d passed6m38s. Read-only10minute minimized observer is running; do not claim long-running acceptance yet.

- ReviewP2 fixed: explicit regular requests now require server basis=regular, removing live/open fallback because session=open also appears on historical closed-market snapshots. Red regression reproduced incorrect acceptance before the fix.
