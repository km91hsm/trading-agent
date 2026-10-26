# 전체 검토 — 2026-10-26 (main + PR #26 기준)

검토 방식: 같은 스냅샷(`a5ca4ec`)을 **Claude 리뷰어 4명**(주문 안전·프로세스/성능·웹/텔레그램·LLM 투자 방식)과 **Codex CLI 2회**(백엔드 / 사용자 경험·투자 방식)가 서로 결과를 모른 채 읽기 전용으로 검토했다. 교차검증 열: **양쪽** = 두 모델이 독립적으로 같은 결함을 지적(신뢰 높음), **C** = Claude 만, **X** = Codex 만. 핵심 P0·P1 은 코드를 직접 따라가 다시 확인했다(✔).

## 1. 주문 안전·정확성 (주문 경로 — 고치면 머지 전 확인 필요)

| # | 심각도 | 교차 | 위치 | 결함 | 고칠 방향 |
|---|---|---|---|---|---|
| O1 | **P0(추정)** | 양쪽 ✔ | `broker/accounts.py:182` | US 잔고의 `Cash.nav` 가 `output3.tot_asst_amt`(원화 환산 총자산)를 USD 로 사용. 저장소 fixture 도 1,725,000(=1,250.5×1,380.5). 비중·현금 유보·섹터/테마 상한·TRIM 수량·헤지·서킷브레이커가 모두 왜곡 — US 매수 1건이 가능금액 전부를 씀 | 환율(`frst_bltn_exrt`)로 나누거나 외화 예수금 + 평가 합으로. 실기 응답으로 통화 확인 |
| O2 | P1 | 양쪽 | `orchestrator/reprice.py:131` | 재호가 후보에 손절(55_)·헤지(56_) 주문이 섞여, 취소 뒤 "최신 결정 + 일반 게이트"로 재제출 → 손절이 사라지거나 bear 에서 헤지 거부 | `50_`/`51_` 주문만 후보, 헤지는 `is_hedge`, 손절 제외 |
| O3 | P1 | 양쪽 | `ledger/repo.py:365` | 체결된(filled) 아침 TRIM 이 같은 결정의 오후 재평가 SELL 을 "이미 주문 있음"으로 막음 | 중복 판정에서 filled 제외 또는 라운드(주문 stage) 단위 |
| O4 | P1 | 양쪽 | `broker/kis_client.py:146` | 주문 POST 의 5xx·비JSON 응답을 `failed`(죽은 주문)로 → 접수됐을 수 있는 주문을 중복 제출 | POST 한정으로 `NETWORK_UNKNOWN`(unknown) 처리 |
| O5 | P1 | 양쪽 | `orchestrator/monitor.py:519` | 일시정지·off 계좌에서도 손절 직전 미체결 SELL 을 KIS 에서 실제 취소 → 보호 매도만 사라짐 | 취소 전에 paused·effective_mode 확인 |
| O6 | P1 | 양쪽 | `scalp/engine.py:278-314` | 단타 청산이 주문 결과와 무관하게 포지션을 닫고, 접수를 체결로 봄, 진입하지 않은 계좌까지 전량 매도 | 계좌별 체결 수량 기준, 미체결 BUY 취소 후 청산 (기본 꺼짐) |
| O7 | P1 | X | `scalp/engine.py:247` | bear·전략가 금지가 기존 단타 청산까지 막음 | 청산 먼저, 신규 진입에만 제한 |
| O8 | P1 | X | `orchestrator/executor.py:556` | 중복 확인 ↔ 주문 사이 원자적 예약 없음 → 두 프로세스가 동시에 같은 주문 | 실행 키 선점(UNIQUE) 후 주문 |
| O9 | P1 | X | `orchestrator/tools.py:757` | `40_risk_check` 만 있고 주문 전에 죽으면 재개가 "실행 완료"로 보고 주문을 영구 생략 | 주문·승인 stage 또는 명시적 완료 표식 기준 |
| O10 | P1 | X | `risk/rules.py:265` | 미체결 매수·승인 대기가 종목 수·섹터·테마 한도에 안 잡힘 | 예상 포트폴리오(살아 있는 BUY + pending)로 판정 |
| O11 | P2 | C | `executor.py:874` | 승인 사이 재평가가 결정을 SELL/HOLD 로 바꿔도 BUY 승인이 실행됨 | 승인 시 결정 action 재확인, 31_ 제출 때 같은 결정의 pending 만료 |
| O12 | P2 | X | `executor.py:895` | 승인 실행 때 게이트 재판정 없음(손실 중단·bear 전환 무시) | 승인 직전 최소 재판정(국면·일일손실) |
| O13 | P2 | C | `monitor.py` / `tools.py` | 손절·청산 SELL 때 같은 종목 당일 미체결 BUY 를 취소하지 않음 → 손절 후 재매수 | 청산 전 live BUY 취소 |
| O14 | P2 | C | `hedge.py:195` | −2x 레버리지 조건 `count ≥ 3` 에 전략가·수급 신호가 섞임 | 정량 3지표 count 로만 |
| O15 | P2 | X | `monitor.py:529` | off·dry-run 손절 기록 플래그가 같은 날 auto 전환 뒤 실제 손절을 막음 | 기록 억제와 주문 억제 분리 |
| O16 | P1 | X | `agents/schemas.py:79` | 음수·0 손절가가 스키마를 통과 → 기본 −15% 손절까지 무력화 | 가격 필드 양수 검증 |
| O17 | P3 | C | `repo.py`·`daemon.py` | 텔레그램 미설정이면 만료 승인이 pending 으로 남아 가능금액 예약·중복 방지 지속 | 쿼리에 `expires_at > now` |

