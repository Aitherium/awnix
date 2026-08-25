<#
.SYNOPSIS
  One command to get awnix (or the GobboNet appliance) running on Windows.

.DESCRIPTION
  Ensures PowerShell 7 is present, then picks a lane: a container, a WSL2 distro,
  or a bootable ISO. It reimplements none of them -- awnix-to-wsl.sh and
  assemble-awnix-iso.ps1 already exist and are self-tested.

  Windows Powershell 5.1 can run this file; that is the point. It bootstraps pwsh
  7 rather than requiring it, because a kit that needs the thing it installs is
  not a bootstrap.

.EXAMPLE
  .\bootstrap-awnix.ps1                       # container, the appliance
  .\bootstrap-awnix.ps1 -Target wsl
  .\bootstrap-awnix.ps1 -Target iso
  .\bootstrap-awnix.ps1 -SelfTest
#>
[CmdletBinding()]
param(
    [ValidateSet('container','wsl','iso')]
    [string]$Target = 'container',
    [string]$Image  = $(if ($env:AWNIX_IMAGE) { $env:AWNIX_IMAGE } else { 'ghcr.io/aitherium/gobbonet-appliance:latest' }),
    [string]$Name   = 'awnix',
    [int]$Port      = 8080,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$Fallback = 'localhost/gobbonet-appliance:latest'

function Say { param([string]$m) Write-Host "  $m" }

# ── the guard that exists because this class already shipped ───────────────────
# Three of four advertised installer one-liners once served a 56 KB web PAGE with
# HTTP 200. `Invoke-WebRequest` does not treat a 200 as an error any more than
# curl does, so the published command fed HTML to a shell. Anything fetched here
# is judged on how it STARTS -- not on whether a tag appears somewhere, which
# would refuse a real script whose comment mentions chat.html.
function Test-LooksLikeHtml {
    param([Parameter(Mandatory)][string]$Path)
    $first = (Get-Content -LiteralPath $Path -TotalCount 40 -ErrorAction SilentlyContinue |
              Where-Object { $_.Trim() -ne '' } | Select-Object -First 1)
    if (-not $first) { return $false }
    $f = $first.Trim().ToLowerInvariant()
    if ($f.StartsWith('#!') -or $f.StartsWith('<#') -or $f.StartsWith('#')) { return $false }
    return ($f.StartsWith('<!doctype html') -or $f.StartsWith('<html') -or $f.StartsWith('<head'))
}

function Get-Verified {
    param([Parameter(Mandatory)][string]$Uri, [Parameter(Mandatory)][string]$OutFile,
          [string]$What = 'the file')
    Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
    if (-not (Test-Path $OutFile) -or (Get-Item $OutFile).Length -eq 0) {
        throw "$What downloaded as an EMPTY file from $Uri"
    }
    if (Test-LooksLikeHtml -Path $OutFile) {
        Remove-Item $OutFile -Force
        throw "$Uri served an HTML PAGE, not $What. A 200 with a web page in it is exactly the failure this checks for."
    }
}

function Ensure-Pwsh7 {
    $p = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($p) { Say "PowerShell 7 present ($($p.Source))"; return $true }
    Say 'PowerShell 7 not found — installing'
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Microsoft.PowerShell --source winget --accept-package-agreements --accept-source-agreements -h | Out-Null
    } else {
        Say 'winget is unavailable; install PowerShell 7 from https://aka.ms/powershell-release and re-run'
        return $false
    }
    return [bool](Get-Command pwsh -ErrorAction SilentlyContinue)
}

function Invoke-SelfTest {
    # $script: on BOTH, deliberately. Written as `$fail = 0` here with
    # `$script:fail++` in Ck, the two were DIFFERENT variables: failures printed
    # and the function still returned 0, so the self-test could report FAIL and
    # exit 0 -- a gate that cannot fail. Found by mutating the detector and
    # noticing the exit code did not move.
    $script:fail = 0
    function Ck([string]$n, [bool]$c) { if ($c) { Write-Host "  ok   $n" } else { Write-Host "  FAIL $n"; $script:fail++ } }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("awnixbs-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $h = Join-Path $tmp 'page.html'
        "<!DOCTYPE html>`n<html><head><title>hi</title></head></html>" | Set-Content -LiteralPath $h
        Ck 'an HTML page is recognised' (Test-LooksLikeHtml -Path $h)

        $s = Join-Path $tmp 'real.ps1'
        "<#`n.SYNOPSIS`n  real`n#>`nWrite-Host 'hello'" | Set-Content -LiteralPath $s
        Ck 'a real script is not called HTML' (-not (Test-LooksLikeHtml -Path $s))

        # The false positive its sh twin actually hit: a script MENTIONING html.
        $m = Join-Path $tmp 'mentions.ps1'
        "# serves chat.html and <html> pages`nWrite-Host 'ok'" | Set-Content -LiteralPath $m
        Ck 'a script mentioning html still passes' (-not (Test-LooksLikeHtml -Path $m))

        $e = Join-Path $tmp 'empty.html'
        "`n`n<!DOCTYPE html>" | Set-Content -LiteralPath $e
        Ck 'leading blank lines do not hide an HTML page' (Test-LooksLikeHtml -Path $e)

        Ck 'the target is one this script knows' ($Target -in @('container','wsl','iso'))
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    Write-Host ''
    if ($script:fail -eq 0) { Write-Host 'SELF-TEST PASS'; return 0 }
    Write-Host "SELF-TEST FAILED ($script:fail)"; return $script:fail
}

if ($SelfTest) { exit (Invoke-SelfTest) }

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Ensure-Pwsh7)) { exit 1 }

