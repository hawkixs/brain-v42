<#
.SYNOPSIS
    brain-v42 dev-pc headless boot script (plan Task 5 — NAT + portproxy).

.DESCRIPTION
    Runs at Windows boot (via the "brain-embedding-boot" Task Scheduler task,
    "run whether logged on or not") so the LAN endpoint 192.168.1.11:8003
    survives a reboot-to-login-screen, with NO interactive logon.

    Steps:
      1. Instantiate the Ubuntu-24.04 WSL2 distro (systemd PID1 -> native
         docker-ce -> brain_embedding_supervisor). The always-on systemd
         keepalive unit (sleep infinity) holds the VM up after this returns.
      2. Wait until the supervisor answers GET localhost:8003/healthz from
         INSIDE the distro (supervisor liveness only — no GPU touched here).
      3. Compute the distro's current NAT IP (changes across boots).
      4. Refresh ONLY the :8003 v4tov4 portproxy rule (delete-then-add) so
         0.0.0.0:8003 on the Windows host forwards to <wsl-ip>:8003. We NEVER
         `reset all` and NEVER touch :8001/:9100 (existing services).
      5. Ensure a firewall allow rule for inbound TCP 8003 exists (idempotent).

    Networking is NAT + portproxy on purpose (judge C5 / spec §5.4); mirrored
    networking is explicitly out of scope. See windows/.wslconfig.

.NOTES
    Idempotent and re-run safe: the portproxy rule is delete-then-add, and the
    firewall rule is created only if absent. Errors on the best-effort cleanup
    paths are swallowed so a fresh boot (no prior rule) does not abort.
#>

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- Tunables -----------------------------------------------------------------
$Distro      = 'Ubuntu-24.04'
$Port        = 8003
$ListenAddr  = '0.0.0.0'
$HealthUrl   = "localhost:$Port/healthz"   # supervisor liveness ONLY (no device)
$MaxWaitSec  = 180                          # boot -> supervisor-reachable budget
$PollEverySec = 3
$LogDir      = 'C:\ProgramData\brain-v42'
$LogFile     = Join-Path $LogDir 'boot-embedding.log'

# --- Logging ------------------------------------------------------------------
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    try {
        if (-not (Test-Path -LiteralPath $LogDir)) {
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogFile -Value $line
    } catch {
        # Logging must never break the boot path.
    }
}

Write-Log "boot.ps1 starting (distro=$Distro port=$Port)"

# --- Step 1: instantiate the distro -------------------------------------------
# `-u root -e /bin/true` boots systemd and returns immediately; the keepalive
# unit (sleep infinity) is what actually holds the VM up afterwards.
Write-Log "instantiating WSL distro '$Distro'"
& wsl.exe -d $Distro -u root -e /bin/true
if ($LASTEXITCODE -ne 0) {
    Write-Log "wsl instantiate returned exit code $LASTEXITCODE (continuing; distro may already be running)" 'WARN'
}

# --- Step 2: wait for the supervisor to answer /healthz inside the distro -----
Write-Log "waiting for supervisor at $HealthUrl (max ${MaxWaitSec}s)"
$deadline = (Get-Date).AddSeconds($MaxWaitSec)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    # `curl -sf` -> non-zero exit on non-2xx; we only care about reachability.
    & wsl.exe -d $Distro -e curl -sf $HealthUrl | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $healthy = $true
        break
    }
    Start-Sleep -Seconds $PollEverySec
}
if (-not $healthy) {
    Write-Log "supervisor did NOT answer $HealthUrl within ${MaxWaitSec}s; refreshing portproxy anyway so a late-coming supervisor is still reachable" 'WARN'
} else {
    Write-Log "supervisor is reachable on /healthz"
}

# --- Step 3: compute the distro's current NAT IP ------------------------------
# `hostname -I` returns a space-separated list; the first token is the IPv4 we
# forward to. NAT assigns a fresh address most boots, so this must be recomputed.
$hostnameOut = (& wsl.exe -d $Distro -e hostname -I) 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($hostnameOut)) {
    Write-Log "could not obtain WSL IP via 'hostname -I' (exit=$LASTEXITCODE); aborting portproxy refresh" 'ERROR'
    exit 1
}
# `wsl.exe` output can be UTF-16LE / NUL-polluted; strip NULs before splitting,
# then validate the first token is a real dotted-quad IPv4 before handing it to
# netsh (a garbage value would create a broken portproxy rule).
$WslIp = (($hostnameOut -replace "`0", '').Trim() -split '\s+')[0]
if ([string]::IsNullOrWhiteSpace($WslIp)) {
    Write-Log "parsed empty WSL IP from '$hostnameOut'; aborting" 'ERROR'
    exit 1
}
if ($WslIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
    Write-Log "parsed WSL IP '$WslIp' is not a valid IPv4 (from '$hostnameOut'); aborting" 'ERROR'
    throw "Bad WSL IP: '$WslIp'"
}
Write-Log "WSL IP resolved to $WslIp"

# --- Step 4: refresh ONLY the :8003 portproxy rule ----------------------------
# delete-then-add makes this idempotent and re-points to the new WSL IP. We
# NEVER use `reset all` and NEVER touch the :8001/:9100 rules.
Write-Log "deleting stale :$Port portproxy rule (ignored if absent)"
& netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=$ListenAddr 2>$null | Out-Null
# Do not treat a missing-rule delete as failure.

Write-Log "adding portproxy: $ListenAddr`:$Port -> $WslIp`:$Port"
& netsh interface portproxy add v4tov4 listenport=$Port listenaddress=$ListenAddr connectport=$Port connectaddress=$WslIp
if ($LASTEXITCODE -ne 0) {
    Write-Log "netsh portproxy add failed (exit=$LASTEXITCODE)" 'ERROR'
    exit 1
}
Write-Log "portproxy rule active for :$Port"

# --- Step 5: ensure the inbound firewall allow rule for TCP 8003 exists --------
# Guard so a re-run does not stack duplicate rules. We match by our own rule
# name; only create when absent.
$FwRuleName = "brain-v42 embedding $Port (TCP in)"
$existing = & netsh advfirewall firewall show rule name="$FwRuleName" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Log "firewall rule '$FwRuleName' already present"
} else {
    Write-Log "creating firewall rule '$FwRuleName'"
    & netsh advfirewall firewall add rule name="$FwRuleName" dir=in action=allow protocol=TCP localport=$Port | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Non-fatal: portproxy may still work on a permissive network; surface it.
        Write-Log "firewall rule add returned exit=$LASTEXITCODE (continuing)" 'WARN'
    }
}

Write-Log "boot.ps1 finished OK (proxy 0.0.0.0:$Port -> $WslIp`:$Port)"
exit 0
