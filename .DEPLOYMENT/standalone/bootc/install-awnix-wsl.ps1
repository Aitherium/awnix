<#
.SYNOPSIS
  Run awnix as a WSL2 distro on this Windows machine -- one line, no account.

.DESCRIPTION
  The self-service lane for a machine you are not signed in on. Everything this
  script touches is public: the image is pulled anonymously from ghcr.io, the
  tarball is built locally, and nothing is sent anywhere.

      irm https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-awnix-wsl.ps1 | iex

  Three stages, each where its commands exist:

    1. EXPORT a rootfs tarball from the public container image -- via Docker
       (Docker Desktop) when it is installed and answering, else via podman
       inside a WSL distro (installed into that distro the first time when no
       distro has it).
    2. IMPORT the tarball with `wsl --import` as a WSL2 distro named `awnix`.
    3. Make PID 1 systemd (/etc/wsl.conf, [boot] systemd=true), restart and
       VERIFY /proc/1/comm. The public images bake no wsl.conf -- without this
       step the distro starts but runs no services.

  Defaults: the awnix-ai-full image (the whole aw* stack; the model sized to
  this machine downloads on first boot). Pass -Image for awnix or awnix-full,
  or any dated tag instead of :latest. Needs ~2 GB of download and ~10 GB free.

  On a machine whose live fleet runs in a WSL distro, use bootstrap-awnix.ps1
  -Target wsl beside this script instead -- it carries the fleet refusals.
  This script refuses to touch any distro other than -Name, and -Force is
  required before it replaces an existing one.

.EXAMPLE
  irm https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-awnix-wsl.ps1 | iex
  .\install-awnix-wsl.ps1 -Image ghcr.io/aitherium/awnix-full:2026.10.05
  .\install-awnix-wsl.ps1 -Name myawnix -StageDir D:\awnix -Force
  .\install-awnix-wsl.ps1 -SelfTest

.NOTES
  The export runs where a container engine already is; the import runs where
  wsl.exe is. This script performs both and verifies the result -- "import
  returned 0" and "the distro boots systemd" are different facts.
#>
[CmdletBinding()]
param(
    [string]$Name = 'awnix',
    [string]$Image = 'ghcr.io/aitherium/awnix-ai-full:latest',
    [string]$StageDir = $(if ($env:AWNIX_WSL_STAGE) { $env:AWNIX_WSL_STAGE } else { Join-Path $env:LOCALAPPDATA 'awnix\wsl' }),
    [string]$ExportDistro = '',
    [switch]$Force,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
$MinFreeBytes = [int64](10GB)   # download + tarball + import growth
$MinTarBytes  = [int64](50MB)   # a rootfs smaller than this is not an OS

function Say  { param([string]$m) Write-Host "  $m" }
function Die  { param([string]$m) throw "install-awnix-wsl: $m" }

# -- pure helpers (self-tested; no WSL, no I/O) -------------------------------

function Test-AwnixWslName {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    return [bool]($Candidate -match '^[A-Za-z0-9][A-Za-z0-9._-]*$')
}

function ConvertTo-WslPath {
    <# E:\x\y -> /mnt/e/x/y #>
    param([string]$Path)
    $p = $Path -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)$') { return "/mnt/$($Matches[1].ToLower())$($Matches[2])" }
    return $p
}

function ConvertFrom-WslList {
    <# `wsl -l -v` text (NULs already stripped) -> name -> @{State;Version}. Pure. #>
    param([string]$Text)
    $out = @{}
    foreach ($line in ($Text -split "`r?`n")) {
        $t = $line.Trim().TrimStart('*').Trim()
        if (-not $t -or $t -match '^NAME\s+STATE') { continue }
        $p = $t -split '\s+'
        if ($p.Count -ge 3) { $out[$p[0]] = @{ State = $p[1]; Version = $p[2] } }
    }
    return $out
}

function Merge-IniKey {
    <#
      Set [Section] Key=Value keeping every other line (an existing
      /etc/wsl.conf is hand-edited and must stay that way). Replaces the key in
      place when present, appends at the end of the section when absent, adds
      the section when missing. Idempotent. Pure.
    #>
    param([string]$Text, [string]$Section, [string]$Key, [string]$Value)
    $lines = @()
    if ($Text) { $lines = ($Text -replace "`r", '') -split "`n" }
    $out = New-Object System.Collections.Generic.List[string]
    $inSec = $false; $done = $false; $seenSec = $false
    foreach ($ln in $lines) {
        if ($ln -match '^\s*\[([^\]]+)\]\s*$') {
            if ($inSec -and -not $done) { $out.Add("$Key=$Value"); $done = $true }
            $inSec = ($Matches[1].Trim() -eq $Section)
            if ($inSec) { $seenSec = $true }
        } elseif ($inSec -and (-not $done) -and ($ln -match ('^\s*' + [regex]::Escape($Key) + '\s*='))) {
            $out.Add("$Key=$Value"); $done = $true; continue
        }
        $out.Add($ln)
    }
    if ($inSec -and -not $done) { $out.Add("$Key=$Value") }
    if (-not $seenSec) {
        if ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -ne '') { $out.Add('') }
        $out.Add("[$Section]"); $out.Add("$Key=$Value")
    }
    return ($out -join "`n")
}