## 2. 프로세스·성능·신뢰성

| # | 심각도 | 교차 | 위치 | 결함 | 고칠 방향 |
|---|---|---|---|---|---|
| P1 | P1 | C | `daemon.py:1084`, `resume.py`, `reflect.py` | 재평가·복기 세션에 `cancel` 미전달 → 종료 시 고아 claude 세션이 주문을 낼 수 있음 | `cancel=stop_event` 전달, 기동 시 남은 프로세스 그룹 정리 |
| P2 | P1 | C | `daemon.py` 대기열 | 재평가(우선순위 10)가 백필·scan_analyze 뒤로 수십 분~시간 밀림 | 재평가 전용 레인, LIGHT_KINDS 는 heavy 락 밖 |
| P3 | P1 | C | `sources/news_store.py:286` | `novel_items` 가 쓰기 잠금을 잡은 채 O(N×M) 유사도 루프 → 손절 기록·submit 이 busy timeout | 메모리에서 판정 후 짧은 커밋, `(market, received_at)` 인덱스 |
| P4 | P1 | X | `daemon.py:118` | 죽은 잠금 인수 경쟁으로 데몬 2개 동시 실행 가능 | OS 파일 잠금(수명 동안 유지) |
| P5 | P2 | X | `jobs.py:210` | 동시 enqueue 가 중복 검사를 함께 통과 | 부분 UNIQUE 인덱스 또는 BEGIN IMMEDIATE |
| P6 | P2 | 양쪽 | `usage.py` | 감속이 창별 최신 관측·리셋 시각을 무시 → 최대 6시간 불필요한 차단 / 우회 | (fix25 에서 수정 중) |
| P7 | P2 | C | `sources/inputs.py:573` | 수집 절 타임아웃 180초가 실제로 함수를 끊지 못함(`shutdown(wait=True)`) | `cancel_futures`, 절별 기한 |
| P8 | P2 | C | `daemon.py:1838` | `heavy_wanted` Event 하나 — cron 둘이 기다리면 양보가 사라짐 | 대기 카운터 |
| P9 | P2 | C | `ml/features.py:355` | derive 가 파일 락을 쥔 채 heavy 락만 양보 → reflect 스냅샷 LockTimeout | 락 안에서는 양보 안 함 |
| P10 | P2 | C | 로깅 | daemon.err 무한 증가, apscheduler·uvicorn 로그 하루 3.5만 줄 | 데몬 stderr 핸들러 끔, apscheduler WARNING, `access_log=False` |
| P11 | P2 | C | Windows `os.replace` | 다른 프로세스가 연 파일 교체 실패, 피처 읽기 실패를 파손으로 보고 덮어씀 | 재시도 `atomic_replace`, 파손/읽기 실패 구분 |
| P12 | P3 | C | 보존 | `state/runs`·control 날짜 키·`job_requests`·`runs`·`llm_rate_limits` 정리 없음 | reflect 끝에 90일 정리 |
| P13 | P3 | C | `broker/rate_store.py` | KIS 호출마다 SQLite 연결·DDL·fsync | 스레드 로컬 연결 재사용 |

