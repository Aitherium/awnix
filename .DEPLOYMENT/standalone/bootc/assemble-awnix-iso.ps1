<#
.SYNOPSIS
  Turn the downloaded awnix release parts into a bootable ISO. Ships WITH the release.

.DESCRIPTION
  The ISO is ~2.8 GB and a GitHub release asset is capped at 2 GiB, so it is published as
  awnix-x86_64.iso.00.part, .01.part, ... plus a SHA256SUMS. Without this script a reader
  lands on a release page with no file named *.iso on it and reasonably concludes the
  download is broken -- a silent failure in documentation form.

  The Windows half exists because most people writing a boot USB are on Windows, and the
  usual advice there is wrong in a way that produces a corrupt image:

    copy /b a.part + b.part out.iso     <-- ASCII mode; can stop at a 0x1A (EOF) byte
    Get-Content a.part, b.part | Set-Content out.iso   <-- decodes bytes as TEXT

  Both "succeed" and yield an unbootable ISO, which reads as a bad build rather than a bad
  join. This uses raw FileStreams, so the bytes are copied verbatim, and it streams in
  chunks rather than loading ~2.8 GB into memory.

  Two further traps this handles:
    * SHA256SUMS records the digest of the ASSEMBLED image, not of any part. Checking a
      part against it fails on a download that is perfectly fine.
    * Parts are concatenated in NUMERIC order, not lexical -- a 10th part would otherwise
      sort between 01 and 02.

.EXAMPLE
  .\assemble-awnix-iso.ps1
.EXAMPLE
  .\assemble-awnix-iso.ps1 -Dir $HOME\Downloads -Out D:\awnix.iso -KeepParts
.EXAMPLE
  .\assemble-awnix-iso.ps1 -SelfTest
