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
