<#
.SYNOPSIS
  Install Aither on a Windows PC and connect it to your Aitherium account -- one line.

.DESCRIPTION
  The installer for a person who has never opened a terminal. Press Windows+R, paste
  the line below, press Enter:

      powershell -ep Bypass -c "irm https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-aither-windows.ps1|iex"

  or double-click Install-Aither.cmd, which runs the same line.

  What it does, in the order the person sees it:

    1. CHECK      Windows 10 2004 (build 19041) or later, 64-bit Intel/AMD, hardware
                  virtualization on, enough free disk. Opens the Aitherium page in the
                  browser straight away, so the first thing made does not wait for the
                  download (the platform does the heavy work, not this PC).
    2. WSL        if Windows Subsystem for Linux is missing: `wsl --install
                  --no-distribution` (one Windows "allow changes?" prompt), then ONE
                  restart. A RunOnce entry brings this installer back after sign-in.
    3. DOWNLOAD   the prebuilt awnix root filesystem from the newest
                  awnix-wsl-<edition>-<date> release on Aitherium/awnix: its parts,
                  each sha256-checked, resumed with HTTP Range after a dropped
                  connection, then joined and checked again as a whole.
    4. INSTALL    `wsl --import` as distro 'awnix', systemd as PID 1 (/etc/wsl.conf),
                  verified by reading /proc/1/comm.
    5. CONNECT    `awnix-setup --link --json` inside the distro: the existing device
                  code link (the same flow the awnix tty and web console run). The
                  sign-in page opens in the default browser with the code filled in;
                  the bearer lands in /etc/awnix/link.token inside the distro and is
                  never shown or sent anywhere else.

  Every step prints how long it took; the times are saved to install-times.json next
  to the download, so a screen recording carries measured numbers.

  -DryRun (or -WhatIf) runs no wsl.exe, downloads nothing big, changes nothing: it
  checks this PC, reads the release manifest and prints every action it WOULD take.
  -SelfTest runs the pure checks (no network, no WSL, no files).
  -DownloadOnly checks this PC and does the real download + join, then stops: it never
  runs wsl.exe, so it is safe on a machine whose own services live in WSL.

.NOTES
  The distro this installs is the person's own. It is not an AitherOS fleet host, so
  it is reached directly through one function (Invoke-InDistro), never through the
  fleet transport. Every message that ends the installer says what to do next.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('base', 'full', 'ai-full')]
    [string]$Edition = 'base',
    [string]$Name = 'awnix',
    [string]$InstallDir = '',
    [string]$ReleaseRepo = 'Aitherium/awnix',
    [string]$ManifestUrl = '',
    [string]$ScriptUrl = 'https://raw.githubusercontent.com/Aitherium/awnix/main/.DEPLOYMENT/standalone/bootc/install-aither-windows.ps1',
    [string]$StartUrl = '',
    [switch]$Resume,
    [switch]$DryRun,
    [switch]$NoBrowser,
    [switch]$DownloadOnly,
    [switch]$SelfTest,
    [ValidateSet('', 'missing', 'ready', 'pending-reboot')]
    [string]$SimulateWsl = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'Continue'
if ($WhatIfPreference) { $DryRun = $true }
if (-not $InstallDir) {
    $InstallDir = if ($env:AITHER_INSTALL_DIR) { $env:AITHER_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'Aither' }
}
if (-not $StartUrl) {
    $StartUrl = if ($env:AITHER_START_URL) { $env:AITHER_START_URL } else { 'https://aitherium.com/studio?mode=media&view=video' }
}
$script:UA = 'aither-windows-installer/1'
$script:MinBuild = 19041
$script:NeedFreeBytes = [int64](12GB)
$script:RunOnceKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
$script:RunOnceName = 'AitherInstall'
$script:Times = [ordered]@{}
$script:Steps = @('Checking this computer', 'Turning on Windows Subsystem for Linux',
                  'Downloading Aither', 'Installing Aither', 'Connecting to your Aitherium account')
# Share of the whole bar each step owns; the download dominates every real install.
$script:Weights = @(2, 8, 70, 15, 5)

# ===== pure helpers (self-tested; no WSL, no network, no files) ==============

function Get-OverallPercent {
    <# Step index (0-based) + fraction done within it -> whole-install percent, 0-100. #>
    param([int]$Step, [double]$Fraction)
    if ($Fraction -lt 0) { $Fraction = 0 }
    if ($Fraction -gt 1) { $Fraction = 1 }
    $before = 0
    for ($i = 0; $i -lt $Step -and $i -lt $script:Weights.Count; $i++) { $before += $script:Weights[$i] }
    $mine = 0
    if ($Step -lt $script:Weights.Count) { $mine = $script:Weights[$Step] }
    $total = 0
    foreach ($w in $script:Weights) { $total += $w }
    return [int][math]::Floor((($before + $mine * $Fraction) / $total) * 100)
}

