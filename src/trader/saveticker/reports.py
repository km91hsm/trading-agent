"""브라우저 다운로드 PDF를 원본 그대로 보관하고 텍스트를 별도 추출한다."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from trader.saveticker.models import Attachment


def _atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".download-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_pdf(source: Path, root: Path, *, max_bytes: int = 50_000_000) -> Attachment:
    from pypdf import PdfReader

    if source.stat().st_size > max_bytes:
        raise ValueError("pdf_size_limit")
    data = source.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("invalid_pdf")
    reader = PdfReader(source)
    if reader.is_encrypted or not 0 < len(reader.pages) <= 200:
        raise ValueError("unsupported_pdf")
    digest = hashlib.sha256(data).hexdigest()
    relative = Path("attachments") / f"{digest}.pdf"
    _atomic_bytes(root / relative, data)
    # 원본 다운로드 성공과 텍스트 추출 성공은 구분한다.
    try:
        chunks = [page.extract_text() or "" for page in reader.pages]
        content = "\n\n".join(chunks).strip()
    except Exception:
        content = ""
    text_relative = relative.with_suffix(".txt") if content else None
    if text_relative:
        _atomic_bytes(root / text_relative, content.encode("utf-8"))
    return Attachment(
        pdf_path=relative.as_posix(),
        sha256=digest,
        size_bytes=len(data),
        pages=len(reader.pages),
        text_path=text_relative.as_posix() if text_relative else None,
        text_chars=len(content),
    )
