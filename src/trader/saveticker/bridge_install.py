"""Reviewable Chrome extension packages and explicit Native Messaging installation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path

from trader.platform import bundle, paths
from trader.saveticker.store import atomic_json, atomic_text

HOST = "com.trader.saveticker"
EXTENSION_ID = "gfchfnjghmjiidifohjglpodaakdphho"
PERMISSIONS = {"alarms", "storage", "nativeMessaging", "downloads"}


class InstallError(RuntimeError):
    """Fixed installation error codes; never include credential contents."""


def extension_source() -> Path:
    candidates = [bundle.resource_dir() / "extensions/saveticker"]
    if not bundle.is_frozen():
        candidates.append(Path(__file__).resolve().parents[3] / "extensions/saveticker")
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate
    raise InstallError("extension_resources_missing")


def _manifest(folder: Path) -> dict:
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        raw = base64.b64decode(manifest["key"], validate=True)
        digest = hashlib.sha256(raw).hexdigest()[:32]
        identity = "".join(chr(97 + int(c, 16)) for c in digest)
        if identity != EXTENSION_ID:
            raise ValueError
        if set(manifest["permissions"]) != PERMISSIONS:
            raise ValueError
        if manifest["host_permissions"] != ["https://www.saveticker.com/*"]:
            raise ValueError
        if manifest["manifest_version"] != 3:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError("invalid_extension_manifest") from None
    return manifest


def collector_root(project_home: Path) -> Path:
    from trader import config

    try:
        configured = config.load_sources(project_home / "config/sources.yaml").news.saveticker_dir
    except config.ConfigError:
        configured = None
    root = Path(configured) if configured else Path("state/external/saveticker")
    return (root if root.is_absolute() else project_home / root).resolve()


def prepare(destination: Path, *, project_home: Path | None = None) -> dict:
    """Write a package only. No Chrome registration or backend activation."""
    project_home = (project_home or paths.project_root()).resolve()
    destination = destination.resolve()
    if destination.exists():
        raise InstallError("destination_exists")
    if sys.platform == "win32" and not bundle.is_frozen():
        raise InstallError("windows_source_requires_bundle")
    source = extension_source()
    _manifest(source)
    destination.mkdir(parents=True, mode=0o700)
    extension = destination / "extension"
    shutil.copytree(source, extension, ignore=shutil.ignore_patterns("tests", "node_modules"))
    root = collector_root(project_home)
    if bundle.is_frozen():
        runtime = destination / "native-runtime"
        runtime.mkdir()
        original = bundle.cli_executable()
        app = bundle.app_bundle_dir()
        if app is not None:
            copied_app = runtime / app.name
            shutil.copytree(app, copied_app, symlinks=True)
            target = copied_app / original.relative_to(app)
        else:
            target = runtime / original.name
            shutil.copy2(original, target)
            internal = original.parent / "_internal"
            if internal.is_dir():
                shutil.copytree(internal, runtime / "_internal", symlinks=True)
        if sys.platform == "win32" and app is None:
            executable = target
            atomic_json(
                runtime / "native-config.json",
                {
                    "collector_root": str(root),
                    "project_home": str(project_home),
                },
            )
        else:
            executable = destination / "native-host"
            command = shlex.join(
                [
                    "env",
                    f"TRADER_HOME={project_home}",
                    f"SAVETICKER_NATIVE_ROOT={root}",
                    str(target),
                ]
            )
            atomic_text(executable, "#!/bin/sh\nexec " + command + ' "$@"\n')
            executable.chmod(0o700)
    else:
        executable = destination / "native-host"
        command = shlex.join(
            [
                "env",
                f"TRADER_HOME={project_home}",
                f"SAVETICKER_NATIVE_ROOT={root}",
                sys.executable,
                "-m",
                "trader",
                "saveticker",
                "native-host",
            ]
        )
        atomic_text(executable, "#!/bin/sh\nexec " + command + ' "$@"\n')
        executable.chmod(0o700)
    host = {
        "name": HOST,
        "description": "SaveTicker local collector",
        "type": "stdio",
        "path": str(executable),
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    atomic_json(destination / "native-host.json", host)
    metadata = {
        "schema_version": 1,
        "extension_id": EXTENSION_ID,
        "project_home": str(project_home),
        "collector_root": str(root),
        "extension_dir": str(extension),
        "native_manifest": str(destination / "native-host.json"),
        "installed": False,
        "permissions": sorted(PERMISSIONS),
        "site": "https://www.saveticker.com/*",
    }
    atomic_json(destination / "prepared.json", metadata)
    atomic_text(
        destination / "REVIEW.md",
        "# 설치 전 확인\n\n"
        "일반 Chrome의 세이브티커 페이지 전용 수집 확장입니다.\n\n"
        "- 권한: 세이브티커 페이지 읽기·전용 탭 관리, 주기 알람,\n"
        "  로컬 프로그램 연결, 리포트 다운로드\n"
        "- 비밀번호·쿠키 내보내기·다른 사이트 접근·AI·브로커 호출 없음\n"
        "- Chrome과 컴퓨터가 켜져 있어야 수집합니다. 만료/재확인은 직접 완료해야 합니다.\n"
        "- Native host 등록과 기존 자동로그인 backend 전환은 별도 install-host 명령입니다.\n"
        "- 설치 후 chrome://extensions에서 개발자 모드 → 압축해제된 확장 로드 → extension 폴더\n",
    )
    return metadata


def _prepared(folder: Path, *, check_config: bool = True) -> tuple[dict, Path, Path]:
    try:
        record = json.loads((folder / "prepared.json").read_text(encoding="utf-8"))
        _manifest(folder / "extension")
        host = json.loads((folder / "native-host.json").read_text(encoding="utf-8"))
        if record["extension_id"] != EXTENSION_ID or host["allowed_origins"] != [
            f"chrome-extension://{EXTENSION_ID}/"
        ]:
            raise ValueError
        if not Path(host["path"]).is_file():
            raise ValueError
        project = Path(record["project_home"]).resolve()
        root = Path(record["collector_root"]).resolve()
        if check_config and root != collector_root(project):
            raise ValueError
        return record, project, root
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError("invalid_prepared_package") from None


def registration_path(user_home: Path) -> Path:
    if sys.platform == "darwin":
        base = user_home / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
    elif sys.platform == "win32":
        base = user_home / "AppData/Local/Trader/NativeMessagingHosts"
    else:
        base = user_home / ".config/google-chrome/NativeMessagingHosts"
    return base / f"{HOST}.json"


def _registry_value():
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Google\Chrome\NativeMessagingHosts\{HOST}"
        ) as key:
            try:
                return winreg.QueryValueEx(key, "")[0]
            except FileNotFoundError:
                return ""
    except FileNotFoundError:
        return None


def _write_registration(target: Path, host: dict, *, register_windows: bool = True) -> None:
    atomic_json(target, host)
    if sys.platform == "win32" and register_windows:
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"Software\Google\Chrome\NativeMessagingHosts\{HOST}"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(target))


def _remove_registration(target: Path, *, register_windows: bool = True) -> None:
    if sys.platform == "win32" and register_windows:
        import winreg

        value = _registry_value()
        if value is not None:
            if value not in ("", str(target)):
                raise InstallError("registration_owner_mismatch")
            winreg.DeleteKey(
                winreg.HKEY_CURRENT_USER, rf"Software\Google\Chrome\NativeMessagingHosts\{HOST}"
            )
    target.unlink(missing_ok=True)


def _stop_legacy(root: Path) -> None:
    """Terminate only a verified collector service; never the trading daemon."""
    from trader.daemon import Lock
    from trader.saveticker import automation

    if not Lock(root / "runtime/service.lock").held():
        automation.kill_leftovers(root)
        return
    row = automation.read(root / "runtime/service.json") or {}
    pid = row.get("pid")
    if os.name == "nt" or not isinstance(pid, int) or pid == os.getpid():
        raise InstallError("stop_legacy_collector_first")
    import subprocess

    command = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    if "trader saveticker service" not in command:
        raise InstallError("unverified_legacy_process")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 70
    while Lock(root / "runtime/service.lock").held():
        if time.monotonic() >= deadline:
            raise InstallError("legacy_stop_timeout")
        time.sleep(0.2)
    automation.kill_leftovers(root)


def install_host(folder: Path, *, user_home: Path | None = None) -> dict:
    folder = folder.resolve()
    _, project, root = _prepared(folder)
    registration = registration_path(user_home or Path.home())
    config = root / "runtime/bridge-config.json"
    env = project / "secrets/saveticker.env"
    backup = env.with_name(env.name + ".extension-backup")
    if backup.exists():
        raise InstallError("backup_exists")
    windows_registry = sys.platform == "win32" and user_home is None
    if (
        registration.exists()
        or config.exists()
        or (windows_registry and _registry_value() is not None)
    ):
        raise InstallError("registration_exists")
    _stop_legacy(root)
    moved = False
    try:
        if env.is_file():
            env.rename(backup)
            moved = True
        host = json.loads((folder / "native-host.json").read_text(encoding="utf-8"))
        _write_registration(registration, host, register_windows=windows_registry)
        atomic_json(
            config,
            {
                "backend": "extension",
                "extension_id": EXTENSION_ID,
                "package": str(folder),
                "registration": str(registration),
                "credentials_backed_up": moved,
            },
        )
    except Exception:
        _remove_registration(registration, register_windows=windows_registry)
        config.unlink(missing_ok=True)
        if moved and backup.exists() and not env.exists():
            backup.rename(env)
        raise InstallError("registration_failed") from None
    return {"ok": True, "registration": str(registration), "backend": "extension"}


def uninstall_host(folder: Path, *, user_home: Path | None = None) -> dict:
    _, project, root = _prepared(folder.resolve(), check_config=False)
    registration = registration_path(user_home or Path.home())
    config = root / "runtime/bridge-config.json"
    env = project / "secrets/saveticker.env"
    backup = env.with_name(env.name + ".extension-backup")
    try:
        installed = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InstallError("not_installed") from None
    if installed.get("package") != str(folder.resolve()):
        raise InstallError("registration_owner_mismatch")
    if env.exists() and backup.exists():
        raise InstallError("credentials_conflict")
    # Active native host must be disconnected from the popup first.
    from trader.daemon import Lock

    if Lock(root / "runtime/bridge-host.lock").held():
        raise InstallError("stop_extension_first")
    if registration.exists():
        try:
            actual = json.loads(registration.read_text(encoding="utf-8"))
            expected = json.loads((folder / "native-host.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise InstallError("registration_owner_mismatch") from None
        if actual != expected:
            raise InstallError("registration_owner_mismatch")
    _remove_registration(registration, register_windows=user_home is None)
    config.unlink(missing_ok=True)
    if backup.exists():
        backup.rename(env)
    return {"ok": True, "backend": "legacy", "credentials_restored": env.exists()}


def native_entry(origin: str) -> int:
    """Native routing bypasses CLI logging and never executes broker/daemon commands."""
    from trader.saveticker import home, native

    selected_root = os.environ.get("SAVETICKER_NATIVE_ROOT")
    pointer = bundle.cli_executable().parent / "native-config.json"
    if selected_root:
        root = Path(selected_root)
    elif bundle.is_frozen() and pointer.is_file():
        try:
            root = Path(json.loads(pointer.read_text(encoding="utf-8"))["collector_root"])
        except (OSError, ValueError, KeyError, TypeError):
            return 1
    else:
        root = home()
    try:
        config = json.loads((root / "runtime/bridge-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    if config.get("backend") != "extension" or config.get("extension_id") != EXTENSION_ID:
        return 1
    result = native.serve(root, EXTENSION_ID, origin)
    return result if isinstance(result, int) else 0


def is_native_origin(value: str) -> bool:
    return re.fullmatch(r"chrome-extension://[a-p]{32}/", value) is not None
