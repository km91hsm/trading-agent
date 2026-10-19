"""다른 프로젝트 .env 에서 이 저장소가 쓰는 키만 이름 매핑으로 복사한다 (값은 출력 금지)."""

from trader import secrets_io


def test_import_fills_missing_keys_only_and_maps_names(tmp_path):
    src = tmp_path / "stock.env"
    src.write_text(
        "TRADING_MODE=paper\nKIS_APP_KEY=pk\nKIS_APP_SECRET=ps\nKIS_LIVE_APP_KEY=rk\n"
        "KIS_LIVE_APP_SECRET=rs\nDART_API_KEY=d1\nNAVER_CLIENT_ID=n1\nNAVER_CLIENT_SECRET=n2\n"
        "FRED_API_KEY=f1\nTELEGRAM_BOT_TOKEN=t1\nTELEGRAM_CHAT_ID=c1\nYOUTUBE_API_KEY=y\n",
        encoding="utf-8",
    )
    dst = tmp_path / "secrets.env"
    dst.write_text("# 기존\nDART_API_KEY=old\nNAVER_CLIENT_ID=\nWEB_TOKEN=w\n", encoding="utf-8")
    r = secrets_io.import_env(src, dst)
    text = dst.read_text(encoding="utf-8")
    assert "DART_API_KEY=old" in text  # 기존 값 유지
    assert "NAVER_CLIENT_ID=n1" in text  # 비어 있던 키는 채움
    assert "KIS_PAPER_APP_KEY=pk" in text and "KIS_REAL_APP_SECRET=rs" in text  # 이름 매핑
    assert "WEB_TOKEN=w" in text and "# 기존" in text  # 기존 줄 보존
    assert "YOUTUBE_API_KEY" not in text  # 이 저장소가 쓰지 않는 키는 복사하지 않음
    assert set(r["copied"]) == {
        "KIS_PAPER_APP_KEY",
        "KIS_PAPER_APP_SECRET",
        "KIS_REAL_APP_KEY",
        "KIS_REAL_APP_SECRET",
        "NAVER_CLIENT_ID",
        "NAVER_CLIENT_SECRET",
        "FRED_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }
    assert r["kept"] == ["DART_API_KEY"] and "ECOS_API_KEY" in r["unavailable"]
    assert r["differs"] == ["DART_API_KEY"] and r["same"] == []  # old ≠ d1
    assert not any("old" in str(v) or "n1" in str(v) for v in r.values())  # 결과에 값 없음


def test_import_overwrite_selected_keys(tmp_path):
    src = tmp_path / "stock.env"
    src.write_text(
        "NAVER_CLIENT_ID=new\nNAVER_CLIENT_SECRET=new2\nDART_API_KEY=d\n", encoding="utf-8"
    )
    dst = tmp_path / "secrets.env"
    dst.write_text(
        "NAVER_CLIENT_ID=old\nNAVER_CLIENT_SECRET=old2\nDART_API_KEY=old3\n", encoding="utf-8"
    )
    r = secrets_io.import_env(
        src, dst, overwrite=True, keys=["NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    )
    text = dst.read_text(encoding="utf-8")
    assert "NAVER_CLIENT_ID=new" in text and "NAVER_CLIENT_SECRET=new2" in text
    assert "DART_API_KEY=old3" in text  # keys 밖은 불변
    assert set(r["copied"]) == {"NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"}


def test_cli_secrets_import_prints_names_only(project, tmp_path):
    from typer.testing import CliRunner

    from trader.cli import app

    src = tmp_path / "stock.env"
    src.write_text("DART_API_KEY=SECRET-VALUE-123\n", encoding="utf-8")
    r = CliRunner().invoke(app, ["secrets", "import", "--from", str(src)])
    assert r.exit_code == 0, r.stdout
    assert "DART_API_KEY" in r.stdout and "SECRET-VALUE-123" not in r.stdout
    assert (project / "secrets" / ".env").read_text(encoding="utf-8").strip() == (
        "DART_API_KEY=SECRET-VALUE-123"
    )


def test_overwrite_keeps_timestamped_backup(tmp_path):
    """값을 바꾸기 전 원본 파일을 .bak-<YYYYMMDD_HHMMSS> 로 남긴다 (되돌릴 수 있게)."""
    src = tmp_path / "stock.env"
    src.write_text("DART_API_KEY=new\n", encoding="utf-8")
    dst = tmp_path / "secrets.env"
    dst.write_text("DART_API_KEY=old\n", encoding="utf-8")
    r = secrets_io.import_env(src, dst, overwrite=True, keys=["DART_API_KEY"])
    backups = list(tmp_path.glob("secrets.env.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "DART_API_KEY=old\n"
    assert r["backup"] == backups[0].name
    # 바꾼 것이 없으면 백업도 없다
    r2 = secrets_io.import_env(src, dst)
    assert r2["copied"] == [] and r2["backup"] is None
    assert len(list(tmp_path.glob("secrets.env.bak-*"))) == 1