switch ($Target) {
    'container' {
        $engine = if (Get-Command podman -ErrorAction SilentlyContinue) { 'podman' }
                  elseif (Get-Command docker -ErrorAction SilentlyContinue) { 'docker' }
                  else { $null }
        if (-not $engine) { throw 'no podman or docker on PATH — install one, or use -Target wsl' }

        $img = $Image
        & $engine image inspect $Image *> $null
        if ($LASTEXITCODE -ne 0) {
            Say "pulling $Image ..."
            & $engine pull $Image *> $null
            if ($LASTEXITCODE -ne 0) {
                & $engine image inspect $Fallback *> $null
                if ($LASTEXITCODE -eq 0) { Say "$Image is not published yet — using the local $Fallback"; $img = $Fallback }
                else { throw "could not obtain $Image (and no local build to fall back on)" }
            }
        }

        & $engine rm -f $Name *> $null
        & $engine run -d --name $Name -p "${Port}:8080" $img adk gobbonet --ui /opt/gobbonet --port 8080 --host 0.0.0.0 --no-open *> $null
        if ($LASTEXITCODE -ne 0) { throw 'the container did not start' }

        for ($i = 0; $i -lt 40; $i++) {
            try {
                Invoke-WebRequest -Uri "http://127.0.0.1:$Port/chat.html" -UseBasicParsing -TimeoutSec 3 | Out-Null
                Say "GobboNet is up: http://127.0.0.1:$Port/chat.html"; exit 0
            } catch { Start-Sleep -Seconds 2 }
        }
        & $engine logs $Name 2>&1 | Select-Object -Last 20 | Write-Host
        throw 'it started but never served chat.html'
    }
    'wsl' {
        # TWO HALVES, EACH WHERE ITS COMMANDS EXIST.
        #
        # The export needs podman, so it runs inside the distro. The import needs
        # wsl.exe, which CANNOT be invoked from inside a distro -- awnix-to-wsl.sh
        # says so and refuses rather than pretending.
        #
        # The first version of this branch handed the whole job to that script
        # over `wsl.exe -u root -- sh -c ...`, i.e. ran it inside. Measured
        # 2026-08-23: it staged a 4.3 GB tarball, registered NO distro, and
        # exited 0 -- a caller saw a clean run and an absent distro.
        $sh = Join-Path $Here 'awnix-to-wsl.sh'
        if (-not (Test-Path $sh)) { throw "awnix-to-wsl.sh is not beside this script" }

        $inside = ($Here -replace '\\','/') -replace '^([A-Za-z]):', { "/mnt/" + $_.Groups[1].Value.ToLower() }
        Say "exporting $Image inside WSL"
        & wsl.exe -u root -- sh -c "cd '$inside' && sh awnix-to-wsl.sh --image '$Image' --name '$Name'"
        if ($LASTEXITCODE -ne 0) { throw "the export step failed (exit $LASTEXITCODE)" }

        # awnix-to-wsl.sh stages here; keep the two in step or the import points
        # at a tarball that is not there.
        $tar = "C:/AitherOS-Data/wsl/$Name-rootfs.tar"
        $dir = "C:/AitherOS-Data/wsl/$Name"
        if (-not (Test-Path $tar)) { throw "the export produced no tarball at $tar" }

        Say "importing as WSL2 distro '$Name'"
        & wsl.exe --import $Name $dir $tar --version 2
        if ($LASTEXITCODE -ne 0) { throw "wsl --import failed (exit $LASTEXITCODE)" }

        # VERIFY, because "--import returned 0" and "the distro runs" are
        # different facts. `wsl -l -q` emits UTF-16 with NULs; strip them or the
        # match silently never succeeds.
        $listed = (& wsl.exe -l -q) -replace "`0","" | ForEach-Object { $_.Trim() }
        if ($listed -notcontains $Name) { throw "$Name imported but is not listed by wsl -l -q" }
        Say "registered. Try:  wsl -d $Name -- adk gobbonet --ui /opt/gobbonet"
        Say "undo with:        wsl --unregister $Name"
        exit 0
    }
    'iso' {
        $ps = Join-Path $Here 'assemble-awnix-iso.ps1'
        if (-not (Test-Path $ps)) { throw "assemble-awnix-iso.ps1 is not beside this script" }
        Say 'handing over to assemble-awnix-iso.ps1'
        & $ps
        exit $LASTEXITCODE
    }
}
