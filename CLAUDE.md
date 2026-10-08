# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 프로젝트: LLM 멀티 에이전트 + ML 주식 자동매매 (KIS)

## 목적
한국투자증권(KIS) Open API 로 모의·실전 계좌를 운용하는 주식 자동매매 시스템. Claude Code 오케스트레이터 세션(`claude -p`)이 서브에이전트(screener, 애널리스트 4, 리서처 2, 트레이더, reflector)를 소집해 분석하고, **파이썬 CLI 도구만 통해** 수집·체크포인트·결정 제출을 한다. 리스크 한도와 주문 실행은 LLM 이 아니라 파이썬이 강제한다. macOS 개발·운영, Windows 동일 실행 보장. 목표는 모의 4주 이상 무사고 + 벤치마크 우위 확인 후 사용자가 실전 전환.

## 세션 시작 시 상태 확인
- `state/STATUS.md` 는 SessionStart 훅(`.claude/hooks/session_start.py`)이 세션 시작·`/clear`·compact 때 자동 주입한다 (STATUS.md + `docs/TODO.md` 미완료 상위 항목).
- 최신화는 `python -m trader status` (Phase 0 이후).
- 다음 할 일은 `docs/TODO.md` 의 첫 미완료(`- [ ]`) 항목. 작업 완료 시 체크 표시를 갱신한다. 각 Phase 마지막은 `완료 기준:` 항목이며 확인 시 `(YYYY-MM-DD 확인)` 을 덧붙인다. 이전 Phase 에서 미룬 항목은 재개 Phase 절에 `(Phase N 이연)` 접미사로 둔다.

## 문서
- 설계 스펙: `docs/superpowers/specs/2026-10-09-trading-agent-design.md`. 아키텍처·도구 계약·체크포인트 파일명·리스크 규칙·Phase 완료 기준의 원천이다. 새 모듈을 만들 때 해당 절을 먼저 읽는다. 절 색인: §0.1 요구사항 R1~R18, §0.2 확정 사항, §0.3 외부 제약(Claude CLI 플래그·KIS 한도·커뮤니티 소스 정책), §1 아키텍처(두 층·도구 4개·명령·디렉터리), §2 브로커, §3 수집(3.3 룩어헤드), §4 에이전트(4.1 오케스트레이터 세션, 4.5 체크포인트), §5 scan/monitor, §6 리스크·executor·원장·알림·메모리, §7 세션 복구, §8 웹 UI, §9 ML, §10 스케줄러·크로스플랫폼, §11 Phase 계획, §12 논문 반영표, §13 검증.
- 구현 플랜: `docs/superpowers/plans/<날짜>-phase<N>-<이름>.md`. 새 Phase 는 `superpowers:writing-plans` 로 플랜을 먼저 쓴다. 플랜의 `범위 조정` 표(제외 항목·이유·재개 Phase)는 TODO.md 이연 항목의 출처다.
- 이 파일은 Phase 완료 시점에 코드와 대조해 어긋난 곳만 고친다. 함수 시그니처·상수·기본값은 적지 않는다(코드가 출처). "왜 그렇게 정했는지" 와 "여러 파일에 걸친 규칙" 만 적는다.

## 실행 명령 (Phase 가 진행되며 늘어난다)
- `uv sync` : 의존성 설치 (Python 3.12)
- `uv run pytest` : 전체 테스트 (네트워크 없이 전부 돈다)
- `uv run ruff check src tests` : 린트, 경고 0건 유지
- `python -m trader status` : `state/STATUS.md` 재생성
- `uv run trader --help` : 등록된 명령 확인
- 브랜치는 Phase 단위 `feat/phase<N>-<이름>` 을 직전 Phase 브랜치 위에 쌓고, PR 도 직전 Phase 브랜치를 대상으로 연다 (Phase 0 은 `main`). `.gitattributes` 가 LF 를 강제한다.