function Format-Bytes {
    param([double]$Bytes)
    $ci = [Globalization.CultureInfo]::InvariantCulture
    if ($Bytes -ge 1GB) { return [string]::Format($ci, '{0:N1} GB', ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return [string]::Format($ci, '{0:N0} MB', ($Bytes / 1MB)) }
    return [string]::Format($ci, '{0:N0} KB', ($Bytes / 1KB))
}

function Format-Duration {
    param([double]$Seconds)
    if ($Seconds -lt 0) { $Seconds = 0 }
    $s = [int][math]::Round($Seconds)
    if ($s -lt 60) { return "$s s" }
    $m = [math]::Floor($s / 60)
    return ('{0} min {1} s' -f $m, ($s % 60))
}

function Format-Eta {
    <# Bytes left at a measured rate -> 'about N min left' (plain words), or '' when unknown. #>
    param([double]$BytesLeft, [double]$BytesPerSecond)
    if ($BytesPerSecond -le 0) { return '' }
    $sec = $BytesLeft / $BytesPerSecond
    if ($sec -lt 60) { return 'less than a minute left' }
    $min = [int][math]::Ceiling($sec / 60)
    if ($min -eq 1) { return 'about 1 minute left' }
    return "about $min minutes left"
}

function Get-RetryDelay {
    <# Attempt 1,2,3... -> seconds to wait: 5, 10, 20, 40, then 60 forever. #>
    param([int]$Attempt)
    if ($Attempt -le 1) { return 5 }
    $d = 5 * [math]::Pow(2, $Attempt - 1)
    if ($d -gt 60) { return 60 }
    return [int]$d
}

function Test-WindowsSupport {
    <# $null when this Windows can run Aither, else the one-sentence next step. #>
    param([int]$Build, [string]$Arch)
    if ($Arch -and $Arch -notmatch '^(X64|AMD64)$') {
        return 'This computer has an ARM processor, and Aither for Windows needs an Intel or AMD processor today, so please use a different computer.'
    }
    if ($Build -lt $script:MinBuild) {
        return "This copy of Windows is too old for Aither (build $Build): open Settings, then Windows Update, install every update, and run the same command again."
    }
    return $null
}

function Test-VirtualizationPure {
    <# 'ok' | 'off' | 'unknown' from what Windows reports.
       HypervisorPresent means a hypervisor already runs, and then the firmware flag reads
       FALSE by design -- so it is checked first. #>
    param($HypervisorPresent, $FirmwareEnabled)
    if ($HypervisorPresent -eq $true) { return 'ok' }
    if ($FirmwareEnabled -eq $true) { return 'ok' }
    if ($FirmwareEnabled -eq $false) { return 'off' }
    return 'unknown'
}

function Test-FreeSpace {
    param([int64]$FreeBytes, [int64]$NeedBytes, [string]$Drive)
    if ($FreeBytes -lt 0) { return $null }   # unreadable: let the import itself report
    if ($FreeBytes -lt $NeedBytes) {
        return ('Drive {0} has {1} free and Aither needs about {2}: empty the Recycle Bin or remove large files you do not need, then run the same command again.' -f $Drive, (Format-Bytes $FreeBytes), (Format-Bytes $NeedBytes))
    }
    return $null
}

function Get-WslReadiness {
    <# 'ready' | 'missing' | 'pending-reboot'.
       A requested install that has not seen a reboot since is pending, whatever wsl.exe
       says -- the feature is staged, not running. #>
    param([bool]$WslExeFound, [int]$StatusExit, [Nullable[datetime]]$InstallRequestedAt, [Nullable[datetime]]$LastBoot)
    if ($InstallRequestedAt -and $LastBoot -and ($InstallRequestedAt -gt $LastBoot)) { return 'pending-reboot' }
    if (-not $WslExeFound) { return 'missing' }
    if ($StatusExit -eq 0) { return 'ready' }
    return 'missing'
}

function Get-FriendlyWslError {
    <# Raw wsl.exe output -> one sentence that says what to do next. #>
    param([string]$Text)
    $t = "$Text"
    if ($t -match '0x80370102|0x80370114|HCS_E_HYPERV_NOT_INSTALLED|[Vv]irtuali[sz]ation') {
        return "Virtualization is turned off in this computer's start-up settings: restart, open the BIOS/UEFI setup, turn on 'Virtualization' (Intel VT-x or AMD SVM), then run the same command again."
    }
    if ($t -match '0x8007019e|not been enabled|0x8000000d') {
        return 'Windows has not finished turning on WSL: restart your computer once, then run the same command again.'
    }
    if ($t -match '0x80070070|not enough space|disk is full') {
        return 'The disk is full: free up at least 12 GB (empty the Recycle Bin, remove large files), then run the same command again.'
    }
    if ($t -match '0x800701bc|kernel|wsl --update') {
        return "WSL needs an update: run the same command again and accept the 'allow changes' prompt so it can update itself."
    }
    if ($t -match 'already exists|ERROR_ALREADY_EXISTS') {
        return 'Aither is already installed on this computer: run the same command again and it will finish connecting the copy you have.'
    }
    return 'Something unexpected happened: take a screenshot of this window and send it to the person helping you.'
}

function Test-SafeAssetName {
    param([string]$Candidate)
    return [bool]($Candidate -and $Candidate -match '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' -and $Candidate -notmatch '\.\.')
}

function Test-RootfsManifest {
    <# $null when the release manifest is usable, else why not. Never trust a name it
       carries as a path: every name must be a bare, safe file name. #>
    param($Manifest)
    if (-not $Manifest) { return 'empty manifest' }
    if ("$($Manifest.kind)" -ne 'awnix-wsl-rootfs') { return "not a WSL rootfs manifest (kind '$($Manifest.kind)')" }
    if (-not (Test-SafeAssetName "$($Manifest.name)")) { return "unsafe file name '$($Manifest.name)'" }
    if ("$($Manifest.sha256)" -notmatch '^[0-9a-f]{64}$') { return 'whole-file sha256 missing' }
    $parts = @($Manifest.parts)
    if ($parts.Count -lt 1) { return 'no parts' }
    [int64]$sum = 0
    foreach ($p in $parts) {
        if (-not (Test-SafeAssetName "$($p.name)")) { return "unsafe part name '$($p.name)'" }
        if ("$($p.sha256)" -notmatch '^[0-9a-f]{64}$') { return "part $($p.name) has no sha256" }
        if ([int64]$p.size -le 0) { return "part $($p.name) has no size" }
        $sum += [int64]$p.size
    }
    if ($sum -ne [int64]$Manifest.total) { return "parts add up to $sum bytes, manifest says $($Manifest.total)" }
    if ([int64]$Manifest.total -lt [int64](50MB)) { return 'the rootfs is too small to be an operating system' }
    return $null
}

function Select-WslRelease {
    <# GitHub releases JSON (array) + edition -> @{Tag; ManifestUrl; BaseUrl} of the newest
       published awnix-wsl-<edition>-YYYY.MM.DD[-n] release carrying awnix-wsl.json; $null if none. #>
    param($Releases, [string]$Edition, [string]$Repo)
    $rx = '^awnix-wsl-' + [regex]::Escape($Edition) + '-(\d{4}\.\d{2}\.\d{2})(-[A-Za-z0-9]+)?$'
    $best = $null; $bestKey = ''
    foreach ($r in @($Releases)) {
        if ($r.draft -or $r.prerelease) { continue }
        $tag = "$($r.tag_name)"
        if ($tag -notmatch $rx) { continue }
        $asset = @($r.assets) | Where-Object { $_.name -eq 'awnix-wsl.json' } | Select-Object -First 1
        if (-not $asset) { continue }
        $key = $Matches[1] + "$($Matches[2])"
        if ($key -gt $bestKey) { $bestKey = $key; $best = @{ Tag = $tag; ManifestUrl = "$($asset.browser_download_url)" } }
    }
    if ($best) { $best.BaseUrl = "https://github.com/$Repo/releases/download/$($best.Tag)/" }
    return $best
}

function Merge-IniKey {
    <# Set [Section] Key=Value, keeping every other line. Idempotent. (Same rules as
       install-awnix-wsl.ps1, which carries its own self-test of them.) #>
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

function ConvertFrom-LinkEvent {
    <# One line of `awnix-setup --link --json` -> the event object, or $null for noise. #>
    param([string]$Line)
    $l = "$Line".Trim()
    if (-not $l.StartsWith('{')) { return $null }
    try { $o = $l | ConvertFrom-Json } catch { return $null }
    if (-not $o.event) { return $null }
    return $o
}

function Get-ResumeCommand {
    param([string]$ScriptPath, [string]$Edition)
    return ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Resume -Edition {1}' -f $ScriptPath, $Edition)
}

# ===== output =================================================================

function Say { param([string]$m = '') Write-Host "  $m" }

function Stop-Install {
    <# The ONE way this installer ends on an error: a plain sentence that names the next step. #>
    param([string]$Next)
    Write-Progress -Activity 'Installing Aither' -Completed
    Write-Host ''
    Write-Host "  Aither could not finish installing." -ForegroundColor Yellow
    Write-Host "  $Next" -ForegroundColor Yellow
    Write-Host ''
    Save-Times
    if (-not $script:NoPause) { Read-Host '  Press Enter to close this window' | Out-Null }
    exit 1
}

function Show-Step {
    param([int]$Index, [double]$Fraction = 0, [string]$Detail = '')
    $pct = Get-OverallPercent -Step $Index -Fraction $Fraction
    $title = "Step $($Index + 1) of $($script:Steps.Count): $($script:Steps[$Index])"
    $status = "$pct% done"
    if ($Detail) { $status = "$status -- $Detail" }
    Write-Progress -Activity 'Installing Aither' -Status $title -CurrentOperation $status -PercentComplete $pct
}

function Start-StepTimer {
    param([int]$Index)
    $script:StepWatch = [Diagnostics.Stopwatch]::StartNew()
    Write-Host ''
    Write-Host ("  [{0}%] Step {1} of {2}: {3}" -f (Get-OverallPercent -Step $Index -Fraction 0), ($Index + 1), $script:Steps.Count, $script:Steps[$Index]) -ForegroundColor Cyan
    Show-Step -Index $Index
}

function Stop-StepTimer {
    param([int]$Index, [string]$Note = 'done')
    $sec = $script:StepWatch.Elapsed.TotalSeconds
    $script:Times[$script:Steps[$Index]] = [math]::Round($sec, 1)
    Say ("{0} ({1})" -f $Note, (Format-Duration $sec))
    Show-Step -Index $Index -Fraction 1
}

function Save-Times {
    try {
        if (-not (Test-Path -LiteralPath $InstallDir)) { return }
        $o = [ordered]@{ edition = $Edition; finished = (Get-Date).ToString('s'); seconds = $script:Times }
        ($o | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $InstallDir 'install-times.json') -Encoding ASCII
    } catch { Write-Verbose "times not saved: $($_.Exception.Message)" }
}

# ===== state (survives the one reboot) =======================================

function Get-StatePath { return (Join-Path $InstallDir 'install-state.json') }

function Read-State {
    $p = Get-StatePath
    if (Test-Path -LiteralPath $p) {
        try { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) } catch { Write-Verbose 'state unreadable; starting over' }
    }
    return [pscustomobject]@{ wslInstallRequestedAt = $null; startPageOpened = $false }
}

