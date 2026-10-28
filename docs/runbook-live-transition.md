# 실전 전환 런북 (Phase 8, 사용자 수동 단계)

실전 계좌(`real-main`)는 `off` 로 시작하며, 어떤 코드 경로도 모드를 자동으로 올리지 않는다.
전환·되돌림은 전부 사용자가 아래 명령으로 수행한다. 각 단계의 명령은 `uv run` 을 앞에 붙인다.

## 0. 전제
- 모의 계좌(`paper-main`)가 `auto` 로 4주 이상 무사고 운영됐다 (`docs/TODO.md` Phase 6·8 완료 기준).
- `secrets/.env` 에 실전 앱키·계좌가 있고 `uv run trader kis ping --account real-main` 이 exit 0.
- **실전 계좌 전용 앱키**를 따로 발급해 계좌 전용 키(`KIS_REAL_MAIN_APP_KEY`·`_APP_SECRET`, 설정 화면
  '연결 키')로 넣고, 시세용 실전 공용 키(`KIS_REAL_APP_KEY`)와 **다른 키**로 둔다. KIS 는 앱키당 웹소켓
  세션이 하나라 같은 키면 실전 모드가 `off` 가 아닌 동안 체결통보가 세션을 쓰고 국내 실시간 시세·호가·
  지수가 전부 10초 폴링으로 내려간다(화면 표 머리 '실전 체결통보가 앱키 사용 중', 2-P13).
- 텔레그램 알림이 동작한다 (`uv run trader notify-test`).

## 1. 준비도 점검 (읽기 전용)
```
uv run trader readiness
```
출력 JSON 의 `ok` 가 true 여야 다음 단계로 간다. 항목과 임계값:

| key | 기준 | 비고 |
|---|---|---|
| paper_days | ≥ 20 | `runs` 의 analyze ok 날짜 수(KST), FAKE 세션 제외 |
| run_error_rate | ≤ 10% | 최근 28일 runs (monitor·텔레그램 같은 interval 잡은 오류일 때만 기록되므로 보수적) |
| order_failed_rate | ≤ 10% | 최근 28일 orders(failed·rejected) |
| return_vs_benchmark | 정보 | 모의 30일 수익률 − 유니버스 1/N(피처 스토어 종가). NAV 가 대부분 현금이면 시장 움직임과 섞여 선택 능력을 재지 못해 판정에서 뺐다 |
| llm_alpha | 만기 ≥ 20쌍 ∧ 95% 구간 하한 > 0 | 섀도 포트폴리오: 같은 날 LLM 이 산 종목(BUY/ADD 목표 비중) − 같은 scan 후보 균등의 5거래일 수익 차이, 부트스트랩 구간. 웹 `/performance` 'LLM 종목 선택 효과' |
| mdd | ≥ −10% | 모의 30일 최대 낙폭 |
| calibration_active | 정보 | 만기 결정 ≥ `ml.yaml` min_decisions(60) |
| stale_approvals | 0건 | 만료됐는데 pending 인 승인 |

`ok` 가 false 면 전환하지 않는다. 성과 세부는 웹 `/performance` 와 `uv run trader backtest --since <모의 시작일>`.

## 2. approve 모드로 전환 (주문마다 텔레그램 승인)
```
uv run trader control mode real-main approve
```
- 실전 리스크 한도는 `config/settings.yaml` 의 `risk` 절(모의와 동일 규칙)이고, 승인 요청은 30분 뒤 만료된다.
- 텔레그램 `/mode real-main off` 는 비상 정지용으로 허용되지만 approve/auto 로 올리는 것은 CLI 만 받는다.
- 1주 동안 매 주문을 텔레그램 `/approve <id>` 또는 웹 주문/승인 페이지에서 직접 승인한다.
  거부는 `/reject <id>`. 손절 매도는 approve 에서도 승인 없이 즉시 나간다(스펙 §6.2).
- 매일 `uv run trader status` 의 미승인 주문·성과 절과 `state/logs/daemon.out` 을 확인한다.

## 3. auto 전환 (확인 문자열 필수)
approve 1주가 무사고이고 `readiness` 가 여전히 true 일 때만:
```
uv run trader control mode real-main auto --confirm LIVE
```
확인 문자열이 없으면 거부된다. 전환 뒤 첫 영업일은 장중에 `trader status` 와 텔레그램 알림을 지켜본다.

## 4. 되돌리기 (언제든)
```
uv run trader control pause            # 전 계좌 즉시 정지 (주문 없음, 손절도 기록만)
uv run trader control mode real-main off
uv run trader control resume           # 모의만 계속 돌릴 때
```
- 열려 있는 승인 요청은 `uv run trader approvals list` 로 확인하고 `reject` 한다.
- 보유 포지션은 자동으로 청산하지 않는다. 필요하면 KIS 앱에서 직접 정리한다.

## 5. 금지
- 코드·데몬·텔레그램 명령으로 실전 모드를 올리는 경로를 추가하지 않는다 (CLAUDE.md 금지사항).
- `readiness` 가 false 인 상태에서 2·3단계를 진행하지 않는다.

## 6. 하락장 헤지와 실전 계좌 (Phase 17)

- 국면이 `bear` 면 파이썬이 인버스 ETF(KR `114800`/`252670`, US `SH`/`SDS`)를 계좌 NAV 의 최대 `hedge.max_weight_pct`(기본 20%)만큼 **실전 계좌에도** 사려고 한다. 단 계좌 모드를 그대로 지킨다: `off` 면 판정만 기록, `approve` 면 승인 요청(30분), `auto` 는 LIVE 확인 기록이 있을 때만 주문.
- 레버리지(−2x)는 3지표가 모두 켜졌을 때만 쓰며 변동성 끌림이 있다. 실전에서 끄려면 `config/settings.yaml` 의 `hedge.leverage: false`, 헤지 자체를 끄려면 `hedge.enabled: false`(국면 판정·게이트도 함께 꺼진다).
- 헤지 ETF 는 monitor 손절·트리거 대상이 아니라 국면 해제(지표 ≤ 1 이 `exit_streak_days` 연속)로만 팔린다. 급히 정리하려면 `trader control pause` 뒤 HTS 로 매도한다(일시정지면 헤지 진입도 보류).
- 현재 국면은 `uv run trader regime show`, 대시보드 상태줄 '국면' 칩, 텔레그램 `/status` 에서 본다.
