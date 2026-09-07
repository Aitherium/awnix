<#
.SYNOPSIS
  Rebuild → export → import → VERIFY an awnix fleet-host distro, idempotently.

.DESCRIPTION
  The Debian→awnix cutover needs this cycle run many times, and it was run three
  times BY HAND in one session before anyone wrote it down. It is four commands
  and one judgement, and the judgement is the part that keeps being skipped.

  WHY NOT bootstrap-awnix.ps1. That script covers export+import for a STRANGER
  standing up a workstation, and it is right for that. It does not unregister an
  existing distro (so a second run fails on "already exists"), and — the part that
  matters — it does not ask whether systemd came up.

  THE CHECK THAT CAN FAIL, and the reason this script exists at all: a bootc image
  runs systemd as PID 1 when BOOTED, and does NOT when imported into WSL2 — WSL
  starts its own init. Measured 2026-09-05 on the first import of aitheros-fleet:
  `pid1=init(awnix)`, `/etc/wsl.conf` absent, `systemctl is-system-running` =
  offline. Every quadlet would have been inert, and NOTHING about the import said
  so: it booted, podman 5.8.5 was present, the quadlet directory was there and
  correctly empty, systemctl was on PATH. A rehearsal that stops at "it booted"
  certifies a host that cannot run a single unit.

  So this script ends by asserting what PID 1 actually is, and exits non-zero when
  it is not systemd. Acting without re-asserting has only proven that the commands
  ran.

.EXAMPLE
  .\rehearse-awnix.ps1                     # export current image, import, verify
  .\rehearse-awnix.ps1 -Build              # rebuild the image first
  .\rehearse-awnix.ps1 -Name awnix-test    # a throwaway alongside the real one
  .\rehearse-awnix.ps1 -SelfTest
