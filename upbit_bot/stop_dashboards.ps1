param([switch]$AlsoKillByPort, [int]$StreamlitPort = 8501, [int]$PortRangeCount = 20)

$ErrorActionPreference = "SilentlyContinue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $scriptDir "dashboard_pids.json"

if (Test-Path $pidFile) {
    try {
        $d = Get-Content $pidFile -Raw | ConvertFrom-Json
        foreach ($p in @($d.streamlit_pid)) {
            if ($p) { Stop-Process -Id $p -Force; Write-Host "[STOP] PID $p" }
        }
        Remove-Item $pidFile -Force
    } catch { Write-Host "[WARN] PID 파일 처리 실패" }
}

if ($AlsoKillByPort) {
    for ($port = $StreamlitPort; $port -lt ($StreamlitPort + $PortRangeCount); $port++) {
        netstat -ano | Select-String ":$port" | Select-String "LISTENING" | ForEach-Object {
            $pid = ($_ -split "\s+")[-1]
            if ($pid -match "^\d+$") { Stop-Process -Id $pid -Force; Write-Host "[KILL] Port $port PID $pid" }
        }
    }
}

Write-Host "대시보드 종료 완료"
