<#
.SYNOPSIS
  Offer an installed awnix WSL2 distro to a fleet's placement controller.

.DESCRIPTION
  The Windows side of `awnix fleet join`. Every awnix image carries the fleet
  verb (it is part of the public base), so any installed awnix distro can be
  offered to a fleet you run. The installer, install-awnix-wsl.ps1, takes no
  fleet parameter: installing a distro and offering it are two separate acts.

  Two commands, in this order, on the Windows machine that becomes a node:

    1. install the distro:
         .\install-awnix-wsl.ps1 -Name node1
    2. offer it to the fleet you run:
         .\awnix-fleet-wsl.ps1 -Name node1 -FleetController https://fleet.example:8149 -FleetToken $tok

  Step 2 hands the distro one line (controller address, enrollment token the
  fleet operator minted for this machine, optional certificate pin, optional
  node id) as the setup seed, and applies it. The distro is measured, enrolled
  as PENDING, and its agent runs in observe mode. Nothing is started in the
  distro by the join; the operator approves the node on the controller.

  The seed is written through standard input and lands root-only
  (/etc/awnix/seed.json, deleted by awnix-setup once applied): the token is
  never an argument of any process this script starts, never a file on the
  Windows side, and never printed. A self-signed controller also needs
  -FleetPinSha256 (the SHA-256 fingerprint of its certificate). -FleetNodeId
  is the id the fleet operator uses for this machine (the token is usually
  bound to it); without it the distro enrolls under its hostname.

  Every command inside the distro goes through the fleet-host transport
  (.DEPLOYMENT/scripts/fleet-distro.ps1, as transport wsl:<Name>), so this
  script runs from an AitherOS checkout and refuses anywhere else.
  Without that checkout, run the verb yourself inside the distro, as root:
  awnix fleet join <controller-url> <token>  (see awnix(8)).

  It refuses, and changes nothing, when: the distro does not answer; the image
  has no fleet verb; the distro already holds a seed. It never installs,
  replaces or unregisters a distro.

.EXAMPLE
  .\awnix-fleet-wsl.ps1 -Name node1 -FleetController https://fleet.example:8149 -FleetToken $tok
  .\awnix-fleet-wsl.ps1 -Name node1 -FleetController https://192.0.2.10:8149 -FleetToken $tok -FleetPinSha256 $pin -FleetNodeId node1-wsl
  .\awnix-fleet-wsl.ps1 -SelfTest
