param(
    [int]$DjangoPort = 8001,
    [int]$StreamlitPort = 8501,
    [int]$PortRangeCount = 20,
    [switch]$AlsoKillByPort
)

$ErrorActionPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $scriptDir "dashboard_pids.json"

function Get-ListeningPids {
    param([int]$Port)

    $lines = netstat -ano | Select-String ":$Port" | Select-String "LISTENING"
    $pids = @()

    foreach ($line in $lines) {
        $parts = ($line.ToString().Trim() -split "\s+")
        if ($parts.Count -lt 5) {
            continue
        }

        $procId = $parts[-1]
        if ($procId -match "^\d+$" -and $procId -ne "0") {
            $pids += [int]$procId
        }
    }

    return $pids | Sort-Object -Unique
}

function Stop-TrackedProcesses {
    param([string]$PidFilePath)

    if (-not (Test-Path $PidFilePath)) {
        return
    }

    try {
        $pidData = Get-Content -Path $PidFilePath -Raw | ConvertFrom-Json
    } catch {
        Write-Host "[WARN] PID 파일 파싱 실패: $($_.Exception.Message)"
        return
    }

    $pidList = @()
    foreach ($name in @("django_pid", "streamlit_pid", "django_pids", "streamlit_pids")) {
        if ($pidData.$name) {
            $pidList += @($pidData.$name)
        }
    }

    foreach ($rawPid in ($pidList | Sort-Object -Unique)) {
        $procId = 0
        if (-not [int]::TryParse("$rawPid", [ref]$procId)) {
            continue
        }
        Stop-Process -Id $procId -Force
        Write-Host "[STOP] PID $procId (pid 파일)"
    }
}

function Kill-PortRange {
    param(
        [int]$StartPort,
        [int]$Count
    )

    for ($port = $StartPort; $port -lt ($StartPort + $Count); $port++) {
        foreach ($procId in (Get-ListeningPids -Port $port)) {
            Stop-Process -Id $procId -Force
            Write-Host "[KILL] Port $port PID $procId"
        }
    }
}

Stop-TrackedProcesses -PidFilePath $pidFile

if ($AlsoKillByPort) {
    Kill-PortRange -StartPort $DjangoPort -Count $PortRangeCount
    Kill-PortRange -StartPort $StreamlitPort -Count $PortRangeCount
}

Write-Host "대시보드 프로세스 정리 완료"
