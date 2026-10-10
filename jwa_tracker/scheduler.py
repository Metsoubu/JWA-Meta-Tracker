"""Windows Task Scheduler integration (no administrator rights needed).

The task runs `pythonw.exe tracker.py collect --trigger scheduled` every hour,
plus a few minutes after you log in. The collector itself decides whether an
update is due (see collector.is_due), so the effective cadence is every 6 hours
(midnight, 6 am, noon and 6 pm), with hourly retries after a failure and automatic catch-up
when the computer was off or asleep at the scheduled time ("StartWhenAvailable").
Claude Code or the dashboard do not need to be open.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from . import config

CREATE_NO_WINDOW = 0x08000000


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def task_xml(command: Path, script: Path, workdir: Path, user: str) -> str:
    start = datetime.now().replace(minute=5, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    args = f'"{script}" collect --trigger scheduled'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{escape(config.APP_NAME)}</Author>
    <Description>Checks for new Jurassic World Alive leaderboard data. Updates run every 6 hours (midnight, 6 am, noon, 6 pm); hourly checks retry failures and catch up after the PC was off. Created by {escape(config.APP_NAME)}.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT1H</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT3M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Automatic updates use Windows Task Scheduler and are only set up on Windows."
    xml = task_xml(pythonw_path(), config.ENTRY_SCRIPT, config.PROJECT_DIR, current_user())
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
        fh.write(xml)
        path = fh.name
    try:
        result = _run(["schtasks", "/Create", "/TN", config.TASK_NAME, "/XML", path, "/F"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "schtasks failed").strip()
    _status_cache.clear()
    return True, "Automatic updates are set up in Windows Task Scheduler."


def remove() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "Not on Windows."
    result = _run(["schtasks", "/Delete", "/TN", config.TASK_NAME, "/F"])
    _status_cache.clear()
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, "Automatic updates were removed from Windows Task Scheduler."


def run_now() -> tuple[bool, str]:
    result = _run(["schtasks", "/Run", "/TN", config.TASK_NAME])
    return result.returncode == 0, (result.stdout or result.stderr).strip()


_PS_STATUS = r"""
$ErrorActionPreference = 'Stop'
try {
  $t = Get-ScheduledTask -TaskName '__NAME__'
  $i = $t | Get-ScheduledTaskInfo
  $a = $t.Actions | Select-Object -First 1
  [pscustomobject]@{
    installed = $true; state = [string]$t.State
    command = [string]$a.Execute; arguments = [string]$a.Arguments; workdir = [string]$a.WorkingDirectory
    last_run = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
    next_run = if ($i.NextRunTime) { $i.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
    last_result = $i.LastTaskResult
  } | ConvertTo-Json -Compress
} catch { '{"installed": false}' }
"""

_status_cache: dict[str, Any] = {}


def status(max_age: float = 60.0) -> dict[str, Any]:
    """Task state from Windows (cached briefly; PowerShell takes about a second)."""
    if os.name != "nt":
        return {"installed": False, "supported": False}
    cached = _status_cache.get("value")
    if cached and time.monotonic() - _status_cache["at"] < max_age:
        return cached
    script = _PS_STATUS.replace("__NAME__", config.TASK_NAME.replace("'", "''"))
    try:
        result = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=30)
        info = json.loads((result.stdout or "").strip() or '{"installed": false}')
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        info = {"installed": None, "error": f"Could not read Task Scheduler status: {exc}"}
    info["supported"] = True
    if info.get("installed"):
        expected_cmd = str(pythonw_path()).lower()
        info["up_to_date"] = (
            (info.get("command") or "").strip('"').lower() == expected_cmd
            and str(config.ENTRY_SCRIPT).lower() in (info.get("arguments") or "").lower()
        )
    _status_cache.update(value=info, at=time.monotonic())
    return info


def ensure_installed() -> tuple[bool, str]:
    """Install or repair the task (e.g. after Python was upgraded or the folder moved)."""
    info = status(max_age=0)
    if info.get("installed") and info.get("up_to_date"):
        return True, "Automatic updates are active."
    return install()
