"""Windows 热点入站修复：仅提升固定的防火墙命令，不提升主程序。"""
from __future__ import annotations

import base64
import os
from pathlib import Path


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def hotspot_rule_script(port: int) -> str:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("手机显示端口必须在 1024–65535 之间")
    # 参数只来自经过校验的整数；不执行来自配置/地址栏的命令文本。
    return f"""$ErrorActionPreference = 'Stop'
try {{
    $rule = @{{
        Name = 'WTMonitorTerminal-Phone-Hotspot-TCP-{port}'
        DisplayName = 'WT Phone Hotspot TCP {port}'
        Direction = 'Inbound'
        Action = 'Allow'
        Enabled = 'True'
        Profile = 'Any'
        Protocol = 'TCP'
        LocalPort = {port}
        RemoteAddress = '192.168.137.0/24'
        PolicyStore = 'PersistentStore'
    }}
    $existing = Get-NetFirewallRule -Name $rule.Name -PolicyStore PersistentStore -ErrorAction SilentlyContinue
    if ($existing) {{
        $displayName = $rule.DisplayName
        $rule.Remove('DisplayName')
        Set-NetFirewallRule @rule -NewDisplayName $displayName | Out-Null
    }} else {{
        New-NetFirewallRule @rule | Out-Null
    }}
    exit 0
}} catch {{
    exit 1
}}
"""


def repair_command(port: int) -> tuple[str, list[str]]:
    """外层普通权限 PowerShell 等待 UAC 子进程，并把真实退出码交给 Qt。"""
    encoded = _encoded(hotspot_rule_script(port))
    launcher = f"""$ErrorActionPreference = 'Stop'
try {{
    $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}'
    exit $child.ExitCode
}} catch {{
    if ($_.Exception.NativeErrorCode -eq 1223 -or $_.Exception.InnerException.NativeErrorCode -eq 1223) {{ exit 1223 }}
    exit 1
}}
"""
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return str(powershell), ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                             "-EncodedCommand", _encoded(launcher)]