function Test-FreeSpacePure {
    <# $null when $FreeBytes covers $NeedBytes plus a 2 GiB margin; else the reason. #>
    param([int64]$FreeBytes, [int64]$NeedBytes, [string]$Where)
    if ($FreeBytes -lt 0) { return "could not read free space for $Where" }
    if ($FreeBytes -lt ($NeedBytes + 2GB)) {
        return ("{0} has {1:N1} GB free; this lane needs about {2:N0} GB. Pass -StageDir on a roomier drive." -f $Where, ($FreeBytes / 1GB), (($NeedBytes + 2GB) / 1GB))
    }
    return $null
}

function Get-DriveFreeBytes {
    param([string]$Path)
    if ($Path -notmatch '^([A-Za-z]):') { return -1 }
    try { return [int64](Get-PSDrive -Name $Matches[1] -ErrorAction Stop).Free } catch { return -1 }
}

# -- live helpers -------------------------------------------------------------

function Get-WslListRaw {
    <# A listing that cannot be read is a refusal: "could not look" is not "clear". #>
    $raw = (& wsl.exe -l -v 2>&1 | Out-String) -replace "`0", ''
    if ($raw -match 'atastrophic failure|E_UNEXPECTED' -or -not $raw.Trim()) {
        Die "could not read 'wsl -l -v' (a wedged WSL returns an empty list). Run 'wsl --shutdown' and retry."
    }
    return $raw
}

function New-RootfsTarViaDocker {
    <# $true when Docker (Desktop) exported the tarball; $false to fall through. #>
    param([string]$Image, [string]$Tar)
    if (-not (Get-Command docker.exe -ErrorAction SilentlyContinue)) { return $false }
    & docker.exe version --format '{{.Server.Version}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        Say 'Docker is on PATH but its daemon is not answering - using the WSL podman lane instead'
        return $false
    }
    Say "pulling $Image (Docker)"
    & docker.exe pull $Image
    if ($LASTEXITCODE -ne 0) { Die "docker pull failed for $Image" }
    $cid = (& docker.exe create $Image).Trim()
    if (-not $cid) { Die 'docker create produced no container id' }
    try {
        Say "exporting rootfs to $Tar (this is the slow part)"
        if (Test-Path -LiteralPath $Tar) { Remove-Item -LiteralPath $Tar -Force }
        & docker.exe export $cid -o $Tar
        if ($LASTEXITCODE -ne 0) { Die 'docker export failed' }
    } finally {
        & docker.exe rm $cid *> $null
    }
    return $true
}

function New-RootfsTarViaWsl {
    <# podman inside a WSL distro. The sh script travels base64-encoded so no
       quoting layer (PowerShell 5.1, wsl.exe, sh) can corrupt it. #>
    param([string]$Image, [string]$Tar, [string]$ExportDistro, [string]$DistroListRaw)
    $list = ConvertFrom-WslList $DistroListRaw
    $d = $ExportDistro
    if (-not $d) {
        foreach ($line in ($DistroListRaw -split "`r?`n")) {
            if ($line.TrimStart().StartsWith('*')) { $d = (($line.Trim().TrimStart('*').Trim()) -split '\s+')[0]; break }
        }
    }
    if (-not $d -or -not $list.ContainsKey($d)) {
        Die "no WSL distro to export from - pass -ExportDistro <name> (one that has, or can install, podman), or install Docker Desktop"
    }
    Say "starting '$d' to export in"
    & wsl.exe -d $d -u root -- true *> $null
    $tarWsl = ConvertTo-WslPath $Tar
    $sh = @'
set -e
if ! command -v podman >/dev/null 2>&1; then
  echo "  installing podman in this distro (first time only)"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq podman
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q podman
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache podman
  else
    echo "  no known package manager in this distro - install podman inside it, or Docker Desktop on Windows, and re-run" >&2
    exit 1
  fi
fi
echo "  pulling __IMAGE__"
podman pull __IMAGE__
cid=$(podman create __IMAGE__)
rm -f __TAR__
echo "  exporting rootfs (this is the slow part)"
podman export $cid -o __TAR__
podman rm $cid >/dev/null
'@
    $sh = $sh.Replace('__IMAGE__', $Image).Replace('__TAR__', $tarWsl)
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sh))
    & wsl.exe -d $d -u root -- sh -c "echo $b64 | base64 -d | sh"
    if ($LASTEXITCODE -ne 0) { Die "the export inside '$d' failed (exit $LASTEXITCODE)" }
}