function Write-State {
    param($State)
    if ($DryRun) { return }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    ($State | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Get-StatePath) -Encoding ASCII
}

# ===== the one door into the person's distro =================================

function Invoke-Wsl {
    <# wsl.exe for the Windows-side verbs (--status, --import, --install, --terminate).
       In -DryRun it prints and runs nothing. Output is UTF-8 (WSL_UTF8) with NULs stripped. #>
    param([string[]]$Arguments)
    if ($DryRun) { Say "[dry-run] would run: wsl.exe $($Arguments -join ' ')"; $global:LASTEXITCODE = 0; return '' }
    # Windows PowerShell 5.1 turns a native command's stderr into a TERMINATING error under
    # ErrorActionPreference=Stop; wsl.exe writes its errors there, and we want to read them.
    $ErrorActionPreference = 'Continue'
    $env:WSL_UTF8 = '1'
    $out = (& wsl.exe @Arguments 2>&1 | ForEach-Object { "$_" } | Out-String) -replace "`0", ''
    return $out
}

function Invoke-InDistro {
    <# A command inside THIS installer's distro, as root. The only place one is run.
       Emits stdout AND stderr as plain strings, line by line as they arrive (streamed), so
       a warning on stderr can never abort the install (PowerShell 5.1 + Stop). #>
    param([string[]]$Command)
    $argv = @('--distribution', $Name, '--user', 'root', '--') + $Command
    if ($DryRun) { Say "[dry-run] would run inside '$Name': $($Command -join ' ')"; $global:LASTEXITCODE = 0; return }
    $ErrorActionPreference = 'Continue'
    $env:WSL_UTF8 = '1'
    & wsl.exe @argv 2>&1 | ForEach-Object { "$_" }
}

# ===== live steps =============================================================

function Get-OsFacts {
    $os = $null; $cs = $null; $cpu = $null
    try { $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop } catch { Write-Verbose 'no Win32_OperatingSystem' }
    try { $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop } catch { Write-Verbose 'no Win32_ComputerSystem' }
    try { $cpu = @(Get-CimInstance Win32_Processor -ErrorAction Stop)[0] } catch { Write-Verbose 'no Win32_Processor' }
    $arch = $env:PROCESSOR_ARCHITEW6432
    if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
    $build = 0
    if ($os) { $build = [int]$os.BuildNumber } else { $build = [Environment]::OSVersion.Version.Build }
    $lastBoot = $null
    if ($os) { $lastBoot = $os.LastBootUpTime }
    return @{
        Build = $build; Arch = $arch; LastBoot = $lastBoot
        HypervisorPresent = $(if ($cs) { $cs.HypervisorPresent } else { $null })
        FirmwareVirt = $(if ($cpu) { $cpu.VirtualizationFirmwareEnabled } else { $null })
    }
}

function Open-Page {
    param([string]$Url)
    if ($NoBrowser) { Say "open this page: $Url"; return }
    if ($DryRun) { Say "[dry-run] would open in the browser: $Url"; return }
    try { Start-Process $Url } catch { Say "Please open this page yourself: $Url" }
}

