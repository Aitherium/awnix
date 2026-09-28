# awnix-distro-guards.ps1 -- the refusals every WSL import/unregister in this directory
# must pass before it touches a registration. Dot-source, never run:
#
#     . (Join-Path $PSScriptRoot 'awnix-distro-guards.ps1')
#     $why = Test-AwnixDistroProtected -Name $Name
#     if ($why) { throw "refusing to unregister '$Name': $why" }
#
# WHY (2026-09-27). The fleet moved to a WSL distro NAMED `awnix`, and
# rehearse-awnix.ps1 defaulted -Name to `awnix` and began with
# `wsl --unregister $Name` -- so running the rehearsal with no arguments would have
# unregistered the LIVE fleet root. bootstrap-awnix.ps1 printed
# "undo with: wsl --unregister awnix" on the same host. Both are one keystroke from
# deleting the fleet's rootfs, and nothing in either script knew what the fleet was.
#
# Three rules, each a pure function over explicit inputs so -SelfTest can prove it
# fails without touching WSL:
#   1. a name equal to the RESOLVED fleet distro (fleet-distro.ps1) is protected
#      (case-insensitive: WSL distro names are);
#   2. a registration that holds cutover state is protected: its name is the source
#      or target recorded in the awnix migration state file (current-migration.json),
#      its BasePath is the directory named after that target (the live `awnix` is
#      registered at ...\wsl\aitheros-fleet), or its BasePath shares the fleet's;
#   3. no second distro is imported/started while the fleet distro is Running
#      (check_wsl_cotenancy: attaching a second disk to the shared utility VM failed
#      the fleet's vhdx writes and rebooted it twice on 2026-09-20).
# PS 5.1-safe and read-only: nothing here mutates WSL.

$script:AwnixGuardHere = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$script:AwnixFleetDistroPs1 = Join-Path $script:AwnixGuardHere '..\..\scripts\fleet-distro.ps1'
if (Test-Path -LiteralPath $script:AwnixFleetDistroPs1) {
    . $script:AwnixFleetDistroPs1
} elseif (-not (Get-Command Get-AitherFleetDistro -ErrorAction SilentlyContinue)) {
    # A copy of this directory outside the repo (a stranger's kit): the same env
    # chain as the resolver, then its default. Never 'Debian'.
    function Get-AitherFleetDistro {
        foreach ($v in @('AITHER_FLEET_DISTRO', 'AITHER_WSL_DISTRO', 'FLEET_DISTRO', 'AWDESK_FLEET_DISTRO')) {
            $x = [Environment]::GetEnvironmentVariable($v)
            if ($x -and $x.Trim()) { return $x.Trim() }
        }
        return 'awnix'
    }
}

function Get-AwnixStateFileCandidates {
    <# Where the migration writes current-migration.json (awnix-migration.yaml target.export_dir). #>
    $c = @()
    if ($env:AITHER_AWNIX_STATE_FILE) { $c += $env:AITHER_AWNIX_STATE_FILE }
    $c += 'C:/AitherOS-Data/wsl/_export/current-migration.json'
    return $c
}

function Read-AwnixCutoverState {
    <# @{ Source; Target } from the first readable state file, or $null. #>
    param([string[]]$Paths = (Get-AwnixStateFileCandidates))
    foreach ($p in $Paths) {
        if (-not $p -or -not (Test-Path -LiteralPath $p)) { continue }
        try {
            $j = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json
            return @{ Source = [string]$j.source; Target = [string]$j.target; Path = $p }
        } catch { continue }
    }
    return $null
}

function Get-AwnixRegistrations {
    <# name -> BasePath for every registered WSL distro, from the Lxss registry (no wsl.exe). #>
    $out = @{}
    $lxss = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
    if (-not (Test-Path $lxss)) { return $out }
    foreach ($k in Get-ChildItem $lxss -ErrorAction SilentlyContinue) {
        $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
        if ($p -and $p.PSObject.Properties['DistributionName']) {
            $out[[string]$p.DistributionName] = ([string]$p.BasePath -replace '^\\\\\?\\', '')
        }
    }
    return $out
}