#>
[CmdletBinding()]
param(
  [string]$Dir = ".",
  [string]$Out = "",
  [switch]$KeepParts,
  [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'

function Get-Parts {
  param([string]$Path)
  # Sort on the numeric index between the last two dots. A plain Sort-Object on Name puts
  # part 10 before part 02, which silently produces a scrambled image.
  Get-ChildItem -Path $Path -Filter '*.iso.*.part' -File -ErrorAction SilentlyContinue |
    Sort-Object { [int]($_.Name -replace '^.*\.iso\.(\d+)\.part$', '$1') }
}

function Join-Parts {
  param([System.IO.FileInfo[]]$Parts, [string]$Destination)
  $outStream = [System.IO.File]::Create($Destination)
  try {
    $buffer = New-Object byte[] (4MB)
    foreach ($p in $Parts) {
      $inStream = [System.IO.File]::OpenRead($p.FullName)
      try {
        while (($read = $inStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
          $outStream.Write($buffer, 0, $read)
        }
      } finally { $inStream.Dispose() }
    }
  } finally { $outStream.Dispose() }
}

# ── self-test ────────────────────────────────────────────────────────────────────────
if ($SelfTest) {
  $fail = $false
  function Check($got, $want, $label) {
    if ($got -eq $want) { Write-Host "  ok   $label" }
    else { Write-Host "  FAIL $label (got '$got' want '$want')"; $script:fail = $true }
  }

  $t = Join-Path ([System.IO.Path]::GetTempPath()) ("awnixst_" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $t | Out-Null
  try {
    [System.IO.File]::WriteAllBytes((Join-Path $t 'x.iso.00.part'), [byte[]](72,69,76,76,79))      # HELLO
    [System.IO.File]::WriteAllBytes((Join-Path $t 'x.iso.01.part'), [byte[]](87,79,82,76,68))      # WORLD
    [System.IO.File]::WriteAllBytes((Join-Path $t 'x.iso.10.part'), [byte[]](84,69,78,84,72))      # TENTH

    $parts = Get-Parts $t
    Check (($parts | ForEach-Object Name) -join ' ') 'x.iso.00.part x.iso.01.part x.iso.10.part' `
          'parts sort NUMERICALLY, not lexically'

    $dest = Join-Path $t 'joined.bin'
    Join-Parts -Parts $parts -Destination $dest
    Check ([System.IO.File]::ReadAllText($dest)) 'HELLOWORLDTENTH' 'join concatenates in that order'

    # A byte that ASCII-mode copy treats as EOF must survive verbatim -- this is the
    # specific corruption `copy /b` avoids and `Get-Content|Set-Content` does not.
    [System.IO.File]::WriteAllBytes((Join-Path $t 'y.iso.00.part'), [byte[]](1,26,2))
    [System.IO.File]::WriteAllBytes((Join-Path $t 'y.iso.01.part'), [byte[]](3,0,4))
    $yp = Get-Parts $t | Where-Object { $_.Name -like 'y.*' }
    $ydest = Join-Path $t 'y.bin'
    Join-Parts -Parts $yp -Destination $ydest
    $bytes = [System.IO.File]::ReadAllBytes($ydest)
    Check ($bytes -join ',') '1,26,2,3,0,4' 'binary-safe: 0x1A and 0x00 survive the join'

    $empty = Join-Path $t 'none'
    New-Item -ItemType Directory -Path $empty | Out-Null
    Check ((Get-Parts $empty | Measure-Object).Count) 0 'an empty directory yields no parts'

    $h = (Get-FileHash -Path $dest -Algorithm SHA256).Hash
    Check $h.Length 64 'Get-FileHash returns a sha256'
  } finally { Remove-Item -Recurse -Force $t -ErrorAction SilentlyContinue }

  if ($fail) { Write-Host 'SELF-TEST FAILED'; exit 1 }
  Write-Host 'SELF-TEST PASS'; exit 0
}

# ── assemble ─────────────────────────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $Dir -PathType Container)) {
  Write-Error "not a directory: $Dir"; exit 2
}

$parts = @(Get-Parts $Dir)
if ($parts.Count -eq 0) {
  Write-Host "assemble-awnix-iso: no *.iso.*.part files in '$Dir'."
  Write-Host "  Download every part AND the SHA256SUMS file from the release into one"
  Write-Host "  folder, then run this from there (or pass -Dir)."
  exit 1
}

$stem = $parts[0].Name -replace '\.\d+\.part$', ''
if (-not $Out) { $Out = Join-Path $Dir $stem }

Write-Host 'assemble-awnix-iso'
Write-Host "  parts : $($parts.Count)"
$parts | ForEach-Object { Write-Host "          $($_.Name)" }
Write-Host "  out   : $Out"

$sums = Join-Path $Dir 'SHA256SUMS'
if (-not (Test-Path -LiteralPath $sums)) {
  Write-Host "assemble-awnix-iso: SHA256SUMS not found next to the parts -- refusing to"
  Write-Host "  present an UNVERIFIED image as done. It is on the same release page."
  exit 2
}

# Build to a temp name; only move into place once the digest agrees. A half-written file
# named *.iso is worse than no file, because it looks usable.
$tmp = "$Out.assembling"
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
try {
  Join-Parts -Parts $parts -Destination $tmp
} catch {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
  Write-Error "concatenation failed (out of disk?) -- nothing was moved into place: $_"
  exit 1
}

# Match on the DIGEST column: the recorded name is the builder's internal one and differs
# from the published part names.
$want = (Select-String -Path $sums -Pattern '\.iso$' | Select-Object -First 1).Line -split '\s+' | Select-Object -First 1
if (-not $want) {
  Remove-Item -LiteralPath $tmp -Force
  Write-Host 'assemble-awnix-iso: SHA256SUMS has no .iso line -- cannot verify'; exit 2
}

$got = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
if ($got -ne $want.ToLower()) {
  Remove-Item -LiteralPath $tmp -Force
  Write-Host 'assemble-awnix-iso: CHECKSUM MISMATCH -- the assembled image is not the published one.'
  Write-Host "     expected $want"
  Write-Host "     got      $got"
  Write-Host '     A part is truncated or missing. Re-download the parts; the bad image was'
  Write-Host '     deleted rather than left on disk looking usable.'
  exit 1
}

Move-Item -LiteralPath $tmp -Destination $Out -Force
Write-Host "  sha256: $got  VERIFIED"

if (-not $KeepParts) {
  $parts | Remove-Item -Force
  Write-Host '  parts removed (-KeepParts to retain them)'
}

Write-Host ''
Write-Host "  Ready: $Out"
Write-Host '  Write it to a USB stick, or attach it as a VM boot disc.'
exit 0