#>
[CmdletBinding()]
param(
    [string]$Image = 'localhost/aitheros-fleet:latest',
    [string]$Name  = 'awnix',
    [string]$Distro = 'Debian',
    [switch]$Build,
    # Run ONLY the assertion against an already-imported distro. The identity probe
    # (pid1 / podman / unit count) was run standalone three times in one session
    # while diagnosing why quadlets would not run -- so it is a mode of this script
    # rather than a second script, because a rival that answers the same question
    # slightly differently is how two tools come to disagree about one host.
    [switch]$VerifyOnly,
    [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
function Say  { param([string]$m) Write-Host "  $m" }
function Fail { param([string]$m) Write-Host "  FAIL: $m" -ForegroundColor Red; exit 1 }
# Exit 2 is "could not judge", and it is a DIFFERENT answer from exit 1. Collapsing
# them is how "the checker is broken" gets filed as "the host is broken".
function Dead { param([string]$m) Write-Host "  CANNOT JUDGE: $m" -ForegroundColor Yellow; exit 2 }

# wsl.exe emits UTF-16 with NULs and, when its exec path is wedged, prints
# "Catastrophic failure" while EXITING 0. Trusting the exit code reads a wedged
# host as success, so every call below is judged on OUTPUT.
function Wsl-Text { param([string[]]$Args)
    $raw = & wsl.exe @Args 2>&1 | Out-String
    return ($raw -replace "`0", '')
}
function Wedged { param([string]$t) return ($t -match 'atastrophic failure|E_UNEXPECTED') }

if ($SelfTest) {
    $ok = $true
    if (-not (Test-Path (Join-Path $Here 'awnix-to-wsl.sh'))) { Write-Host '  SELFTEST: awnix-to-wsl.sh missing'; $ok = $false }
    if (-not (Test-Path (Join-Path $Here 'Containerfile.aitheros-fleet'))) { Write-Host '  SELFTEST: Containerfile.aitheros-fleet missing'; $ok = $false }
    # The wedge detector must fire on the real string, and not on ordinary output.
    if (-not (Wedged 'Catastrophic failure')) { Write-Host '  SELFTEST: wedge detector missed the real string'; $ok = $false }
    if (Wedged 'pid1=systemd')                { Write-Host '  SELFTEST: wedge detector cried wolf'; $ok = $false }
    Write-Host ("  SELF-TEST: " + $(if ($ok) { 'PASS' } else { 'FAIL' }))
    exit $(if ($ok) { 0 } else { 1 })
}

function Assert-FleetHost {
    param([string]$DistroName)
    Say "verifying '$DistroName' (systemd must be PID 1, or no quadlet can run)"
    $v = Wsl-Text @('-d', $DistroName, '--', '/bin/sh', '-c',
        'echo pid1=$(ps -p 1 -o comm=); echo podman=$(command -v podman >/dev/null && echo yes || echo no); echo units=$(ls -1 /etc/containers/systemd 2>/dev/null | wc -l)')
    if (Wedged $v) { Dead "'$DistroName' will not start (E_UNEXPECTED)" }
    # AN EMPTY PROBE IS NOT A FAILED PROBE, and this script got that wrong first.
    # When WSL's exec path is wedged the output can come back EMPTY rather than
    # carrying the "Catastrophic failure" string, and the checks below then read a
    # missing `pid1=` line as "PID 1 is not systemd" — blaming the IMAGE for a
    # wedged HOST. Measured 2026-09-05: it printed exactly that while /etc/wsl.conf
    # could not even be read. "I could not look" is a third answer, and a rehearsal
    # that cannot tell it from "this host is wrong" sends the next person to fix
    # the wrong thing.
    if ($v -notmatch 'pid1=') { Dead "no probe output from '$DistroName' - could not judge" }
    Say ($v.Trim() -replace "`r?`n", ' | ')
    if ($v -notmatch 'pid1=systemd') { Fail 'PID 1 is not systemd - every quadlet would be inert. Check /etc/wsl.conf in the image.' }
    if ($v -notmatch 'podman=yes')   { Fail 'podman is absent - this is not a fleet host' }
    Say 'OK: systemd is PID 1 and podman is present'
}

if ($VerifyOnly) { Assert-FleetHost -DistroName $Name; exit 0 }

if ($Build) {
    Say "building $Image"
    # --dns: build containers inherit the host's Tailscale-only resolver and cannot
    # reach it, so public names fail while the fleet's own containers resolve fine.
    $b = Wsl-Text @('-d', $Distro, '-u', 'root', 'sh', '-c',
        "cd /mnt/c/AitherOS-Fresh/.DEPLOYMENT/standalone/bootc && podman build --dns 10.89.0.1 -t $Image -f Containerfile.aitheros-fleet . 2>&1 | tail -3")
    if (Wedged $b) { Fail 'WSL is wedged (E_UNEXPECTED); nothing was changed' }
    if ($b -notmatch 'Successfully tagged') { Say $b; Fail 'build did not tag an image' }
    Say 'built'
}

# Idempotent: a previous rehearsal must not make this one fail. Unregistering a
# distro whose only content came from an image is not destructive -- the image is
# the source of truth, which is the whole property the cutover is buying.
$existing = Wsl-Text @('-l', '-q')
if ($existing -split "`r?`n" | Where-Object { $_.Trim() -eq $Name }) {
    Say "unregistering existing '$Name'"
    $null = Wsl-Text @('--unregister', $Name)
}

Say "exporting $Image"
$e = Wsl-Text @('-d', $Distro, '-u', 'root', 'sh', '-c',
    "cd /mnt/c/AitherOS-Fresh/.DEPLOYMENT/standalone/bootc && sh awnix-to-wsl.sh --image '$Image' --name '$Name'")
if (Wedged $e) { Fail 'WSL is wedged (E_UNEXPECTED) during export' }
if ($e -notmatch 'exported (\d+) bytes') { Say $e; Fail 'export did not report a byte count' }
Say ("exported " + $Matches[1] + " bytes")

$tar = "C:/AitherOS-Data/wsl/$Name-rootfs.tar"
$dir = "C:/AitherOS-Data/wsl/$Name"
if (-not (Test-Path $tar)) { Fail "the export produced no tarball at $tar" }

Say "importing as '$Name'"
& wsl.exe --import $Name $dir $tar --version 2 | Out-Null

# ── THE ASSERTION. Everything above only proves commands ran. ────────────────
Assert-FleetHost -DistroName $Name
exit 0