function Step-Check {
    param($State)
    Start-StepTimer 0
    $f = Get-OsFacts
    $why = Test-WindowsSupport -Build $f.Build -Arch $f.Arch
    if ($why) { Stop-Install $why }
    $v = Test-VirtualizationPure -HypervisorPresent $f.HypervisorPresent -FirmwareEnabled $f.FirmwareVirt
    if ($v -eq 'off') {
        Stop-Install "Virtualization is turned off in this computer's start-up settings: restart, open the BIOS/UEFI setup, turn on 'Virtualization' (Intel VT-x or AMD SVM), then run the same command again."
    }
    $drive = ($InstallDir.Substring(0, 1)).ToUpper()
    $free = -1
    try { $free = [int64](Get-PSDrive -Name $drive -ErrorAction Stop).Free } catch { $free = -1 }
    $why = Test-FreeSpace -FreeBytes $free -NeedBytes $script:NeedFreeBytes -Drive "${drive}:"
    if ($why) { Stop-Install $why }
    Say ("Windows build {0}, {1}, virtualization {2}, {3} free on {4}:" -f $f.Build, $f.Arch, $v, (Format-Bytes $free), $drive)
    if (-not $State.startPageOpened) {
        Say 'Opening the Aitherium page in your browser -- you can start creating there now;'
        Say 'this window keeps installing Aither on your computer in the background.'
        Open-Page $StartUrl
        $State.startPageOpened = $true
        Write-State $State
    }
    Stop-StepTimer 0 'this computer is ready'
    return $f
}

function Get-WslState {
    param($State, $Facts)
    if ($SimulateWsl) { return $SimulateWsl }
    $found = [bool](Get-Command wsl.exe -ErrorAction SilentlyContinue)
    $exit = 1
    if ($found -and -not $DryRun) {
        $ErrorActionPreference = 'Continue'   # stderr from wsl.exe must not abort (PS 5.1)
        $env:WSL_UTF8 = '1'
        & wsl.exe --status *> $null
        $exit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } elseif ($found -and $DryRun) {
        # Read-only even so: --status starts no distro. Kept out of -DryRun anyway, so a
        # dry run on a machine whose fleet runs in WSL touches WSL not at all.
        Say '[dry-run] not asking wsl.exe for its status; pass -SimulateWsl missing|ready to walk a branch'
        $exit = 0
    }
    $req = $null
    if ($State.wslInstallRequestedAt) { try { $req = [datetime]$State.wslInstallRequestedAt } catch { $req = $null } }
    return (Get-WslReadiness -WslExeFound $found -StatusExit $exit -InstallRequestedAt $req -LastBoot $Facts.LastBoot)
}

function Register-Resume {
    $self = Join-Path $InstallDir 'install-aither-windows.ps1'
    $cmd = Get-ResumeCommand -ScriptPath $self -Edition $Edition
    if ($DryRun) { Say "[dry-run] would continue after the restart with: $cmd"; return }
    if ($PSCommandPath -and ((Resolve-Path -LiteralPath $PSCommandPath).Path -ne $self)) {
        Copy-Item -LiteralPath $PSCommandPath -Destination $self -Force
    }
    New-Item -Path $script:RunOnceKey -Force | Out-Null
    Set-ItemProperty -Path $script:RunOnceKey -Name $script:RunOnceName -Value $cmd
}

function Unregister-Resume {
    if ($DryRun) { return }
    Remove-ItemProperty -Path $script:RunOnceKey -Name $script:RunOnceName -ErrorAction SilentlyContinue
}

function Step-Wsl {
    param($State, $Facts)
    Start-StepTimer 1
    $st = Get-WslState $State $Facts
    if ($st -eq 'ready') { Stop-StepTimer 1 'WSL is already on'; return }
    if ($st -eq 'pending-reboot') {
        Register-Resume
        Say 'Windows still needs the one restart to finish turning on WSL.'
        Request-Restart
        return
    }
    Say 'Aither runs inside a Windows feature called WSL. Turning it on now.'
    Say "Windows will ask 'Do you want to allow this app to make changes to your device?' -- click Yes."
    if ($DryRun) {
        Say '[dry-run] would run (as administrator): wsl.exe --install --no-distribution'
    } else {
        $p = $null
        try {
            $p = Start-Process -FilePath 'wsl.exe' -ArgumentList '--install', '--no-distribution' -Verb RunAs -Wait -PassThru
        } catch {
            Stop-Install "Windows did not get permission to turn on WSL: run the same command again and click Yes when Windows asks."
        }
        if ($p.ExitCode -ne 0) {
            # Older inbox wsl.exe has no --no-distribution: turn the two features on directly.
            Say 'Using the older way to turn WSL on.'
            $dism = 'dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart; dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart'
            try {
                $p2 = Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-Command', $dism -Verb RunAs -Wait -PassThru
            } catch {
                Stop-Install "Windows did not get permission to turn on WSL: run the same command again and click Yes when Windows asks."
            }
            # dism exits 3010 for "done, restart required".
            if ($p2.ExitCode -ne 0 -and $p2.ExitCode -ne 3010) {
                Stop-Install (Get-FriendlyWslError "exit $($p2.ExitCode)")
            }
        }
    }
    $State.wslInstallRequestedAt = (Get-Date).ToString('o')
    Write-State $State
    Register-Resume
    Stop-StepTimer 1 'WSL is turned on; Windows needs one restart to finish'
    Request-Restart
}

function Request-Restart {
    Save-Times
    Write-Host ''
    Write-Host '  Your computer needs to restart ONE time to finish turning on WSL.' -ForegroundColor Cyan
    Say 'Save anything you have open first. After the restart, sign in as usual:'
    Say 'this window comes back by itself and Aither carries on where it stopped.'
    Write-Host ''
    if ($DryRun) { Say '[dry-run] would ask to restart now'; exit 0 }
    $a = Read-Host '  Restart now? Type Y and press Enter (or N to restart later yourself)'
    if ("$a".Trim().ToUpper().StartsWith('Y')) { Restart-Computer -Force }
    Say 'OK -- restart whenever you are ready; Aither continues after you sign in again.'
    exit 0
}

function Get-JsonFromUrl {
    param([string]$Url)
    $h = @{ 'User-Agent' = $script:UA; 'Accept' = 'application/vnd.github+json' }
    for ($i = 1; $i -le 4; $i++) {
        try { return (Invoke-RestMethod -Uri $Url -Headers $h -UseBasicParsing -TimeoutSec 30) }
        catch {
            if ($i -eq 4) { throw }
            Start-Sleep -Seconds (Get-RetryDelay $i)
        }
    }
}

