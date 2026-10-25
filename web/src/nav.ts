/** 메뉴 한 곳(Phase 23 G): 셸 레일·모바일 하단 탭·⌘K 팔레트·'작동 방식'의 "최근 추가" 가 같은 목록을 쓴다. */

export type NavItem = { to: string; label: string; purpose: string; key?: string };
export type NavGroup = { id: string; title: string; items: NavItem[] };

export const NAV: NavGroup[] = [
  {
    id: "ops",
    title: "운용",
    items: [
      { to: "/", label: "대시보드", purpose: "오늘의 시세표·점검·잡·사용량", key: "d" },
      { to: "/today", label: "오늘의 실행", purpose: "스캔·장중 발굴·단타·종목별 단계 파일", key: "t" },
      { to: "/orders", label: "주문/승인", purpose: "승인 대기·주문·판정·결정 추적", key: "o" },
      { to: "/account", label: "계좌", purpose: "보유·섹터·주문·승인", key: "a" },
      { to: "/compare", label: "계좌 비교", purpose: "모의·실전 나란히" },
    ],
  },
  {
    id: "analysis",
    title: "분석",
    items: [
      { to: "/chart", label: "차트", purpose: "일봉·결정 마커", key: "c" },
      { to: "/performance", label: "성과", purpose: "NAV·벤치마크·준비도", key: "p" },
      { to: "/data", label: "데이터", purpose: "피처 스토어·수집·뉴스" },
      { to: "/ml", label: "ML", purpose: "퀀트·RL 모델" },
    ],
  },
  {
    id: "system",
    title: "시스템",
    items: [
      { to: "/how", label: "작동 방식", purpose: "흐름·규칙·최근 추가", key: "h" },
      { to: "/settings", label: "설정/제어", purpose: "토큰·모드·일시정지·관심종목", key: "s" },
    ],
  },
];

/** 모바일 하단 탭 4칸 + '전체'(서랍). */
export const MOBILE_TABS: NavItem[] = [NAV[0].items[0], NAV[0].items[1], NAV[0].items[2], NAV[0].items[3]];

export type Feature = { since: string; title: string; what: string; to: string };

/** 최근 추가 기능(새 것이 위). 화면·기능을 더하면 여기 한 줄. */
export const FEATURES: Feature[] = [
  { since: "Phase 23", title: "장중 뉴스 발굴", what: "30분마다 새 뉴스(유사 기사 제외)가 있으면 종목 재분석·scout 발굴 세션", to: "/today" },
  { since: "Phase 23", title: "보유기간 프로필", what: "단기·중기·장기 결정마다 급등락 임계·재평가 간격·아침 재분석 주기가 다르다", to: "/account" },
  { since: "Phase 23", title: "분 단위 단타(모의)", what: "1분봉 거래량 급증 ∧ 당일 고가·VWAP 돌파 → 파이썬 진입·청산, 기본 끔", to: "/today" },
  { since: "Phase 23", title: "운영 점검 특보판", what: "손익·주문·대조·데몬·세션 다섯 영역 단계, 격상 때만 알림", to: "/" },
  { since: "Phase 23", title: "아침 미체결 재호가", what: "개장 30분 뒤 지정가 미체결을 취소 → 현재가 재제출(추격 1% 상한)", to: "/orders" },
  { since: "Phase 23", title: "결정 추적", what: "주문 행에서 애널리스트 → 토론 → 결정 → 게이트 → 주문을 한 묶음으로", to: "/orders" },
  { since: "Phase 23", title: "⌘K 이동", what: "화면·종목 코드로 바로 이동(쓰기 명령 없음), g 두 키", to: "/how" },
  { since: "Phase 21", title: "야간 대조", what: "KIS 잔고 vs 원장 보유, 불일치면 알림·스냅샷 보정", to: "/account" },
  { since: "Phase 21", title: "우선순위 큐·/run", what: "손절 뒤 재평가가 백필보다 먼저, 텔레그램 /run", to: "/" },
  { since: "Phase 19", title: "뉴스 저장소·scout 발굴", what: "시장 전체 뉴스로 유니버스 밖 종목 발굴", to: "/today" },
];

export function flatNav(): NavItem[] {
  return NAV.flatMap((g) => g.items);
}

export function recentFeatures(n = 6): Feature[] {
  return FEATURES.slice(0, n);
}

export type PaletteItem = { kind: "page" | "ticker"; label: string; hint: string; to: string };

const KR_CODE = /^\d{6}$/;
const US_SYMBOL = /^[A-Z]{1,5}$/;

/** 팔레트 후보: 화면(라벨·목적 부분 일치) + 종목 코드(6자리 숫자·1~5자 대문자) → 차트. 이동 전용. */
export function paletteItems(query: string, names: Record<string, string> = {}): PaletteItem[] {
  const q = query.trim();
  const out: PaletteItem[] = [];
  const lower = q.toLowerCase();
  for (const it of flatNav()) {
    if (!q || it.label.toLowerCase().includes(lower) || it.purpose.toLowerCase().includes(lower)) {
      out.push({ kind: "page", label: it.label, hint: it.purpose, to: it.to });
    }
  }
  const up = q.toUpperCase();
  if (KR_CODE.test(q)) out.unshift({ kind: "ticker", label: `${names[`KR:${q}`] ?? q} 차트`, hint: `KR ${q}`, to: `/chart?market=KR&ticker=${q}` });
  else if (US_SYMBOL.test(up) && q.length >= 2) out.unshift({ kind: "ticker", label: `${names[`US:${up}`] ?? up} 차트`, hint: `US ${up}`, to: `/chart?market=US&ticker=${up}` });
  if (q.length >= 2) {
    for (const [key, name] of Object.entries(names)) {
      if (name.toLowerCase().includes(lower) && out.length < 12) {
        const [m, t] = key.split(":");
        if (!out.some((o) => o.to.endsWith(`ticker=${t}`))) out.push({ kind: "ticker", label: `${name} 차트`, hint: `${m} ${t}`, to: `/chart?market=${m}&ticker=${t}` });
      }
    }
  }
  return out.slice(0, 12);
}

/** 'g' 뒤 한 키 → 경로. 없으면 null. */
export function chordTarget(key: string): string | null {
  return flatNav().find((i) => i.key === key.toLowerCase())?.to ?? null;
}
