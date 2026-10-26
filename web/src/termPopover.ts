// 용어 팝오버 위치(순수 함수): 용어 바로 아래, 화면 밖으로 나가면 위로 올리고 좌우는 화면 안에 맞춘다.
// position: fixed 라 표의 가로 스크롤 상자에 잘리지 않는다.
export const POP_WIDTH = 280;
const GAP = 6;
const EDGE = 8;

export function popoverPosition(
  r: { left: number; top: number; bottom: number },
  viewport: { width: number; height: number },
  estHeight = 96,
): { position: "fixed"; left: number; top?: number; bottom?: number; maxWidth: number } {
  const maxWidth = Math.min(POP_WIDTH, viewport.width - EDGE * 2);
  const left = Math.max(EDGE, Math.min(r.left, viewport.width - maxWidth - EDGE));
  const below = r.bottom + GAP;
  if (below + estHeight > viewport.height && r.top - GAP - estHeight > 0) {
    return { position: "fixed", left, bottom: viewport.height - r.top + GAP, maxWidth };
  }
  return { position: "fixed", left, top: below, maxWidth };
}
