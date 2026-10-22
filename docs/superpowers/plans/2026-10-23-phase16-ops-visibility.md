# Phase 16 운영 가시성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 웹·텔레그램·STATUS 에 종목명을 보이고, Claude 구독 사용량(5시간·7일)과 데몬 잡 상태를 보여 주며, AI 파이프라인 '작동 방식' 페이지를 둔다.

**Architecture:** 이름은 원장 `ticker_names` 테이블 하나에 모으고(잔고·00_inputs 관측 시 upsert, 캐시 JSON 폴백, 네트워크 없음) `names.label` 이 유일한 표기 함수다. 사용량은 대화형 Claude Code 상태줄이 남긴 `~/.claude/rate_limits.json`(관측값) + `agent_calls` 창 집계. 데몬은 `heartbeat.py` 가 쓰는 `state/daemon_heartbeat.json` + `daemon.lock` pid 생존으로 판정하고 `/api/system/daemon` 이 읽는다.

**Tech Stack:** Python 3.12 / FastAPI / SQLite / APScheduler, React 19 + Vite + Vitest.

**Spec:** `/Users/ohhyeonseok/.claude/plans/clever-pondering-ritchie.md`(사용자 승인 플랜), 스펙 §8 웹 UI·§10.1 데몬.

## Global Constraints
- `ledger/db.MIGRATIONS` 는 버전 추가만(v10). pydantic `extra="forbid"`.
- 네트워크 호출로 이름을 조회하지 않는다(캐시 JSON 은 `fetched_at` 무시하고 읽는다).
- 기존 API 응답 형태 불변, 새 엔드포인트는 조회 전용(토큰 없음).
- 색은 `tokens.css` 변수만, 새 UI 는 `DESIGN.md` 표 문법(`Table.tsx`)·`Section`·`Term` 을 쓴다.
- 사용자 대면 문구 한국어, 로거 `trader.<계층>`.

## Review Focus
1. 캐시 JSON 이 없거나 깨졌을 때 `/api/names` 가 500 이 아니라 빈 매핑을 주는가 → Task 1 테스트.
2. `daemon.lock` 의 pid 가 재사용된 다른 프로세스일 때 "실행 중" 오탐 → 하트비트 `updated_at` 경과 `stale` 플래그를 함께 보여 준다(Task 3 테스트: lock 살아 있음 + 하트비트 2시간 전 → `stale: true`).
3. `rate_limits.json` 의 `resets_at` 이 없거나 형식이 다를 때 → None 허용(Task 2 테스트).
4. interval 잡(monitor)이 60초마다 하트비트 파일을 쓸 때 쓰기 실패(OSError)가 잡을 죽이지 않는가 → Task 3 테스트.
5. 이름이 ticker 와 같거나 빈 문자열일 때 원장에 쓰지 않는가 → Task 1 테스트.

---

### Task 1: 종목명 — 원장 테이블·해석기·표기·API·프런트 `<Ticker>`

**Files:** Create `src/trader/names.py`, `tests/test_names.py`, `web/src/names.ts`, `web/src/names.test.ts`, `web/src/components/Ticker.tsx`. Modify `src/trader/ledger/db.py`(v10), `ledger/repo.py`, `broker/models.py`, `broker/accounts.py`, `orchestrator/executor.py`, `orchestrator/tools.py`, `orchestrator/monitor.py`, `daemon.py`, `notify/commands.py`, `status.py`, `web/routers/system.py`, `web/src/hooks.ts`, `web/src/components/Tables.tsx`, `web/src/pages/*.tsx`.

**Interfaces:**
- `repo.upsert_ticker_name(conn, market, ticker, name, source) -> bool`, `repo.ticker_names(conn, market=None) -> dict[tuple[str,str], str]`.
- `names.load_cache_names(cache_dir) -> dict[tuple[str,str], str]`, `names.resolve(conn, cache_dir, market, ticker) -> str | None`, `names.label(ticker, name) -> str`, `names.display(conn, cache_dir, market, ticker) -> str`, `names.default_cache_dir() -> Path`.
- `Position.name: str | None`.
- `GET /api/names?market=&tickers=` → `{"KR:005930": "삼성전자"}`.
- 프런트: `nameKey(market, ticker)`, `AppState.names`, `AppState.ensureNames(market, tickers)`, `<Ticker market ticker />`.

- [ ] 테스트 `tests/test_names.py`: upsert 무시 규칙(빈 이름·ticker 동일), 캐시 파일 로드(만료 `fetched_at` 도 읽음, 깨진 파일은 빈 dict), `resolve` 가 원장 → 캐시 순이며 캐시 적중을 `source="cache"` 로 적재, `label("005930","삼성전자") == "삼성전자 005930"`, `label("005930", None) == "005930"`.
- [ ] 구현 `names.py` + repo + v10.
- [ ] `tests/broker`: 잔고 픽스처로 `Position.name` 이 "삼성전자"/"애플".
- [ ] `executor.build_portfolio` 가 `Position.name` 을 upsert(source `balance`), `tools.submit_decision` 이 `00_inputs.meta.name` 을 upsert(source `inputs`) — 기존 executor/tools 테스트에 단언 추가.
- [ ] 알림·STATUS 문구를 `names.display`/`label` 로(`commands.approval_line` 은 `order.get("name")` 이 있으면 우선 — 승인 요청 order 에 `name` 을 함께 넣는다). `tests/test_status.py`·notify 테스트에서 "삼성전자 005930" 확인.
- [ ] `GET /api/names` + `tests/web/test_names_api.py`(원장 행, 캐시 폴백, 모르는 종목은 키 없음).
- [ ] 프런트 `names.ts` 테스트 → `hooks.ts` names/ensureNames → `Ticker.tsx` → 코드만 쓰던 자리 교체 → `components.test.tsx` 스모크.
- [ ] Commit `feat(names): 종목명 원장·해석기·표기·API·프런트 Ticker`.