#>
[CmdletBinding()]
param(
    [string]$Name = 'awnix',
    [string]$FleetController = '',
    [string]$FleetToken = '',
    [string]$FleetPinSha256 = '',
    [string]$FleetNodeId = '',
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'

function Say  { param([string]$m) Write-Host "  $m" }
function Die  { param([string]$m) throw "awnix-fleet-wsl: $m" }

# The ONE resolver of how a command reaches a fleet host. Nothing below builds a
# distro command line of its own (FDT001).
$TransportLib = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'scripts\fleet-distro.ps1'
if (-not (Test-Path -LiteralPath $TransportLib)) {
    Die "needs the fleet-host transport at $TransportLib - run this from an AitherOS checkout"
}
. $TransportLib

# -- pure helpers (self-tested; no WSL, no I/O) -------------------------------

function Test-FleetJoinArgs {
    <# $null when the fleet parameters are usable; else the reason. The
       character sets are deliberately narrow: the values are placed in a JSON
       string and a one-line join request, so nothing that needs escaping in
       either is accepted. Pure. #>
    param([string]$Controller, [string]$Token, [string]$Pin, [string]$NodeId = '')
    if (-not $Controller) { return '-FleetController is required to join a fleet (it was not given)' }
    if (-not $Token) { return '-FleetToken is required to join a fleet (it was not given)' }
    if ($Controller -notmatch '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$') {
        return '-FleetController must be an http(s) URL such as https://fleet.example:8149'
    }
    if ($Token -notmatch '^[A-Za-z0-9._~+/=-]{16,4096}$') {
        return '-FleetToken does not look like an enrollment token'
    }
    if ($Pin -and ($Pin -notmatch '^[0-9A-Fa-f]{64}$')) {
        return '-FleetPinSha256 must be the 64 hex digits of the controller certificate SHA-256'
    }
    if ($NodeId -and ($NodeId -cnotmatch '^[a-z0-9][a-z0-9._-]{0,62}$')) {
        return '-FleetNodeId must be a fleet node id: lowercase letters, digits, . _ -'
    }
    return $null
}

function New-FleetSeedJson {
    <# The seed that answers the "Join a fleet" setup step. The step is
       kind=secret: awnix-setup writes the value to a 0600 file and runs the
       verb with --request-file, so the token never reaches argv. Call only
       with values Test-FleetJoinArgs accepted. Pure. #>
    param([string]$Controller, [string]$Token, [string]$Pin, [string]$NodeId = '')
    $line = "$Controller $Token"
    if ($Pin) { $line = "$line sha256:$($Pin.ToLower())" }
    if ($NodeId) { $line = "$line node=$NodeId" }
    return '{"schema": 1, "setup": {"fleet": "' + $line + '"}}'
}

function Get-FleetJoinSteps {
    <# Every command this script runs inside the distro, by name. Fixed text:
       no parameter of this script is part of any of them. Pure. #>
    return [ordered]@{
        Alive     = @('/usr/bin/true')
        HasVerb   = @('/usr/bin/test', '-x', '/usr/libexec/awnix/awnix-fleet')
        HasSeed   = @('/usr/bin/test', '-e', '/etc/awnix/seed.json')
        # the seed arrives on stdin; umask before the redirect creates the file 0600
        WriteSeed = @('/bin/sh', '-c', 'mkdir -p -m 0755 /etc/awnix && umask 077 && cat > /etc/awnix/seed.json')
        SeedLanded = @('/usr/bin/test', '-s', '/etc/awnix/seed.json')
        DropSeed  = @('/usr/bin/rm', '-f', '/etc/awnix/seed.json')
        Apply     = @('/usr/bin/awnix-setup', '--apply-seed')
        Joined    = @('/usr/bin/test', '-e', '/etc/aither/placement/agent.env')
        InFlight  = @('/usr/bin/test', '-e', '/etc/aither/placement/join.request')
        Status    = @('/usr/bin/awnix', 'fleet', 'status')
    }
}

function Get-NodeHost {
    <# The transport object for the distro: validated by the resolver. #>
    param([string]$Distro)
    $p = ConvertFrom-AitherFleetTransport -Value "wsl:$Distro" -Distro $Distro
    return New-AitherFleetHost $p[0] $p[1] $p[2] 'awnix-fleet-wsl'
}

function Get-NodeArgv {
    <# The argv (exe first) that runs -Command as root inside the distro. #>
    param($NodeHost, [string[]]$Command)
    return @(Get-AitherFleetArgv -Command $Command -User 'root' -FleetHost $NodeHost)
}

# -- live helpers -------------------------------------------------------------

function Invoke-Node {
    <# Run one named step. -Quiet drops its output (a probe); -StdinText feeds
       it on standard input. Returns the exit code. #>
    param($NodeHost, [string]$Step, [switch]$Quiet, [string]$StdinText = $null)
    $argv = Get-NodeArgv -NodeHost $NodeHost -Command (Get-FleetJoinSteps)[$Step]
    $exe = $argv[0]; $rest = @($argv | Select-Object -Skip 1)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # a native stderr line is not a terminating error
    try {
        if ($StdinText) {
            $StdinText | & $exe @rest *> $null
        } elseif ($Quiet) {
            & $exe @rest *> $null
        } else {
            & $exe @rest | ForEach-Object { Write-Host "    $_" }
        }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevEap }
}

function Invoke-FleetJoin {
    <# Seed, apply, then report what is TRUE: "the seed was applied" and "the
       box joined" are different facts, so the answer is whether the agent's
       env file exists. A join in flight still holds its request file, so that
       is waited for. Returns $null, or why the distro did not join. #>
    param($NodeHost, [string]$Distro, [string]$SeedJson)
    if ((Invoke-Node $NodeHost 'Alive' -Quiet) -ne 0) {
        return "the distro '$Distro' does not answer (not installed, or it does not start)"
    }
    if ((Invoke-Node $NodeHost 'HasVerb' -Quiet) -ne 0) {
        return "this image has no 'awnix fleet' verb - install the distro from a current awnix image"
    }
    if ((Invoke-Node $NodeHost 'HasSeed' -Quiet) -eq 0) {
        return 'the distro already holds /etc/awnix/seed.json and it was left alone'
    }
    $wrote = Invoke-Node $NodeHost 'WriteSeed' -StdinText $SeedJson
    if (($wrote -ne 0) -or ((Invoke-Node $NodeHost 'SeedLanded' -Quiet) -ne 0)) {
        # no seed was there before this run, so what is there now is this run's
        [void](Invoke-Node $NodeHost 'DropSeed' -Quiet)
        return 'could not write /etc/awnix/seed.json inside the distro (nothing was applied)'
    }
    Say 'seeded (/etc/awnix/seed.json, root-only); joining: measure, enroll as pending, agent in observe mode'
    [void](Invoke-Node $NodeHost 'Apply')
    $joined = $false
    for ($i = 0; $i -lt 45; $i++) {
        if ((Invoke-Node $NodeHost 'Joined' -Quiet) -eq 0) { $joined = $true; break }
        if ((Invoke-Node $NodeHost 'InFlight' -Quiet) -ne 0) { break }
        Start-Sleep -Seconds 2
    }
    [void](Invoke-Node $NodeHost 'Status')
    if ($joined) { return $null }
    return 'the join did not complete (the status above says why; retry inside the distro with: awnix fleet join <controller-url> @<token-file>)'
}

# -- self-test (pure; touches neither WSL nor the filesystem) -----------------

function Invoke-SelfTest {
    $script:fail = 0
    function Ck { param([string]$Label, [bool]$Ok)
        if ($Ok) { Write-Host "  ok   $Label" } else { Write-Host "  FAIL $Label"; $script:fail++ } }

    $tok = 'enroll-token-0123456789abcdef'
    $pin = 'AB' * 32
    Ck 'controller and token pass' ($null -eq (Test-FleetJoinArgs 'https://fleet.example:8149' $tok ''))
    Ck 'controller, token and pin pass' ($null -eq (Test-FleetJoinArgs 'https://192.0.2.10:8149/base' $tok $pin))
    Ck 'no fleet parameters is refused' ([bool](Test-FleetJoinArgs '' '' ''))
    Ck 'a token without a controller is refused' ([bool](Test-FleetJoinArgs '' $tok ''))
    Ck 'a controller without a token is refused' ([bool](Test-FleetJoinArgs 'https://fleet.example' '' ''))
    Ck 'a non-URL controller is refused' ([bool](Test-FleetJoinArgs 'fleet.example:8149' $tok ''))
    Ck 'a controller that could break the seed JSON is refused' ([bool](Test-FleetJoinArgs 'https://fleet.example/"x' $tok ''))
    Ck 'a token with a space or quote is refused' (([bool](Test-FleetJoinArgs 'https://fleet.example' 'enroll token 0123456789' '')) -and ([bool](Test-FleetJoinArgs 'https://fleet.example' ($tok + '"') '')))
    Ck 'a short pin is refused' ([bool](Test-FleetJoinArgs 'https://fleet.example' $tok 'abcd'))
    Ck 'a node id passes and a hostname-shaped one is refused' (($null -eq (Test-FleetJoinArgs 'https://fleet.example' $tok '' 'node1-wsl')) -and ([bool](Test-FleetJoinArgs 'https://fleet.example' $tok '' 'DESKTOP ABC')))
    $seed = New-FleetSeedJson 'https://fleet.example:8149' $tok '' | ConvertFrom-Json
    Ck 'the seed answers the fleet step with one line' (($seed.schema -eq 1) -and ($seed.setup.fleet -eq "https://fleet.example:8149 $tok"))
    $seed2 = New-FleetSeedJson 'https://fleet.example:8149' $tok $pin | ConvertFrom-Json
    Ck 'the seed carries the pin as a lowercase third field' ($seed2.setup.fleet -ceq ("https://fleet.example:8149 $tok sha256:" + $pin.ToLower()))
    Ck 'the seed names no admin, disk or hostname' (@($seed.PSObject.Properties.Name) -join ',' -eq 'schema,setup')
    $seed3 = New-FleetSeedJson 'https://fleet.example:8149' $tok $pin 'node1-wsl' | ConvertFrom-Json
    Ck 'the seed carries the node id as node=<id>' ($seed3.setup.fleet -ceq ("https://fleet.example:8149 $tok sha256:" + $pin.ToLower() + ' node=node1-wsl'))

    $node = Get-NodeHost 'node1'
    Ck 'the distro is reached as transport wsl:<Name>' ($node.Spec -ceq 'wsl:node1')
    $argv = Get-NodeArgv -NodeHost $node -Command @('/usr/bin/true')
    Ck 'a command runs as root with its argv intact' (($argv[0] -ceq 'wsl.exe') -and ((@($argv | Select-Object -Skip 1) -join ' ') -ceq '-d node1 -u root --exec /usr/bin/true'))
    $bad = $false
    try { [void](Get-NodeHost 'no such distro') } catch { $bad = $true }
    Ck 'a distro name the resolver refuses is refused' $bad
    $steps = Get-FleetJoinSteps
    $all = (@($steps.Keys) | ForEach-Object { (Get-NodeArgv -NodeHost $node -Command $steps[$_]) -join ' ' }) -join "`n"
    Ck 'no step carries the token, the controller or the seed' (($all -notmatch [regex]::Escape($tok)) -and ($all -notmatch 'fleet\.example') -and ($all -notmatch 'schema'))
    Ck 'the seed is written from stdin under umask 077' (($steps['WriteSeed'] -join ' ') -ceq '/bin/sh -c mkdir -p -m 0755 /etc/awnix && umask 077 && cat > /etc/awnix/seed.json')
    Ck 'the seed is applied by awnix-setup, which runs the verb with a request file' (($steps['Apply'] -join ' ') -ceq '/usr/bin/awnix-setup --apply-seed')
    Ck 'no step starts, stops or unregisters anything' ($all -notmatch 'systemctl|podman|--unregister|--import|--terminate|--shutdown')

    Write-Host ''
    if ($script:fail -eq 0) { Write-Host 'SELF-TEST PASS'; return 0 }
    Write-Host "SELF-TEST FAILED ($script:fail)"; return $script:fail
}

# -- main ---------------------------------------------------------------------

if ($SelfTest) { exit (Invoke-SelfTest) }

try {
    # Judged before the distro is touched.
    $why = Test-FleetJoinArgs $FleetController $FleetToken $FleetPinSha256 $FleetNodeId
    if ($why) { Die $why }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Die 'WSL is not installed on this machine (wsl.exe not found)'
    }
    try { $node = Get-NodeHost $Name } catch { Die "invalid -Name '$Name' (letters, digits, dot, dash, underscore)" }

    Say "offering '$Name' to the fleet at $FleetController"
    $problem = Invoke-FleetJoin -NodeHost $node -Distro $Name -SeedJson (New-FleetSeedJson $FleetController $FleetToken $FleetPinSha256 $FleetNodeId)
    if ($problem) { Die "'$Name' did NOT join the fleet: $problem" }
    Say "OK - '$Name' is enrolled with $FleetController and waits as PENDING until the operator approves it there."
    Say 'its agent runs in observe mode - nothing was started. Check inside the distro: awnix fleet status'
} catch {
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
