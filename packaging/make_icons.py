"""앱 아이콘 생성: 웹 파비콘과 같은 마크(둥근 먹 바탕 + 미색 ▲)를 Pillow 로 직접 그린다.

산출: packaging/icons/trader.ico (Windows), trader.icns (macOS, iconutil 이 있을 때), tray-dark.png·
tray-light.png (메뉴바/작업표시줄용 44px 단색 ▲, 투명 바탕). 생성물은 커밋한다 — CI 는 다시 만들지 않는다.

사용: uv run --group build python packaging/make_icons.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "icons"
INK = (0x17, 0x18, 0x1A, 255)
PAPER = (0xF6, 0xF4, 0xEE, 255)


def render_mark(size: int, *, ink=INK, paper=PAPER) -> Image.Image:
    """favicon.svg 와 같은 기하: viewBox 32 기준 rx=5 둥근 사각 + 꼭짓점 (16,8.5)(24.5,22)(7.5,22) 삼각형."""
    scale = 8  # 안티에일리어싱을 위해 크게 그려 줄인다
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = big / 32
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=5 * u, fill=ink)
    d.polygon([(16 * u, 8.5 * u), (24.5 * u, 22 * u), (7.5 * u, 22 * u)], fill=paper)
    return img.resize((size, size), Image.LANCZOS)


def render_tray(size: int, color: tuple[int, int, int, int]) -> Image.Image:
    """트레이용: 투명 바탕에 단색 ▲ 하나 (macOS 메뉴바는 어두운 색, 다크 메뉴바는 밝은 색)."""
    scale = 8
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = big / 32
    d.polygon([(16 * u, 5 * u), (28 * u, 26 * u), (4 * u, 26 * u)], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    base = render_mark(256)
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])


def write_icns(path: Path) -> bool:
    """macOS 의 iconutil 로 .icns 를 만든다. 없으면 False (다른 OS 에서는 건너뛴다)."""
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "trader.iconset"
        iconset.mkdir()
        for s in (16, 32, 128, 256, 512):
            render_mark(s).save(iconset / f"icon_{s}x{s}.png")
            render_mark(s * 2).save(iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(
            [iconutil, "-c", "icns", str(iconset), "-o", str(path)],
            shell=False, check=True, encoding="utf-8", errors="replace",
        )
    return True


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_ico(OUT / "trader.ico")
    made_icns = write_icns(OUT / "trader.icns")
    render_tray(44, (0x17, 0x18, 0x1A, 255)).save(OUT / "tray-dark.png")
    render_tray(44, (0xF6, 0xF4, 0xEE, 255)).save(OUT / "tray-light.png")
    render_mark(512).save(OUT / "trader-512.png")
    print(f"ico·png 생성, icns {'생성' if made_icns else '건너뜀(iconutil 없음)'}: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
