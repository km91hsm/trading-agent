"""로그온 시 데몬 자동 시작 등록 (스펙 §10.1). 파이썬 템플릿 + subprocess, 셸 스크립트 없음.

- macOS: ~/Library/LaunchAgents/com.trader.daemon.plist + `launchctl load -w`
- Windows: %USERPROFILE%/trader-daemon.xml + `schtasks /Create /TN trader-daemon /XML … /F`
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from trader.config import ConfigError
from trader.platform import paths

LABEL = "com.trader.daemon"
TASK_NAME = "trader-daemon"

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>-m</string>
    <string>trader</string>
    <string>daemon</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>EnvironmentVariables</key>
  <dict><key>TRADER_HOME</key><string>{root}</string><key>PYTHONIOENCODING</key><string>utf-8</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{out}</string>
  <key>StandardErrorPath</key><string>{err}</string>
</dict>
</plist>
"""

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>trader daemon (KIS auto trading)</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>-m trader daemon</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def render(platform: str, *, python: Path, project_root: Path, label: str = LABEL) -> str:
    logs = project_root / "state" / "logs"
    if platform == "darwin":
        return _PLIST.format(
            label=escape(label),
            python=escape(str(python)),
            root=escape(str(project_root)),
            out=escape(str(logs / "daemon.out")),
            err=escape(str(logs / "daemon.err")),
        )
    if platform == "win32":
        return _TASK_XML.format(python=escape(str(python)), root=escape(str(project_root)))
    raise ConfigError(f"지원하지 않는 플랫폼입니다: {platform} (macOS·Windows 만)")


def _target(platform: str, home: Path) -> Path:
    if platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if platform == "win32":
        return home / f"{TASK_NAME}.xml"
    raise ConfigError(f"지원하지 않는 플랫폼입니다: {platform} (macOS·Windows 만)")


def _run(run: Callable[..., object], cmd: list[str]) -> None:
    r = run(cmd, shell=False, capture_output=True, encoding="utf-8", errors="replace")
    code = getattr(r, "returncode", 0)
    if code:
        err = (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "").strip()
        raise ConfigError(f"등록 명령 실패({code}): {' '.join(cmd)}: {err}")


def install(
    *,
    dry_run: bool,
    run: Callable[..., object] = subprocess.run,
    platform: str = sys.platform,
    home: Path | None = None,
    python: Path | None = None,
    project_root: Path | None = None,
) -> dict:
    """템플릿을 쓰고 OS 등록 명령을 실행한다. dry_run 이면 아무것도 쓰지 않고 명령만 돌려준다."""
    home = home or Path.home()
    python = python or Path(sys.executable)
    project_root = project_root or paths.project_root()
    path = _target(platform, home)
    text = render(platform, python=python, project_root=project_root)
    if platform == "darwin":
        command = ["launchctl", "load", "-w", str(path)]
    else:
        command = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"]
    if dry_run:
        return {"path": path, "command": command, "written": False, "content": text}
    path.parent.mkdir(parents=True, exist_ok=True)
    (project_root / "state" / "logs").mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8" if platform == "darwin" else "utf-16")
    _run(run, command)
    return {"path": path, "command": command, "written": True, "content": text}


def uninstall(
    *,
    run: Callable[..., object] = subprocess.run,
    platform: str = sys.platform,
    home: Path | None = None,
) -> dict:
    home = home or Path.home()
    path = _target(platform, home)
    if platform == "darwin":
        command = ["launchctl", "unload", "-w", str(path)]
    else:
        command = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    if path.exists() or platform == "win32":
        _run(run, command)
    if path.exists():
        path.unlink()
    return {"path": path, "command": command}