## 3. 웹 UI·텔레그램

| # | 심각도 | 교차 | 위치 | 결함 | 고칠 방향 |
|---|---|---|---|---|---|
| W1 | **P1** | 양쪽 ✔ | `web/src/pages/Orders.tsx:37` | 계좌를 바꾼 직후 남은 **실전** 승인 행을 누르면 확인창 없이 승인(확인 여부를 행이 아니라 현재 선택 계좌로 판단) | 행의 `account_id` 로 환경 판정, `useLoad` deps 바뀌면 데이터 비움 |
| W2 | P1 | X | `web/routers/control.py:102` | 응답 없는(unknown) 승인 주문을 "브로커 거부" 502 로 표시 + 텔레그램 "approve 실패" → 수동 중복 주문 유도 | unknown 별도 응답·문구 |
| W3 | P1 | C | `Dashboard.tsx:65` | '전체'(기본) 선택 때 미승인·주문·성과가 첫 계좌만 셈 → 실전 대기 승인 누락 | 전체 모드는 `overview.pending_approvals`·계좌별 합치기 |
| W4 | P1 | 양쪽 | `Dashboard.tsx:133` | '전체' 시세표 보유 수량이 첫 계좌 한 행 | 환경별 표시 |
| W5 | P2 | X | `web/sse.py:34` | SSE 가 MAX(id)만 비교 → 주문 상태 변경(체결·만료)을 감지 못함 | 테이블별 변경 버전 |
| W6 | P2 | C | `Shell.tsx:25` | '전체' 선택 시 실전 표식이 사라짐 | 실전 포함이면 `data-env="mixed"` |
| W7 | P2 | C | `Advisory.tsx` | 어제·데몬 꺼진 뒤 점검 결과도 '정상' | 오래되면 '점검 오래됨' |
| W8 | P2 | X | `live.ts:36` | 실시간 현재가가 바뀌어도 익절·손절 거리는 스냅샷 기준 | 시세 적용 때 거리 재계산 |
| W9 | P2 | X | `shell.css:247` | 모바일에서 비핵심 열을 숨겨 수량·가격·손절가를 볼 방법이 없음 | 행 상세 펼침 |
| W10 | P2 | 양쪽 | `Dashboard/Compare/Ml` `<tr onClick>` | 키보드로 상세를 열 수 없음 | 행 안 버튼 |
| W11 | P2 | X | `Tables.tsx:124`, Dashboard | US 주문 날짜는 KST·시각은 ET, '오늘' 필터가 시장 혼용 | 시장 현지 시간대 일관 |
| W12 | P2 | C | glossary | '승인'(모드·사람 승인·게이트 통과)·'보유'(HOLD·보유 수량)·'모의'(paper·dry-run) 용어 충돌, BUY/SELL 영문 노출 | 게이트 통과="통과", HOLD="관망", dry_run="주문 없이 판정", side 한글 |
| T1 | P1 | C | `notify/messages.py:210` | 텔레그램 승인 요청에 모의/실전·금액·만료 시각 없음, 헤지 승인은 무엇을 승인하는지 없음 | `[승인 요청 #12 · 실전] … = 715,000원 · 10:42 까지` |
| T2 | P2 | C | `notify/commands.py` | `/status` 의 BUY·UTC 만료·영문 모드, 실전 `/approve` 에 확인 단계 없음, 모르는 명령 무시 | 한글·KST, `/approve 12 실전`, 명령 목록 응답 |
| T3 | P3 | C | 어투·영문 | 해요체·합니다체·명사형 혼용, `(dry-run)`·`auto`·`bear` 노출, 단타 알림 피로 | 문구 표 + 테스트 |

