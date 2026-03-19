param(
    [int]$DjangoPort = 8001,
    [int]$StreamlitPort = 8501,
    [int]$PortSearchLimit = 50
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$djangoLog = Join-Path $scriptDir "django_server.auto.log"
$djangoErr = Join-Path $scriptDir "django_server.auto.err.log"
$streamlitLog = Join-Path $scriptDir "streamlit.auto.log"
$streamlitErr = Join-Path $scriptDir "streamlit.auto.err.log"
$pidFile = Join-Path $scriptDir "dashboard_pids.json"

function Test-PortAvailable {
    param([int]$Port)

    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener -ne $null) {
            $listener.Stop()
        }
    }
}

function Find-FreePort {
    param(
        [int]$StartPort,
        [int]$SearchLimit
    )

    $port = $StartPort
    for ($i = 0; $i -lt $SearchLimit; $i++) {
        if (Test-PortAvailable -Port $port) {
            return $port
        }
        $port++
    }

    throw "사용 가능한 포트를 찾을 수 없습니다. 시작 포트=$StartPort, 탐색 범위=$SearchLimit"
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

        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "[STOP] PID $procId"
        } catch {
        }
    }
}

function Get-HttpStatus {
    param(
        [string]$Url,
        [int]$WaitSeconds = 20
    )

    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            return $response.StatusCode
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }

    return -1
}

function Clear-Log {
    param([string]$Path)

    if (Test-Path $Path) {
        Remove-Item -Path $Path -Force -ErrorAction SilentlyContinue
    }
}

Stop-TrackedProcesses -PidFilePath $pidFile

$finalDjangoPort = Find-FreePort -StartPort $DjangoPort -SearchLimit $PortSearchLimit
$finalStreamlitPort = Find-FreePort -StartPort $StreamlitPort -SearchLimit $PortSearchLimit
if ($finalStreamlitPort -eq $finalDjangoPort) {
    $finalStreamlitPort = Find-FreePort -StartPort ($finalStreamlitPort + 1) -SearchLimit $PortSearchLimit
}

Clear-Log -Path $djangoLog
Clear-Log -Path $djangoErr
Clear-Log -Path $streamlitLog
Clear-Log -Path $streamlitErr

$djangoProc = Start-Process -FilePath "python" `
    -ArgumentList @("manage.py", "runserver", "127.0.0.1:$finalDjangoPort", "--noreload") `
    -WorkingDirectory $scriptDir `
    -RedirectStandardOutput $djangoLog `
    -RedirectStandardError $djangoErr `
    -PassThru

$streamlitProc = Start-Process -FilePath "python" `
    -ArgumentList @("-m", "streamlit", "run", "dashboard.py", "--server.port", "$finalStreamlitPort", "--server.headless", "true") `
    -WorkingDirectory $scriptDir `
    -RedirectStandardOutput $streamlitLog `
    -RedirectStandardError $streamlitErr `
    -PassThru

$djangoStatus = Get-HttpStatus -Url "http://127.0.0.1:$finalDjangoPort/" -WaitSeconds 15
$streamlitStatus = Get-HttpStatus -Url "http://127.0.0.1:$finalStreamlitPort/" -WaitSeconds 20

$pidData = @{
    django_pid = $djangoProc.Id
    streamlit_pid = $streamlitProc.Id
    django_port = $finalDjangoPort
    streamlit_port = $finalStreamlitPort
    started_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
}
$pidData | ConvertTo-Json | Set-Content -Path $pidFile -Encoding UTF8

Write-Host ""
Write-Host "Django   : http://127.0.0.1:$finalDjangoPort (PID=$($djangoProc.Id), HTTP=$djangoStatus)"
Write-Host "Streamlit: http://127.0.0.1:$finalStreamlitPort (PID=$($streamlitProc.Id), HTTP=$streamlitStatus)"
Write-Host "PID 파일 : $pidFile"
Write-Host ""
Write-Host "로그 파일:"
Write-Host " - $djangoLog"
Write-Host " - $djangoErr"
Write-Host " - $streamlitLog"
Write-Host " - $streamlitErr"

if ($djangoStatus -eq -1) {
    Write-Host ""
    Write-Host "[WARN] Django HTTP 응답이 없습니다. 에러 로그 마지막 20줄:"
    if (Test-Path $djangoErr) {
        Get-Content -Path $djangoErr -Tail 20
    }
}

if ($streamlitStatus -eq -1) {
    Write-Host ""
    Write-Host "[WARN] Streamlit HTTP 응답이 없습니다. 에러 로그 마지막 20줄:"
    if (Test-Path $streamlitErr) {
        Get-Content -Path $streamlitErr -Tail 20
    }
}
