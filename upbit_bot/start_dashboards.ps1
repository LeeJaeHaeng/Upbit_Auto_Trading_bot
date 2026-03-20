param(
    [int]$StreamlitPort = 8501,
    [int]$PortSearchLimit = 20
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pidFile      = Join-Path $scriptDir "dashboard_pids.json"
$streamlitLog = Join-Path $scriptDir "streamlit.auto.log"
$streamlitErr = Join-Path $scriptDir "streamlit.auto.err.log"

if (Test-Path $pidFile) {
    try {
        $old = Get-Content $pidFile -Raw | ConvertFrom-Json
        foreach ($p in @($old.streamlit_pid)) {
            if ($p) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
        }
    } catch {}
}

function Find-FreePort([int]$Start, [int]$Limit) {
    for ($p = $Start; $p -lt ($Start + $Limit); $p++) {
        try {
            $l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
            $l.Start(); $l.Stop(); return $p
        } catch {}
    }
    throw "사용 가능한 포트 없음 (시작: $Start)"
}

$port = Find-FreePort -Start $StreamlitPort -Limit $PortSearchLimit

foreach ($f in @($streamlitLog, $streamlitErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

$proc = Start-Process -FilePath "python" `
    -ArgumentList @("-m", "streamlit", "run", "dashboard.py",
                    "--server.port", "$port", "--server.headless", "true") `
    -WorkingDirectory $scriptDir `
    -RedirectStandardOutput $streamlitLog `
    -RedirectStandardError  $streamlitErr `
    -PassThru

$url = "http://127.0.0.1:$port/"
$deadline = (Get-Date).AddSeconds(30)
$status = -1
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $status = $r.StatusCode; break
    } catch { Start-Sleep -Milliseconds 700 }
}

@{ streamlit_pid = $proc.Id; streamlit_port = $port
   started_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") } |
    ConvertTo-Json | Set-Content -Path $pidFile -Encoding UTF8

Write-Host ""
Write-Host "대시보드 : http://127.0.0.1:$port  (PID=$($proc.Id), HTTP=$status)"
Write-Host "로그     : $streamlitLog"
if ($status -eq -1) {
    Write-Host "[WARN] 응답 없음. 에러 로그:"
    if (Test-Path $streamlitErr) { Get-Content $streamlitErr -Tail 20 }
}