function Resolve-RootfsManifest {
    if ($ManifestUrl) {
        $m = Get-JsonFromUrl $ManifestUrl
        $base = $ManifestUrl.Substring(0, $ManifestUrl.LastIndexOf('/') + 1)
        return @{ Manifest = $m; BaseUrl = $base; Tag = "$($m.tag)" }
    }
    try {
        $rel = Get-JsonFromUrl "https://api.github.com/repos/$ReleaseRepo/releases?per_page=60"
    } catch {
        Stop-Install 'This computer could not reach the Aither download server: check that the internet works (open any website), then run the same command again.'
    }
    $pick = Select-WslRelease -Releases $rel -Edition $Edition -Repo $ReleaseRepo
    if (-not $pick) {
        Stop-Install "The Aither download for Windows ('$Edition') has not been published yet: please try again tomorrow, or tell the person who sent you this command."
    }
    $m = Get-JsonFromUrl $pick.ManifestUrl
    return @{ Manifest = $m; BaseUrl = $pick.BaseUrl; Tag = $pick.Tag }
}

function Get-HttpResponse {
    <# GET with an optional Range start; redirects followed by hand so the Range header
       goes to the final (signed, short-lived) storage URL, re-resolved on every attempt. #>
    param([string]$Url, [int64]$From)
    $u = $Url
    for ($i = 0; $i -lt 6; $i++) {
        $req = [Net.HttpWebRequest]::Create($u)
        $req.AllowAutoRedirect = $false
        $req.UserAgent = $script:UA
        $req.Timeout = 30000
        $req.ReadWriteTimeout = 60000
        if ($From -gt 0) { $req.AddRange($From) }
        $resp = $null
        try { $resp = $req.GetResponse() }
        catch {
            $we = $_.Exception
            while ($we -and -not ($we -is [Net.WebException])) { $we = $we.InnerException }
            if ($we -and $we.Response) { $resp = $we.Response } else { throw }
        }
        $code = [int]$resp.StatusCode
        if ($code -ge 300 -and $code -lt 400) {
            $loc = $resp.Headers['Location']
            $resp.Close()
            $u = (New-Object Uri ((New-Object Uri $u), $loc)).AbsoluteUri
            continue
        }
        return $resp
    }
    throw 'too many redirects'
}

function Save-Part {
    <# One part, resumed from whatever is on disk, retried through dropped connections,
       sha256-verified. Reports progress through $script:DlDone (bytes of earlier parts). #>
    param([string]$Url, [string]$Path, [int64]$Size, [string]$Sha)
    $attempt = 0; $badHash = 0
    while ($true) {
        $have = 0
        if (Test-Path -LiteralPath $Path) { $have = (Get-Item -LiteralPath $Path).Length }
        if ($have -gt $Size) { Remove-Item -LiteralPath $Path -Force; $have = 0 }
        if ($have -eq $Size) {
            $h = (Get-Sha256Hex $Path)
            if ($h -eq $Sha) { return }
            $badHash++
            Remove-Item -LiteralPath $Path -Force
            if ($badHash -ge 3) { Stop-Install 'The download keeps arriving damaged: try a different internet connection, then run the same command again.' }
            continue
        }
        try {
            $resp = Get-HttpResponse -Url $Url -From $have
            $code = [int]$resp.StatusCode
            $mode = [IO.FileMode]::Append
            if ($code -eq 200) { $have = 0; $mode = [IO.FileMode]::Create }
            elseif ($code -eq 416) { $resp.Close(); Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue; continue }
            elseif ($code -ne 206) { $resp.Close(); throw "HTTP $code" }
            $in = $resp.GetResponseStream()
            $out = New-Object IO.FileStream($Path, $mode, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            try {
                $buf = New-Object byte[] (1MB)
                $cur = $have
                while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                    $out.Write($buf, 0, $n)
                    $cur += $n
                    Update-DownloadProgress ($script:DlDone + $cur)
                }
            } finally { $out.Close(); $in.Close(); $resp.Close() }
            $attempt = 0
        } catch {
            $attempt++
            if ($attempt -gt 30) {
                Stop-Install 'The internet connection kept dropping for too long: when it is working again, run the same command -- the download continues where it stopped.'
            }
            $wait = Get-RetryDelay $attempt
            Say "The internet connection dropped. Trying again in $wait seconds -- nothing already downloaded is lost."
            Write-Verbose "download error: $($_.Exception.Message)"
            Start-Sleep -Seconds $wait
        }
    }
}

