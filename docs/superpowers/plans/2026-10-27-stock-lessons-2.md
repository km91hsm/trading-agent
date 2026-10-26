# Stock 교훈 2차 — 운용·화면 (2026-10-27)

1차(안전장치·판단 근거) 뒤. 매매를 바꾸는 항목(현금 ETF 운용·권리 일정 블랙아웃)은 **기본 끔**.

| # | 항목 | 구현 | Stock 근거 |
|---|---|---|---|
| A | 장 마감 일일 리포트·`/daily` | `ledger/daily_report.py`(계좌·시장 NAV·전일 대비·실현손익·주문/체결·미결·킬스위치·국면), 데몬 cron 시장 마감 뒤 + 텔레그램 `/daily` | D-095 |
| B | 위험 카드 | `ledger/risk_card.py`(보유 비중 × 60일 수익: VaR·CVaR 95%, 유니버스 1/N 대비 베타, 상관, 위험 기여) → `GET /api/portfolio/risk` → 계좌 페이지 `RiskCard` | D-088 |
| B' | 기준 시각·오래됨 표시 | 웹 `asOf` 순수 함수 + `AsOf` 표기(잔고·위험 카드·뉴스), 기준을 넘으면 황색 '오래됨' | D-096 |
| B'' | ML 생존 편향 경고 표시 | ML 페이지 모델 행에 `caveats` | D-037 |
| C | 아침 브리핑 | `.claude/agents/briefing.md`(haiku) + `schemas.Briefing` + `quick.ask_json`, 데몬 cron 07:20 KST 평일(구독 사용률 soft 이상·상한이면 생략), 텔레그램 5줄 | docs/06 |
| D | 권리 일정 블랙아웃(기본 끔) | 예탁원 일정 TR(유상·무상증자·액면교체·합병분할, Stock 대조값) 수집 → `control.ksd_events:KR` → `rules.evaluate(blackout=)` 기준일 5일 전~2일 뒤 BUY 거부 | D-108·D-113 |
| E | 현금 단기금리 ETF(기본 끔) | 하락 국면(신규 매수 막힘)일 때만 남는 현금을 `cash_park.ticker`(357870)로, 국면이 풀리면 판다. monitor 첫 틱(헤지 뒤), `execute(is_park=True)` 48_/58_, 게이트는 헤지처럼 면제 | D-115 |
| F | Tailscale 폰 접속 | `web.tailscale.hosts`(tailnet 이름)·`users`(허용 로그인) — 비면 지금처럼 로컬만 | D-054 |