function ConvertTo-AwnixNormPath {
    param([string]$Path)
    if (-not $Path) { return '' }
    return (($Path -replace '\\\\', '\') -replace '/', '\').TrimEnd('\').ToLowerInvariant()
}

function Test-AwnixDistroProtectedPure {
    <#
      The rule, over explicit inputs. $null when $Name may be unregistered/replaced;
      otherwise the reason. $Registrations: name -> BasePath. $State: @{Source;Target} or $null.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FleetDistro,
        [hashtable]$Registrations = @{},
        [hashtable]$State = $null
    )
    if ($Name -ieq $FleetDistro) {
        return "'$Name' IS the fleet distro (resolved by fleet-distro.ps1) - unregistering it deletes the live fleet root"
    }
    if ($State) {
        foreach ($k in @('Source', 'Target')) {
            if ($State[$k] -and $Name -ieq $State[$k]) {
                return "'$Name' is the cutover $($k.ToLower()) recorded in the migration state file - it is not a throwaway"
            }
        }
    }
    $mine = $null
    foreach ($k in $Registrations.Keys) { if ($k -ieq $Name) { $mine = ConvertTo-AwnixNormPath $Registrations[$k] } }
    if ($mine) {
        if ($State -and $State['Target'] -and ((Split-Path -Leaf $mine) -ieq $State['Target'])) {
            return "'$Name' is registered at $mine, the cutover target's install dir - it holds migrated state"
        }
        foreach ($k in $Registrations.Keys) {
            if ($k -ieq $FleetDistro -and (ConvertTo-AwnixNormPath $Registrations[$k]) -eq $mine) {
                return "'$Name' shares its BasePath ($mine) with the fleet distro '$FleetDistro'"
            }
        }
    }
    return $null
}

function Test-AwnixDistroProtected {
    <# Live form: reads the resolver, the registry and the state file. $null = safe. #>
    param([Parameter(Mandatory)][string]$Name)
    return (Test-AwnixDistroProtectedPure -Name $Name -FleetDistro (Get-AitherFleetDistro) `
            -Registrations (Get-AwnixRegistrations) -State (Read-AwnixCutoverState))
}

function ConvertFrom-AwnixWslListVerbose {
    <# `wsl -l -v` text (NULs already stripped) -> name -> state. Pure. #>
    param([string]$Text)
    $out = @{}
    foreach ($line in ($Text -split "`r?`n")) {
        $t = $line.Trim().TrimStart('*').Trim()
        if (-not $t -or $t -match '^NAME\s+STATE') { continue }
        $parts = $t -split '\s+'
        if ($parts.Count -ge 3) { $out[$parts[0]] = $parts[1] }
    }
    return $out
}

function Test-AwnixCotenancyPure {
    <# $null when importing/starting $Name is allowed; otherwise the reason. Pure. #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FleetDistro,
        [hashtable]$States = @{}
    )
    if ($Name -ieq $FleetDistro) { return $null }
    foreach ($k in $States.Keys) {
        if ($k -ieq $FleetDistro -and $States[$k] -ne 'Stopped') {
            return "the fleet distro '$FleetDistro' is $($States[$k]) - a second distro in the shared utility VM has failed the fleet's vhdx writes (2026-09-20). Stop the fleet in a maintenance window first."
        }
    }
    return $null
}

function Test-AwnixCotenancy {
    <# Live form. A listing that cannot be read is a refusal too: "could not look" is not "clear". #>
    param([Parameter(Mandatory)][string]$Name)
    $raw = (& wsl.exe -l -v 2>&1 | Out-String) -replace "`0", ''
    if ($raw -match 'atastrophic failure|E_UNEXPECTED' -or -not $raw.Trim()) {
        return "could not read 'wsl -l -v' - refusing to start a second distro blind"
    }
    return (Test-AwnixCotenancyPure -Name $Name -FleetDistro (Get-AitherFleetDistro) -States (ConvertFrom-AwnixWslListVerbose $raw))
}

function Resolve-AwnixStageRoot {
    <#
      Where rootfs tarballs and throwaway distros land (Windows form, forward slashes).
      env AITHER_WSL_STAGE_DIR, else E:/AitherOS-Data/wsl when E: exists, else C:.
      C: had 3.8 GB free on 2026-09-27 and a fleet rootfs is ~3 GB before import.
    #>
    param([hashtable]$Env = $null, [scriptblock]$DriveExists = { param($d) Test-Path "${d}:\" })
    $v = if ($Env) { $Env['AITHER_WSL_STAGE_DIR'] } else { $env:AITHER_WSL_STAGE_DIR }
    if ($v -and $v.Trim()) { return ($v.Trim() -replace '\\', '/').TrimEnd('/') }
    if (& $DriveExists 'E') { return 'E:/AitherOS-Data/wsl' }
    return 'C:/AitherOS-Data/wsl'
}

function ConvertTo-AwnixWslPath {
    <# E:/x/y -> /mnt/e/x/y #>
    param([Parameter(Mandatory)][string]$Path)
    $p = $Path -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)$') { return "/mnt/$($Matches[1].ToLower())$($Matches[2])" }
    return $p
}

function Get-AwnixFreeBytes {
    <# Free bytes on the drive holding $Path, or -1 when unknown. #>
    param([Parameter(Mandatory)][string]$Path)
    if ($Path -notmatch '^([A-Za-z]):') { return -1 }
    try { return [int64](Get-PSDrive -Name $Matches[1] -ErrorAction Stop).Free } catch { return -1 }
}

function Test-AwnixFreeSpacePure {
    <# $null when $FreeBytes covers $NeedBytes plus a 2 GiB margin; otherwise the reason. #>
    param([int64]$FreeBytes, [int64]$NeedBytes, [string]$Where)
    if ($FreeBytes -lt 0) { return "could not read free space on $Where" }
    $margin = 2GB
    if ($FreeBytes -lt ($NeedBytes + $margin)) {
        return ("{0} has {1:N1} GB free; this needs {2:N1} GB + 2 GB margin. Set AITHER_WSL_STAGE_DIR to a roomier drive." -f $Where, ($FreeBytes / 1GB), ($NeedBytes / 1GB))
    }
    return $null
}

function Invoke-AwnixGuardSelfTest {
    <# Proves each refusal fires and does not cry wolf. Returns the failure count. #>
    function _ck([string]$n, [bool]$c) { if ($c) { Write-Host "  ok   $n" } else { Write-Host "  FAIL $n"; $script:__awg_fail++ } }
    $script:__awg_fail = 0
    $regs = @{ 'awnix' = 'C:/AitherOS-Data/wsl/aitheros-fleet'; 'awnix-rehearse' = 'E:/AitherOS-Data/wsl/awnix-rehearse'; 'twin' = 'C:\AitherOS-Data\wsl\aitheros-fleet\' }
    $st = @{ Source = 'Debian'; Target = 'aitheros-fleet' }
    _ck 'the fleet distro is protected' ([bool](Test-AwnixDistroProtectedPure -Name 'awnix' -FleetDistro 'awnix' -Registrations $regs -State $st))
    _ck 'the fleet distro is protected case-insensitively' ([bool](Test-AwnixDistroProtectedPure -Name 'AWNIX' -FleetDistro 'awnix'))
    _ck 'the cutover source is protected' ([bool](Test-AwnixDistroProtectedPure -Name 'Debian' -FleetDistro 'awnix' -State $st))
    _ck 'the cutover target name is protected' ([bool](Test-AwnixDistroProtectedPure -Name 'aitheros-fleet' -FleetDistro 'awnix' -State $st))
    _ck "a registration in the target's install dir is protected" ([bool](Test-AwnixDistroProtectedPure -Name 'awnix' -FleetDistro 'other' -Registrations $regs -State $st))
    _ck "a registration sharing the fleet's BasePath is protected" ([bool](Test-AwnixDistroProtectedPure -Name 'twin' -FleetDistro 'awnix' -Registrations $regs))
    _ck 'a throwaway rehearsal distro is NOT protected' ($null -eq (Test-AwnixDistroProtectedPure -Name 'awnix-rehearse' -FleetDistro 'awnix' -Registrations $regs -State $st))
    $lv = ConvertFrom-AwnixWslListVerbose "  NAME      STATE           VERSION`n* awnix     Running         2`n  NVIDIA-Workbench Stopped 2`n"
    _ck 'wsl -l -v parses the default-marked row' ($lv['awnix'] -eq 'Running')
    _ck 'importing beside a RUNNING fleet is refused' ([bool](Test-AwnixCotenancyPure -Name 'awnix-rehearse' -FleetDistro 'awnix' -States $lv))
    _ck 'importing beside a STOPPED fleet is allowed' ($null -eq (Test-AwnixCotenancyPure -Name 'awnix-rehearse' -FleetDistro 'awnix' -States @{ awnix = 'Stopped' }))
    _ck 'the fleet itself is never a co-tenant of itself' ($null -eq (Test-AwnixCotenancyPure -Name 'awnix' -FleetDistro 'awnix' -States $lv))
    _ck 'stage root honours AITHER_WSL_STAGE_DIR' ((Resolve-AwnixStageRoot -Env @{ AITHER_WSL_STAGE_DIR = 'F:\stage\' }) -eq 'F:/stage')
    _ck 'stage root defaults to E: when present' ((Resolve-AwnixStageRoot -Env @{} -DriveExists { param($d) $d -eq 'E' }) -eq 'E:/AitherOS-Data/wsl')
    _ck 'stage root falls back to C: without E:' ((Resolve-AwnixStageRoot -Env @{} -DriveExists { param($d) $false }) -eq 'C:/AitherOS-Data/wsl')
    _ck 'a Windows path maps to its /mnt form' ((ConvertTo-AwnixWslPath 'E:/AitherOS-Data/wsl') -eq '/mnt/e/AitherOS-Data/wsl')
    _ck '3.8 GB free refuses a 3 GB rootfs' ([bool](Test-AwnixFreeSpacePure -FreeBytes ([int64](3.8 * 1GB)) -NeedBytes 3GB -Where 'C:'))
    _ck '220 GB free admits a 3 GB rootfs' ($null -eq (Test-AwnixFreeSpacePure -FreeBytes 220GB -NeedBytes 3GB -Where 'E:'))
    _ck 'unknown free space refuses' ([bool](Test-AwnixFreeSpacePure -FreeBytes -1 -NeedBytes 1 -Where 'X:'))
    return $script:__awg_fail
}