function Get-Sha256Hex {
    <# .NET, not Get-FileHash: that cmdlet is missing when the Utility module does not load. #>
    param([string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = [IO.File]::OpenRead($Path)
    try { $h = $sha.ComputeHash($fs) } finally { $fs.Close() }
    return (-join ($h | ForEach-Object { $_.ToString('x2') }))
}

function Update-DownloadProgress {
    param([int64]$Done)
    $now = $script:DlWatch.Elapsed.TotalSeconds
    if (($now - $script:DlLastShow) -lt 0.5 -and $Done -lt $script:DlTotal) { return }
    $script:DlLastShow = $now
    $rate = 0
    $fresh = $Done - $script:DlStartBytes
    if ($now -gt 2) { $rate = $fresh / $now }
    $detail = '{0} of {1}' -f (Format-Bytes $Done), (Format-Bytes $script:DlTotal)
    $eta = Format-Eta -BytesLeft ($script:DlTotal - $Done) -BytesPerSecond $rate
    if ($eta) { $detail = "$detail, $eta" }
    Show-Step -Index 2 -Fraction ($Done / [double]$script:DlTotal) -Detail $detail
    $pct = [int][math]::Floor(100 * $Done / [double]$script:DlTotal)
    if ($pct -ge $script:DlLastPct + 10) {
        $script:DlLastPct = $pct - ($pct % 10)
        Say ("downloaded {0}% ({1})" -f $script:DlLastPct, $detail)
    }
}

function Step-Download {
    Start-StepTimer 2
    $r = Resolve-RootfsManifest
    $m = $r.Manifest
    $why = Test-RootfsManifest $m
    if ($why) { Stop-Install "The Aither download on the server is not usable ($why): please try again later, or tell the person who sent you this command." }
    $stage = Join-Path $InstallDir 'download'
    $tar = Join-Path $stage "$($m.name)"
    Say ("Aither {0} ({1}), {2} to download in {3} part(s)." -f $Edition, $r.Tag, (Format-Bytes ([int64]$m.total)), @($m.parts).Count)
    if ($DryRun) {
        foreach ($p in @($m.parts)) { Say "[dry-run] would download $($r.BaseUrl)$($p.name) ($(Format-Bytes ([int64]$p.size)))" }
        Say "[dry-run] would join them into $tar and check sha256 $($m.sha256)"
        Stop-StepTimer 2 'download planned'
        return $tar
    }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    if ((Test-Path -LiteralPath $tar) -and ((Get-Item -LiteralPath $tar).Length -eq [int64]$m.total) -and
        ((Get-Sha256Hex $tar) -eq "$($m.sha256)")) {
        Stop-StepTimer 2 'already downloaded'
        return $tar
    }
    $script:DlTotal = [int64]$m.total
    $script:DlDone = 0
    $script:DlLastShow = -1
    $script:DlLastPct = 0
    $already = 0
    foreach ($p in @($m.parts)) {
        $pp = Join-Path $stage "$($p.name)"
        if (Test-Path -LiteralPath $pp) { $already += [math]::Min((Get-Item -LiteralPath $pp).Length, [int64]$p.size) }
    }
    $script:DlStartBytes = $already
    if ($already -gt 0) { Say ("continuing a download that had already reached {0}" -f (Format-Bytes $already)) }
    $script:DlWatch = [Diagnostics.Stopwatch]::StartNew()
    foreach ($p in @($m.parts)) {
        $pp = Join-Path $stage "$($p.name)"
        Save-Part -Url ($r.BaseUrl + "$($p.name)") -Path $pp -Size ([int64]$p.size) -Sha "$($p.sha256)"
        $script:DlDone += [int64]$p.size
    }
    Say 'putting the pieces together and checking them'
    $sha = [Security.Cryptography.SHA256]::Create()
    $out = New-Object IO.FileStream($tar, [IO.FileMode]::Create, [IO.FileAccess]::Write)
    try {
        $buf = New-Object byte[] (4MB)
        foreach ($p in @($m.parts)) {
            $in = [IO.File]::OpenRead((Join-Path $stage "$($p.name)"))
            try {
                while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                    $out.Write($buf, 0, $n)
                    [void]$sha.TransformBlock($buf, 0, $n, $null, 0)
                }
            } finally { $in.Close() }
        }
        [void]$sha.TransformFinalBlock((New-Object byte[] 0), 0, 0)
    } finally { $out.Close() }
    $whole = -join ($sha.Hash | ForEach-Object { $_.ToString('x2') })
    if ($whole -ne "$($m.sha256)") {
        Remove-Item -LiteralPath $tar -Force -ErrorAction SilentlyContinue
        Stop-Install 'The download did not check out after joining: run the same command again (the pieces are kept, so it is quick).'
    }
    foreach ($p in @($m.parts)) { Remove-Item -LiteralPath (Join-Path $stage "$($p.name)") -Force -ErrorAction SilentlyContinue }
    $rate = ($script:DlTotal - $script:DlStartBytes) / [math]::Max(1, $script:DlWatch.Elapsed.TotalSeconds)
    Stop-StepTimer 2 ('downloaded and checked, {0}/s on this connection' -f (Format-Bytes $rate))
    return $tar
}

function Test-DistroRegistered {
    if ($DryRun) { return $false }
    $txt = Invoke-Wsl @('--list', '--quiet')
    $names = @($txt -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    return ($names -contains $Name)
}

function Step-Install {
    param([string]$Tar)
    Start-StepTimer 3
    if (Test-DistroRegistered) {
        Say "Aither is already installed here (WSL distro '$Name') -- keeping it."
    } else {
        $dir = Join-Path $InstallDir $Name
        Say 'unpacking Aither (a few minutes; the bar may sit still -- that is normal)'
        Show-Step -Index 3 -Fraction 0.1 -Detail 'unpacking'
        $out = Invoke-Wsl @('--import', $Name, $dir, $Tar, '--version', '2')
        if ($LASTEXITCODE -ne 0) { Write-Host $out; Stop-Install (Get-FriendlyWslError $out) }
        if (-not $DryRun -and -not (Test-DistroRegistered)) {
            Stop-Install 'Windows reported success but Aither is not listed: restart your computer, then run the same command again.'
        }
    }
    Show-Step -Index 3 -Fraction 0.7 -Detail 'starting Aither for the first time'
    $cur = ''
    if (-not $DryRun) { $cur = (Invoke-InDistro @('sh', '-c', 'cat /etc/wsl.conf 2>/dev/null || true') | Out-String) -replace "`r", '' }
    $new = Merge-IniKey $cur 'boot' 'systemd' 'true'
    $new = Merge-IniKey $new 'user' 'default' 'root'
    $new = Merge-IniKey $new 'interop' 'appendWindowsPath' 'false'
    if ($new.TrimEnd("`n") -ne $cur.TrimEnd("`n")) {
        $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($new + "`n"))
        Invoke-InDistro @('sh', '-c', "echo $b64 | base64 -d > /etc/wsl.conf") | Out-Null
        if ($LASTEXITCODE -ne 0) { Stop-Install 'Aither could not finish its first start: restart your computer, then run the same command again.' }
        Invoke-Wsl @('--terminate', $Name) | Out-Null
    }
    $ok = $DryRun
    for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
        $comm = (Invoke-InDistro @('cat', '/proc/1/comm') | Out-String).Trim()
        if ($comm -eq 'systemd') { $ok = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ok) { Stop-Install 'Aither started but its services did not: restart your computer, then run the same command again.' }
    if (-not $DryRun) { Remove-Item -LiteralPath $Tar -Force -ErrorAction SilentlyContinue }
    Stop-StepTimer 3 'Aither is installed and running'
}

function Step-Connect {
    Start-StepTimer 4
    Say 'Opening the Aitherium sign-in page so this computer joins your account.'
    if ($DryRun) {
        Invoke-InDistro @('/usr/bin/awnix-setup', '--link', '--json')
        Say '[dry-run] would open the sign-in page it prints and wait for your approval'
        Stop-StepTimer 4 'connect planned'
        return
    }
    $linked = $false; $shown = $false; $err = ''
    Invoke-InDistro @('/usr/bin/awnix-setup', '--link', '--json') | ForEach-Object {
        $e = ConvertFrom-LinkEvent "$_"
        if (-not $e) { Write-Verbose "link: $_"; return }
        switch ("$($e.event)") {
            'code' {
                if (-not $shown) {
                    $shown = $true
                    Write-Host ''
                    Write-Host "  Your code is:  $($e.user_code)" -ForegroundColor Green
                    Say 'Check that the page in your browser shows the same code, then click Approve.'
                    Say "(If no page opened, go to $($e.verification_uri))"
                    Open-Page "$($e.verification_uri)"
                }
            }
            'waiting' { Show-Step -Index 4 -Fraction 0.5 -Detail 'waiting for you to approve in the browser' }
            'linked' { $linked = $true }
            'already_linked' { $linked = $true }
            'failed' { $err = "$($e.error)" }
        }
    }
    if (-not $linked) {
        if (-not $shown -and -not $err) {
            Stop-Install "This Aither version cannot connect itself yet: open Windows Terminal, choose 'awnix' from the arrow next to the tab, and type  awnix-setup --link  there."
        }
        Stop-Install 'The connection was not approved in time: run the same command again and click Approve on the page that opens.'
    }
    Stop-StepTimer 4 'connected to your Aitherium account'
}

# ===== self-test ==============================================================

function Invoke-SelfTest {
    $script:fail = 0
    function Ck([string]$n, [bool]$c) { if ($c) { Write-Host "  ok   $n" } else { Write-Host "  FAIL $n"; $script:fail++ } }

    Ck 'percent starts at 0' ((Get-OverallPercent 0 0) -eq 0)
    Ck 'percent ends at 100' ((Get-OverallPercent 4 1) -eq 100)
    Ck 'half the download is past 40%' ((Get-OverallPercent 2 0.5) -eq 45)
    Ck 'percent clamps a fraction above 1' ((Get-OverallPercent 2 7) -eq (Get-OverallPercent 3 0))
    Ck 'bytes read as GB' ((Format-Bytes ([double]1.75GB)) -eq '1.8 GB')
    Ck 'bytes read as MB' ((Format-Bytes ([double]780MB)) -eq '780 MB')
    Ck 'eta in plain words' ((Format-Eta -BytesLeft ([double]300MB) -BytesPerSecond ([double]3MB)) -eq 'about 2 minutes left')
    Ck 'eta unknown at no rate' ((Format-Eta 100 0) -eq '')
    Ck 'duration under a minute' ((Format-Duration 42.4) -eq '42 s')
    Ck 'duration over a minute' ((Format-Duration 130) -eq '2 min 10 s')
    Ck 'retry backs off 5,10,20,40' (((@((Get-RetryDelay 1), (Get-RetryDelay 2), (Get-RetryDelay 3), (Get-RetryDelay 4))) -join ',') -eq '5,10,20,40')
    Ck 'retry caps at 60 s' ((Get-RetryDelay 12) -eq 60)

    Ck 'Windows 11 x64 is supported' ($null -eq (Test-WindowsSupport -Build 22631 -Arch 'AMD64'))
    Ck 'Windows 10 1909 is refused with a next step' ((Test-WindowsSupport -Build 18363 -Arch 'AMD64') -match 'Windows Update')
    Ck 'ARM64 is refused in plain words' ((Test-WindowsSupport -Build 22631 -Arch 'ARM64') -match 'ARM processor')
    Ck 'a running hypervisor counts as virtualization on' ((Test-VirtualizationPure $true $false) -eq 'ok')
    Ck 'firmware virtualization off is refused' ((Test-VirtualizationPure $false $false) -eq 'off')
    Ck 'unknown virtualization does not block' ((Test-VirtualizationPure $null $null) -eq 'unknown')
    Ck '5 GB free is refused with a next step' ((Test-FreeSpace ([int64]5GB) ([int64]12GB) 'C:') -match 'Recycle Bin')
    Ck '50 GB free is fine' ($null -eq (Test-FreeSpace ([int64]50GB) ([int64]12GB) 'C:'))

    $boot = [datetime]'2026-11-11T09:00:00'
    Ck 'no wsl.exe = missing' ((Get-WslReadiness $false 1 $null $boot) -eq 'missing')
    Ck 'status 0 = ready' ((Get-WslReadiness $true 0 $null $boot) -eq 'ready')
    Ck 'status non-zero = missing' ((Get-WslReadiness $true 1 $null $boot) -eq 'missing')
    Ck 'install requested after the last boot = pending reboot' ((Get-WslReadiness $true 0 ([datetime]'2026-11-11T10:00:00') $boot) -eq 'pending-reboot')
    Ck 'install requested before the last boot = judged by status' ((Get-WslReadiness $true 0 ([datetime]'2026-11-11T08:00:00') $boot) -eq 'ready')

    Ck 'virtualization error says BIOS' ((Get-FriendlyWslError 'Error code: Wsl/0x80370102') -match 'BIOS')
    Ck 'not-enabled error says restart' ((Get-FriendlyWslError 'WSL has not been enabled 0x8007019e') -match 'restart')
    Ck 'disk error says free space' ((Get-FriendlyWslError 'HRESULT 0x80070070') -match '12 GB')
    Ck 'unknown error says screenshot' ((Get-FriendlyWslError 'weird') -match 'screenshot')
    $all = @('0x80370102', '0x8007019e', '0x80070070', '0x800701bc', 'already exists', 'weird') | ForEach-Object { Get-FriendlyWslError $_ }
    Ck 'every error message is ONE sentence ending in a full stop' (@($all | Where-Object { ($_ -split '\. ').Count -ne 1 -or -not $_.EndsWith('.') }).Count -eq 0)

    $good = [pscustomobject]@{ kind = 'awnix-wsl-rootfs'; name = 'awnix-wsl-base.rootfs.tar.gz'; total = 120MB
        sha256 = ('a' * 64); parts = @([pscustomobject]@{ name = 'awnix-wsl-base.rootfs.tar.gz.part00'; size = 100MB; sha256 = ('b' * 64) },
                                       [pscustomobject]@{ name = 'awnix-wsl-base.rootfs.tar.gz.part01'; size = 20MB; sha256 = ('c' * 64) }) }
    Ck 'a good manifest passes' ($null -eq (Test-RootfsManifest $good))
    $bad = $good.PSObject.Copy(); $bad.total = 121MB
    Ck 'parts that do not add up are refused' ((Test-RootfsManifest $bad) -match 'add up')
    $trav = $good.PSObject.Copy(); $trav.parts = @([pscustomobject]@{ name = '..\..\evil.ps1'; size = 120MB; sha256 = ('b' * 64) })
    Ck 'a path in a part name is refused' ((Test-RootfsManifest $trav) -match 'unsafe')
    $iso = $good.PSObject.Copy(); $iso.kind = 'awnix-iso'
    Ck 'an ISO manifest is not a rootfs' ((Test-RootfsManifest $iso) -match 'not a WSL rootfs')

    $rels = @(
        [pscustomobject]@{ tag_name = 'awnix-wsl-base-2026.11.01'; draft = $false; prerelease = $false; assets = @([pscustomobject]@{ name = 'awnix-wsl.json'; browser_download_url = 'u1' }) },
        [pscustomobject]@{ tag_name = 'awnix-wsl-base-2026.11.08'; draft = $false; prerelease = $false; assets = @([pscustomobject]@{ name = 'awnix-wsl.json'; browser_download_url = 'u2' }) },
        [pscustomobject]@{ tag_name = 'awnix-wsl-base-2026.11.15'; draft = $true; prerelease = $false; assets = @([pscustomobject]@{ name = 'awnix-wsl.json'; browser_download_url = 'u3' }) },
        [pscustomobject]@{ tag_name = 'awnix-wsl-base-2026.11.09'; draft = $false; prerelease = $false; assets = @() },
        [pscustomobject]@{ tag_name = 'awnix-wsl-full-2026.11.10'; draft = $false; prerelease = $false; assets = @([pscustomobject]@{ name = 'awnix-wsl.json'; browser_download_url = 'u5' }) },
        [pscustomobject]@{ tag_name = 'awnix-iso-2026.11.10'; draft = $false; prerelease = $false; assets = @([pscustomobject]@{ name = 'awnix-wsl.json'; browser_download_url = 'u6' }) })
    $pick = Select-WslRelease $rels 'base' 'Aitherium/awnix'
    Ck 'newest published base release with a manifest is picked' ($pick.ManifestUrl -eq 'u2')
    Ck 'part base URL is the release download path' ($pick.BaseUrl -eq 'https://github.com/Aitherium/awnix/releases/download/awnix-wsl-base-2026.11.08/')
    Ck 'another edition is never picked for base' ((Select-WslRelease $rels 'full' 'Aitherium/awnix').ManifestUrl -eq 'u5')
    Ck 'no release = null, not a guess' ($null -eq (Select-WslRelease $rels 'ai-full' 'Aitherium/awnix'))

    $t = Merge-IniKey '' 'boot' 'systemd' 'true'
    Ck 'wsl.conf boot section added' ($t -eq "[boot]`nsystemd=true")
    $t2 = Merge-IniKey "[boot]`nsystemd=false`n# keep" 'boot' 'systemd' 'true'
    Ck 'wsl.conf value replaced, hand lines kept' ($t2 -eq "[boot]`nsystemd=true`n# keep")
    Ck 'wsl.conf merge idempotent' ((Merge-IniKey $t2 'boot' 'systemd' 'true') -eq $t2)

    Ck 'link code event parses' ((ConvertFrom-LinkEvent '{"event": "code", "user_code": "AB-12", "verification_uri": "https://x"}').user_code -eq 'AB-12')
    Ck 'link noise is ignored' ($null -eq (ConvertFrom-LinkEvent 'Warning: something'))
    Ck 'broken json is ignored' ($null -eq (ConvertFrom-LinkEvent '{"event": '))
    Ck 'resume command reruns this file with -Resume' ((Get-ResumeCommand 'C:\Users\m\AppData\Local\Aither\install-aither-windows.ps1' 'base') -eq 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\m\AppData\Local\Aither\install-aither-windows.ps1" -Resume -Edition base')

    $src = $null
    if ($PSCommandPath) { $src = [IO.File]::ReadAllBytes($PSCommandPath) }
    if ($src) { Ck 'this file is pure ASCII (PowerShell 5.1 reads it without a BOM)' (@($src | Where-Object { $_ -gt 127 }).Count -eq 0) }

    Write-Host ''
    if ($script:fail -eq 0) { Write-Host 'SELF-TEST PASS'; return 0 }
    Write-Host "SELF-TEST FAILED ($script:fail)"; return $script:fail
}

# ===== entry ==================================================================

if ($SelfTest) { exit (Invoke-SelfTest) }
if ($env:AITHER_INSTALL_LIBRARY_ONLY -eq '1') { return }   # dot-sourced by the Pester tests

try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { Write-Verbose 'TLS 1.2 already on' }

# Started by `irm | iex`: there is no file to come back to after the restart, so fetch
# this script to a file once and run THAT, in this same window.
if (-not $PSCommandPath) {
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $self = Join-Path $InstallDir 'install-aither-windows.ps1'
    try { Invoke-WebRequest -Uri $ScriptUrl -OutFile $self -UseBasicParsing -Headers @{ 'User-Agent' = $script:UA } }
    catch {
        Write-Host '  Aither could not finish installing.' -ForegroundColor Yellow
        Write-Host '  This computer could not reach the Aither download server: check that the internet works (open any website), then run the same command again.' -ForegroundColor Yellow
        return
    }
    $fw = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $self, '-Edition', $Edition, '-Name', $Name, '-InstallDir', $InstallDir, '-StartUrl', $StartUrl)
    if ($DryRun) { $fw += '-DryRun' }
    if ($NoBrowser) { $fw += '-NoBrowser' }
    if ($SimulateWsl) { $fw += @('-SimulateWsl', $SimulateWsl) }
    & powershell.exe @fw
    return
}

$script:NoPause = $DryRun
$total = [Diagnostics.Stopwatch]::StartNew()
Write-Host ''
Write-Host '  Installing Aither on this computer' -ForegroundColor Cyan
Say 'This takes about 10 to 20 minutes, mostly the download. You can keep using the computer.'
if ($DryRun) { Say 'DRY RUN: nothing will be changed, installed or downloaded (beyond a small file list).' }
if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null }
if ($Resume) { Say 'Welcome back -- carrying on from before the restart.' }