## 아키텍처 핵심 규칙 (여러 파일에 걸친 계약)
- **LLM 은 도구만 쓴다.** 오케스트레이터 세션은 `--tools "Bash,Agent"` + `--allowedTools "Bash(python -m trader tool *)"` 로 띄우고, 외부 세계와는 `python -m trader tool <collect|checkpoint|submit-decision|status>` 로만 접촉한다. 주문은 `submit-decision` 안의 리스크 게이트를 통과한 수량만 나간다.
- **순서는 프롬프트가 아니라 도구 전제조건이 강제한다.** `submit-decision` 은 해당 종목의 `10_analyst_*` 체크포인트 2개 이상과 `25_research_summary` 가 파일로 있을 때만 받는다.
- **체크포인트가 상태의 원본이다.** `state/runs/<날짜>/<시장>/<종목>/<단계>.json` 에 `{schema_version, data, call}` 로 저장하고, 존재하는 단계는 건너뛴다. 읽기·쓰기는 `orchestrator/run_store` 의 함수로만 한다.
- **계좌 모드.** yaml 값이 기본, SQLite `control` 테이블 값이 우선. 모의는 `auto` 고정, 실전은 `off` 로 시작하며 `auto` 전환은 확인 문자열 없이는 거부된다. 결정은 종목당 1회, 리스크 판정·주문은 계좌마다.
- **시세·수급은 실전 env 클라이언트만, 주문·잔고는 계좌의 env.** 모의 서버는 시세류를 주지 않는다. `tr_ids.py` 가 tr_id 의 유일한 출처.
- **룩어헤드 방지.** 모든 수집 항목에 `received_at`, `asof` 이후 발행분 제외. 과거 `asof` 재수집 금지(백테스트는 피처 스토어로만).
- **`claude -p` 플래그는 설치된 `claude --help` 에 있는 것만 쓴다.** `--bare` 는 구독 인증을 끄므로 금지. cwd 는 `state/claude_cwd/`(빈 디렉터리), `--setting-sources ""` 로 훅·플러그인 유입을 막는다. `FAKE_CLAUDE=1` 이면 subprocess 없이 고정 응답.
- **오류 타입.** 설정 오류는 `ConfigError`, KIS 오류는 `KisApiError`. 키는 URL·로그·예외 메시지에 남기지 않는다(`httpx` 로거는 WARNING).

## 금지사항
- 실전 주문을 수동으로 실행하지 않는다 (`.claude/settings.json` 이 `python -m trader kis order *` 를 거부한다).
- `secrets/` 를 커밋하지 않는다. 읽지도 않는다 (settings.json 의 deny 규칙. Bash 로 읽는 명령도 거부되므로 다른 명령과 묶지 않는다). `config/accounts.yaml` 에는 실제 계좌번호가 있으므로 값을 출력·인용하지 않는다.
- 계좌 모드(control 테이블)를 임의로 변경하지 않는다.
- 셸 스크립트(.sh/.bat/.ps1)를 추가하지 않는다. 훅·설치·유틸은 전부 파이썬.
- `state/` 파일을 직접 편집하지 않는다 (STATUS.md 는 `python -m trader status` 로만 갱신).
- 커뮤니티·뉴스 수집기의 기본 on/off 는 스펙 §0.3 정책을 따른다. 사용자 결정 없이 off 소스를 켜지 않는다.

## 코딩 규칙
- 경로는 전부 `pathlib`, 하드코딩 절대경로 금지, 루트는 `platform/paths` 로만 얻는다(`TRADER_HOME` → 없으면 cwd 에서 위로 `pyproject.toml` 탐색). OS 분기는 `platform/` 만.
- 파일명에 `:` 금지 (타임스탬프는 `YYYYMMDD_HHMMSS`). 저장 시각은 UTC ISO8601, 표시만 KST/ET. 타임존은 `zoneinfo` + `tzdata`. 콘솔 출력 UTF-8 강제.
- subprocess 는 `shell=False, encoding="utf-8", errors="replace"` 고정 (cp949 의존 금지). SessionStart 훅은 표준 라이브러리만.
- SQLite 는 WAL 모드, 쓰기 타임아웃 30초, 스키마 버전 + 순서 마이그레이션. 패키지 관리는 `uv`. 의존성은 Windows 휠이 있는 라이브러리만.
- 금액은 `Decimal`. pydantic 모델은 `extra="forbid"` (yaml 키를 추가하면 모델도 같이 바꾼다).
- 사용자 대면 메시지·로그·docstring 은 한국어. 로거 이름은 `trader.<계층>`.
- TDD: 실패 테스트 먼저. 테스트는 `tmp_path` 만 쓰고 네트워크는 `respx` 로 막는다. Phase 마다 커밋하고 `docs/TODO.md` 를 갱신한다.
- 커밋 트레일러: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