## 4. LLM 투자 방식

| # | 심각도 | 교차 | 위치 | 결함 | 개선안 |
|---|---|---|---|---|---|
| L1 | **P0** | C | `readiness.py`, `memory/outcomes.py`, `backtest.py` | 시스템이 **LLM 이 알파를 더하는지 판별할 수 없음**: 30일 계좌 수익 ≥ 1/N 한 번 비교, 헤지·현금으로 하락장에 낮은 베타만으로 이김, 게이트 거부 결정도 채점, 결정일 종가 진입, 표본 부족 | 같은 후보에 대한 섀도 포트폴리오 3종(후보 1/N·퀀트 top-N·LLM) + 짝 비교 bootstrap 신뢰구간 → readiness 기준 교체 |
| L2 | P1 | 양쪽 | `memory/store.py:58` | 장중 재평가가 같은 메모리 행의 action 을 덮어씀(아침 BUY → 오후 HOLD 로 채점) | 재평가 별도 엔트리, 보유 HOLD/미보유 HOLD 구분, 체결가 진입 |
| L3 | P1 | 양쪽 | `memory/outcomes.py:98` | HOLD=−벤치마크, SELL=가상 공매도 수익으로 채점 | 방향 점수·회피손실·실제 손익 분리 |
| L4 | **P1** | C ✔ | `orchestrator/tools.py:280` | `05_quant` 가 `registry.latest` 에 `market=` 없이 → KR 모델이 US 종목 점수를 냄(계약 위반) | `market=market` 한 줄 |
| L5 | P1 | C | `monitor.py:95`, `tools.py:697` | 보유 재분석 HOLD 의 `stop_loss: null` 이면 손절이 −15% 로 풀리고 익절선 사라짐, 트레이더는 원 진입 논거를 못 봄 | 손절은 파이썬이 단조(올리기만)·null 이면 유지, 재분석 입력에 원 결정·보유일·손익 |
| L6 | P1 | C | `ml/features.py:176` | 백필엔 없는 수급·뉴스 건수 열이 라이브부터 생겨 "NaN=과거" 학습, 뉴스 건수는 스크리너 선택이 새는 피처 | 백필 불가 열 학습 제외 |
| L7 | P1 | C | `daemon.py:610`, `features.py:437` | 유니버스 밖 발굴 종목은 만기 봉이 없어 결과가 영원히 비어 측정에서 빠짐, 장중 미완성 봉을 종가로 저장 | 보유·미만기 종목을 일일 스냅샷에, 장중은 D−1 완성 봉까지 |
| L8 | P1 | C | `risk/regime.py:109`, `stance.py` | 전략가 의견이 국면 4번째 지표 + 한도 기울기로 **이중 반영**(정량 1개 + LLM 만으로 bear) | 둘 중 하나만 |
| L9 | P1 | 양쪽 | `trader.md`, `rules.py`, `costs.py` | LLM 이 비중·손절·익절 숫자를 직접 냄(공통 규칙 2와 모순), 사이징이 위험 기반이 아님, 익절가 null 이면 비용 게이트 통과, "기대수익"=목표 상승률(확률·손절 무시) | 수량은 파이썬 위험 예산(ATR·손절 거리), BUY 에 TP/SL 필수, EV 게이트 `p·(TP−P) − (1−p)·(P−SL) > 비용` |
| L10 | P2 | 양쪽 | `tools.py:606,711` | 트레이더 묶음이 evidence 를 항상 제거 — 그런데 "검증 가능한 근거 수"를 세라고 함 | 파이썬이 역할별 근거 수·공백·신호 일치도를 숫자로 |
| L11 | P2 | C | `summary.py`, researcher md | 약세만 opus·BULLISH 금지 → 정당한 수렴도 '아첨' 판정, HOLD 편향(추정) | 양측 대칭, 수렴 판정에 새 근거 인용 여부 |
| L12 | P2 | 양쪽 | `calibration/model.py`, trader.md | 보정이 가격 부호만(상승장 BUY 자동 적중), 표본 1건에도 근거 절반 할인 | 벤치마크 대비 부호, 이항 신뢰구간 하한, 수축 |
| L13 | P2 | C | `memory/store.py` | 검색이 최근·극단 결과 위주(relevance 0.4 < recency+importance), 교훈이 표본 1건 | relevance 가중↑, n 이상일 때만 교훈 |
| L14 | P2 | 양쪽 | `ml/train.py`, `performance.py:224`, `ranker.py` | 생존편향(현재 KOSPI200 3년 백필)·매주 재학습 다중검정·총수익 알파, 퀀트 벤치마크가 미래 모델을 과거에 적용, 승격 평가와 실제 랭킹 경로 불일치 | 고정 홀드아웃·비용 차감, 당시 모델, 같은 경로로 평가 |
| L15 | P2 | C | `scan/quant.py` | 검증 안 된 10일 모멘텀 스크린이 LLM 후보 전체를 좌우 | 스크린 점수 백테스트, 대조군 |
| L16 | P2 | X | `monitor.py:68` | 무효화 조건 파싱: US $100 미만 가격 무시, "3일 연속 종가" 를 즉시 현재가 조건으로, '이하' 를 `<` 로 | 구조화된 조건 필드 |
| L17 | P2 | C | 비용 | 종목당 에이전트 ~10회(opus 3), 결과 다수 HOLD | 애널리스트 합의 시 토론 생략, 애널리스트 모델 하향, 정량 중립 종목 제외 → 섀도 비교로 확인 |
| L18 | P3 | C | trader.md·monitor | 테마 소속 정보가 트레이더에 없음, 퀀트 5일 확률 vs 20~60일 결정 불일치, 익절 도달이 자동 매도 아닌 재평가인데 최소 보유 3일에 막힘 | context 에 theme, horizon 맞춤, 익절은 파이썬 분할 |