function Install-WslDistro {
    param([string]$Name, [string]$StageDir, [string]$Tar)
    Say "importing as WSL2 distro '$Name'"
    & wsl.exe --import $Name (Join-Path $StageDir $Name) $Tar --version 2
    if ($LASTEXITCODE -ne 0) { Die "wsl --import failed (exit $LASTEXITCODE)" }
    # VERIFY: "import returned 0" and "the distro is registered" are different facts.
    $listed = (& wsl.exe -l -q 2>&1) -replace "`0", ''
    $names = @($listed | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($names -notcontains $Name) { Die "'$Name' imported but is not listed by wsl -l -q" }
}

function Set-DistroWslConf {
    <# Merge the three keys the lane needs into /etc/wsl.conf, preserving any
       hand-edited content. Returns $true when the file changed. #>
    param([string]$Name)
    $cur = ''
    try { $cur = ((& wsl.exe -d $Name -u root -- cat /etc/wsl.conf 2>$null) | Out-String) } catch { $cur = '' }
    $cur = $cur -replace "`r", ''
    $new = Merge-IniKey $cur 'boot' 'systemd' 'true'
    $new = Merge-IniKey $new 'user' 'default' 'root'
    $new = Merge-IniKey $new 'interop' 'appendWindowsPath' 'false'
    if ($new.TrimEnd("`n") -eq $cur.TrimEnd("`n")) { return $false }
    Say 'writing /etc/wsl.conf ([boot] systemd=true, [user] default=root)'
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('awnixwsl-' + [Guid]::NewGuid().ToString('N') + '.conf')
    [IO.File]::WriteAllText($tmp, $new)
    try {
        & wsl.exe -d $Name -u root -- cp (ConvertTo-WslPath $tmp) /etc/wsl.conf
        if ($LASTEXITCODE -ne 0) { Die 'could not write /etc/wsl.conf inside the distro' }
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
    return $true
}

function Confirm-Systemd {
    <# Restart the distro and prove systemd is PID 1 -- the only fact that says
       the services (and first-boot setup) will actually run. #>
    param([string]$Name)
    & wsl.exe --terminate $Name *> $null
    for ($i = 0; $i -lt 20; $i++) {
        $comm = ((& wsl.exe -d $Name -u root -- cat /proc/1/comm 2>$null) | Out-String).Trim()
        if ($comm -eq 'systemd') { return $true }
        Start-Sleep -Seconds 2
    }
    $comm = ((& wsl.exe -d $Name -u root -- cat /proc/1/comm 2>$null) | Out-String).Trim()
    $conf = ((& wsl.exe -d $Name -u root -- cat /etc/wsl.conf 2>$null) | Out-String).Trim()
    Write-Host "  /proc/1/comm: '$comm'"
    Write-Host '  /etc/wsl.conf:'
    Write-Host $conf
    return $false
}

# -- self-test (pure; touches neither WSL nor the filesystem) -----------------

function Invoke-SelfTest {
    $script:fail = 0
    function Ck([string]$n, [bool]$c) { if ($c) { Write-Host "  ok   $n" } else { Write-Host "  FAIL $n"; $script:fail++ } }

    $t = Merge-IniKey '' 'boot' 'systemd' 'true'
    Ck 'boot section added when absent' ($t -eq "[boot]`nsystemd=true")
    $t2 = Merge-IniKey $t 'user' 'default' 'root'
    Ck 'existing section preserved' ($t2 -match 'systemd=true')
    Ck 'separator blank line added before new section' ($t2 -match "systemd=true`n`n\[user\]")
    $t3 = Merge-IniKey "[boot]`nsystemd=false`n# keep me" 'boot' 'systemd' 'true'
    Ck 'value replaced in place' ($t3 -match "^\[boot\]`nsystemd=true")
    Ck 'hand-written lines preserved' ($t3 -match '# keep me')
    $t4 = Merge-IniKey $t3 'boot' 'systemd' 'true'
    Ck 'merge is idempotent' ($t4 -eq $t3)
    $t5 = Merge-IniKey "[boot]`nsystemd=true`n[user]`ndefault=someone" 'boot' 'systemd' 'true'
    Ck 'later sections untouched' ($t5 -match 'default=someone')

    $lv = ConvertFrom-WslList "  NAME      STATE           VERSION`n* awnix     Running         2`n  Ubuntu    Stopped         2`n"
    Ck 'wsl list parses the default row' ($lv['awnix'].State -eq 'Running')
    Ck 'wsl list parses other rows' ($lv['Ubuntu'].State -eq 'Stopped')

    Ck 'windows path maps to /mnt form' ((ConvertTo-WslPath 'C:\Users\x\awnix') -eq '/mnt/c/Users/x/awnix')
    Ck 'good distro names pass' ((Test-AwnixWslName 'awnix') -and (Test-AwnixWslName 'awnix-wsl'))
    Ck 'bad distro names refused' ((-not (Test-AwnixWslName '')) -and (-not (Test-AwnixWslName 'a b')) -and (-not (Test-AwnixWslName '-x')))

    Ck '3.8 GB free refuses a 10 GB need' ([bool](Test-FreeSpacePure -FreeBytes ([int64](3.8 * 1GB)) -NeedBytes ([int64](10GB)) -Where 'C:'))
    Ck '220 GB free admits a 10 GB need' ($null -eq (Test-FreeSpacePure -FreeBytes ([int64](220GB)) -NeedBytes ([int64](10GB)) -Where 'E:'))
    Ck 'unknown free space refuses' ([bool](Test-FreeSpacePure -FreeBytes -1 -NeedBytes 1 -Where 'X:'))

    Write-Host ''
    if ($script:fail -eq 0) { Write-Host 'SELF-TEST PASS'; return 0 }
    Write-Host "SELF-TEST FAILED ($script:fail)"; return $script:fail
}

if ($SelfTest) { exit (Invoke-SelfTest) }

# -- the install --------------------------------------------------------------

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Die 'WSL is not installed on this machine (wsl.exe not found). Run:  wsl --install'
}
if (-not (Test-AwnixWslName $Name)) { Die "invalid -Name '$Name' (letters, digits, dot, dash, underscore)" }

Say "awnix in WSL2 as '$Name', from $Image"
$raw = Get-WslListRaw
$list = ConvertFrom-WslList $raw

if ($list.ContainsKey($Name)) {
    if (-not $Force) {
        Die "a WSL distro named '$Name' already exists. Pass -Force to REPLACE IT (only '$Name' - your other distros are untouched), or -Name <other>."
    }
    Say "replacing existing distro '$Name' (-Force)"
    & wsl.exe --terminate $Name *> $null
    & wsl.exe --unregister $Name
    if ($LASTEXITCODE -ne 0) { Die "wsl --unregister $Name failed" }
}

$why = Test-FreeSpacePure -FreeBytes (Get-DriveFreeBytes $StageDir) -NeedBytes $MinFreeBytes -Where $StageDir
if ($why) { Die $why }
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
$tar = Join-Path $StageDir "$Name-rootfs.tar"
if (Test-Path -LiteralPath $tar) { Remove-Item -LiteralPath $tar -Force }

$exported = $false
if (-not $ExportDistro) {
    try { $exported = New-RootfsTarViaDocker -Image $Image -Tar $tar }
    catch { Say "Docker lane failed: $($_.Exception.Message) - trying the WSL podman lane"; $exported = $false }
}
if (-not $exported) { New-RootfsTarViaWsl -Image $Image -Tar $tar -ExportDistro $ExportDistro -DistroListRaw $raw }

if (-not (Test-Path -LiteralPath $tar)) { Die "no tarball at $tar after the export - refusing to import nothing" }
$size = (Get-Item -LiteralPath $tar).Length
if ($size -lt $MinTarBytes) { Die "the exported tarball is only $size bytes - that is not a rootfs" }
Say ('exported {0:N2} GB' -f ($size / 1GB))

Install-WslDistro -Name $Name -StageDir $StageDir -Tar $tar
Set-DistroWslConf -Name $Name | Out-Null
if (-not (Confirm-Systemd -Name $Name)) {
    Die 'imported, but PID 1 is not systemd after the wsl.conf write - copy the output above into an issue'
}

Say ''
Say "OK - '$Name' is up and systemd is PID 1. No account was used anywhere in this."
Say "next:  wsl -d $Name"
Say 'first boot runs awnix-setup: hostname / extra aw* components / link-to-account - all three skippable'