### Task 2: Claude 사용량 — 관측 파일 + 원장 창 집계 + 대시보드 블록

**Files:** Create `src/trader/usage.py`, `tests/test_usage.py`, `web/src/usage.ts`, `web/src/usage.test.ts`. Modify `config.py`(`LlmSettings.usage_file`), `config/settings.yaml`, `templates/config/settings.yaml`, `web/routers/system.py`, `web/src/pages/Dashboard.tsx`, `web/src/types.ts`. 사용자 파일 `~/.claude/statusline-command.sh` 1줄.

**Interfaces:** `usage.read_rate_limits(path, now) -> dict | None`, `usage.ledger_windows(conn, now, cap) -> dict`, `GET /api/system/usage`.

- [ ] 테스트: 파일 없음 → None; `{at, rate_limits:{five_hour:{used_percentage:12, resets_at:…}}}` → `five_hour.used_pct == 12`, `age_s`; `resets_at` 없음 → None; `ledger_windows` 가 fake 행·5시간 밖 행을 제외.
- [ ] 구현 + API + 설정 키.
- [ ] 상태줄 스크립트에 관측 파일 쓰기 1줄(jq, `.tmp` → `mv -f`).
- [ ] 프런트 `usage.ts`(경과 문구·막대 비율) 테스트 → Dashboard "Claude 사용량" 블록 + 상단 상태줄 요약.
- [ ] Commit `feat(usage): Claude 구독 사용량 관측 파일 + 원장 창 집계`.

### Task 3: 데몬 하트비트 + `/api/system/daemon` + 잡 표

**Files:** Create `src/trader/heartbeat.py`, `tests/test_heartbeat.py`, `web/src/jobs.ts`, `web/src/jobs.test.ts`. Modify `daemon.py`(`run_job(heartbeat=)`, `run()` 리스너), `web/app.py`(`WebDeps.schedule`), `web/routers/system.py`, `cli.py`(web deps 에 schedule), `notify/commands.py`(`/status` 한 줄), `tests/test_daemon.py`(FakeScheduler `add_listener`·`get_job`), `web/src/pages/Dashboard.tsx`.

**Interfaces:** `Heartbeat(path).start(pid, started_at, jobs)`, `.job_started(id, at)`, `.job_finished(id, at, status, error=None)`, `.job_missed(id, at)`, `.set_next(id, next_at)`, `.touch(at)`, `.stop(at)`; `heartbeat.read_status(state_dir, now, schedule_jobs) -> dict{alive, pid, started_at, updated_at, age_s, stale, stopped_at, jobs:[{id, kind, market, cron, interval_s, last_start, last_end, last_status, last_error, next_run}]}`; `GET /api/system/daemon`.

- [ ] 테스트: 파일 왕복, 쓰기 실패 무시, `read_status` 의 alive/stale/stopped 분기, 데몬이 없을 때도 schedule 잡 목록이 나옴.
- [ ] 구현 + `run_job` 연결 + 리스너 + API + `/status` 한 줄.
- [ ] 프런트 `jobs.ts` 테스트(라벨·경과·상태 분류) → Dashboard 상태줄·잡 표(60초 재조회).
- [ ] Commit `feat(daemon): 하트비트 파일·/api/system/daemon·잡 표`.

### Task 4: '작동 방식' 페이지

**Files:** Create `web/src/how.ts`, `web/src/how.test.ts`, `web/src/pages/HowItWorks.tsx`. Modify `web/src/App.tsx`, `components/Shell.tsx`, `pages/Today.tsx`, `styles/components.css`.

- [ ] 테스트: 모든 `StageKey` 가 어느 절의 `stages` 에 들어 있다; 절 id 가 고유.
- [ ] 구현(설정·스케줄은 API 에서 읽어 숫자 표시) + 라우트 `/how` + 내비 + Today 단계 머리 링크.
- [ ] Commit `feat(web): 작동 방식 페이지`.

### Task 5: 문서·검증
- [ ] `docs/TODO.md` Phase 16 절, CLAUDE.md 규칙 3줄, `docs/bundle-usage.md` 사용량 안내.
- [ ] `uv run pytest` · `uv run ruff check src tests` · `cd web && npm run typecheck && npm test && npm run build` · `impeccable detect --json web/src`.
- [ ] `uv run trader web` 으로 Chrome 점검(대시보드·주문·오늘·/how, 390px) → `uv run trader status` → 커밋 → main ff 머지·push.