try {
    $state = Read-State
    $facts = Step-Check $state
    if (-not $DownloadOnly) { Step-Wsl $state $facts }
    $tar = Step-Download
    if ($DownloadOnly) {
        # Test lane: proves download, resume and the join on a machine where WSL must not be touched.
        Save-Times
        Say "download-only: $tar is ready; WSL was not touched."
        exit 0
    }
    Step-Install $tar
    Step-Connect
} catch {
    Write-Host "  ($($_.Exception.Message))"
    Stop-Install (Get-FriendlyWslError "$($_.Exception.Message)")
}
Unregister-Resume
if (-not $DryRun) { Remove-Item -LiteralPath (Get-StatePath) -Force -ErrorAction SilentlyContinue }

Write-Progress -Activity 'Installing Aither' -Completed
$script:Times['total'] = [math]::Round($total.Elapsed.TotalSeconds, 1)
Save-Times
Write-Host ''
Write-Host '  [100%] Aither is installed and connected to your Aitherium account.' -ForegroundColor Green
Say 'How long each step took on this computer:'
foreach ($k in $script:Times.Keys) { Say ('  {0,-40} {1}' -f $k, (Format-Duration $script:Times[$k])) }
Say ''
Say "To open Aither later: open Windows Terminal and choose '$Name' from the arrow next to the tab."
Write-Host ''
if (-not $script:NoPause) { Read-Host '  Press Enter to close this window' | Out-Null }