## 5. 잘 되어 있는 것 (양쪽 공통 평가)
- 주문이 `executor` 하나로 모이고, 모드·LIVE 확인·dry_run env 기본값·unknown 처리·조건부 승인 UPDATE 를 우회하는 경로는 찾지 못함.
- 체크포인트가 상태의 원본, 도구 전제조건이 순서를 강제, 룩어헤드 컷오프(`asof`) 설계.
- 단일 인스턴스 잠금·세션 프로세스 그룹 정리·교차 프로세스 레이트리미터·하트비트 골격.
- 웹은 `tokens.css` 위반이 거의 없음(모달 백드롭 rgba 1건).

## 6. 보완 계획
1. **즉시(비주문, CI 녹색이면 머지)** — W1·W2·W3·W4·W6·W7, L4, T1·T2 일부, P1(cancel 전달)·P3(novel_items)·P5·P10, O17.
2. **주문 경로(PR 본문에 diff 요약 → 확인 뒤 머지)** — O1·O2·O3·O4·O5·O9·O11·O13·O14·O16, L5(손절 단조 유지).
3. **설계 결정 필요(사용자 판단)** — L1 섀도 포트폴리오·readiness 기준, L2·L3 메모리 채점 재설계, L8 전략가 이중 반영 중 무엇을 남길지, L9 사이징을 파이썬 위험 예산으로 옮길지, L11 토론 대칭, L17 비용 절감, O8·O10 실행 키·예상 포트폴리오, O6·O7 단타(기본 꺼짐).
