#Requires -Version 5.1
<#
.SYNOPSIS
    Build the Windows app, the self-update ZIP and the installer in one command.

.DESCRIPTION
    The Windows counterpart to tools/build_macos.sh, and the script the GitHub
    Actions Windows job runs - so a release built on a runner is produced by
    exactly the same steps as one built here.

    Outputs:
        dist\UniversalAudioStudio\UniversalAudioStudio.exe             the app
        dist\UniversalAudioStudio_<version>_update.zip                 self-update payload
        mysetup<versiondigits>.exe                                     installer

    Order matters in one place: UniversalAudioStudio.spec aborts unless
    dist\updater_cli.exe already exists, so the helper is built first.

.PARAMETER Python
    Interpreter to build with. Defaults to .venv\Scripts\python.exe when that
    exists (this machine), otherwise plain `python` (the CI runner, where
    actions/setup-python has already put it on PATH).

.PARAMETER Iscc
    Path to Inno Setup's ISCC.exe. Auto-detected when omitted.

.PARAMETER SkipTests
    Do not run the test suite first. Only useful for a quick local iteration.

.PARAMETER SkipDeps
    Do not run `pip install -r requirements.txt`.

.EXAMPLE
    pwsh -File tools/build_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$Iscc,
    [switch]$SkipTests,
    [switch]$SkipDeps
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message"
}

function Fail {
    param([string]$Message)
    Write-Host "::error::build_windows.ps1: $Message"
    exit 1
}

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments)
    Write-Host "    $FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Fail "'$FilePath $($Arguments -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

function Get-AppVersion {
    # Reuse the same helper the macOS build uses instead of re-implementing the
    # version.py parse here and letting the two drift apart.
    $version = (& $Python (Join-Path 'tools' 'get_version.py') | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($version)) {
        Fail 'could not read __version__ from version.py'
    }
    return $version.Trim()
}

function Find-Iscc {
    $candidates = @()
    if (-not [string]::IsNullOrEmpty(${env:ProgramFiles(x86)})) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
    }
    if (-not [string]::IsNullOrEmpty($env:ProgramFiles)) {
        $candidates += (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    }
    if (-not [string]::IsNullOrEmpty(${env:ProgramFiles(x86)})) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 5\ISCC.exe')
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $onPath = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    $Python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
}

Write-Step "0/7 Host: $([Environment]::OSVersion.VersionString)"
Write-Host "    project: $RepoRoot"
Write-Host "    python : $Python"
Invoke-Checked -FilePath $Python -Arguments @('--version')

Write-Step '1/7 Python dependencies'
if ($SkipDeps) {
    Write-Host '    skipped by request (-SkipDeps)'
} else {
    Invoke-Checked -FilePath $Python -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Checked -FilePath $Python -Arguments @('-m', 'pip', 'install', '-r', 'requirements.txt')
}

Write-Step '2/7 Bundled helper binaries'
if ($SkipDeps) {
    Write-Host '    skipped by request (-SkipDeps); assuming ffmpeg/ffprobe/yt-dlp are present'
} else {
    # Runs in-process so its own `exit 1` (with a ::error:: annotation) reports
    # the real cause instead of vanishing behind a nested shell.
    & (Join-Path $PSScriptRoot 'fetch_win_helpers.ps1')
    if ($LASTEXITCODE -ne 0) {
        Fail 'tools/fetch_win_helpers.ps1 failed'
    }
}

Write-Step '3/7 Test suite'
if ($SkipTests) {
    Write-Host '    skipped by request (-SkipTests)'
} else {
    Invoke-Checked -FilePath $Python -Arguments @('-m', 'unittest', 'discover', '-s', 'tests')
}

Write-Step '4/7 Self-update helper (updater_cli.exe)'
# Must be built BEFORE the main spec: UniversalAudioStudio.spec aborts when
# dist\updater_cli.exe is missing, because a build without it cannot self-update.
# This calls PyInstaller with $Python rather than running build_updaters.bat, so
# the build always uses the interpreter chosen above instead of whichever
# `python` happens to be first on PATH.
Invoke-Checked -FilePath $Python -Arguments @(
    '-m', 'PyInstaller', '--noconfirm', '--onefile', '--noconsole',
    '--name', 'updater_cli', 'updater_cli.py'
)

Write-Step '5/7 Building the app (PyInstaller)'
# Only the app bundle is cleaned: wiping all of dist\ would delete the
# updater_cli.exe built above, which the spec requires.
Remove-Item -Recurse -Force (Join-Path $RepoRoot 'build') -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $RepoRoot 'dist\UniversalAudioStudio') -ErrorAction SilentlyContinue
Invoke-Checked -FilePath $Python -Arguments @('-m', 'PyInstaller', 'UniversalAudioStudio.spec', '-y')

Write-Step '6/7 Self-update ZIP'
Invoke-Checked -FilePath $Python -Arguments @('_make_update_package.py')

Write-Step '7/7 Windows installer (Inno Setup)'
$version = Get-AppVersion
# 2.0.0 -> mysetup200, 2.1.0 -> mysetup210: the digits-only name keeps the
# existing installer's identity while tracking the version.
$outputBase = 'mysetup' + ($version -replace '\.', '')
if ([string]::IsNullOrWhiteSpace($Iscc)) {
    $Iscc = Find-Iscc
}
if ([string]::IsNullOrWhiteSpace($Iscc)) {
    Fail 'ISCC.exe (Inno Setup 6) was not found. Install it from https://jrsoftware.org/isdl.php or pass -Iscc <path>.'
}
Write-Host "    version $version -> $outputBase.exe"
Invoke-Checked -FilePath $Iscc -Arguments @(
    "/DAppVersion=$version", "/DOutputBase=$outputBase", 'installer_200.iss'
)

Write-Step 'Verifying the release artifacts'
$artifacts = @(
    'dist\UniversalAudioStudio\UniversalAudioStudio.exe',
    "dist\UniversalAudioStudio_${version}_update.zip",
    "$outputBase.exe"
)
foreach ($relative in $artifacts) {
    $full = Join-Path $RepoRoot $relative
    if (-not (Test-Path $full)) {
        Fail "expected artifact is missing: $relative"
    }
    Write-Host ("    {0,-46} {1,8:N1} MB" -f $relative, ((Get-Item $full).Length / 1MB))
}

Write-Host ''
Write-Host '============================================================'
Write-Host "Built Universal Audio Studio $version for Windows."
Write-Host '============================================================'
Write-Host ''
Write-Host 'To release it (all of this happens on GitHub now):'
Write-Host "  1. bump version.py to the next version and push"
Write-Host "  2. GitHub -> Releases -> Draft a new release -> tag v$version -> Publish"
Write-Host '     The workflow rebuilds Windows and macOS and attaches:'
Write-Host '       UniversalAudioStudio-<version>-arm64.dmg   (Apple Silicon)'
Write-Host '       UniversalAudioStudio-<version>-x86_64.dmg  (Intel)'
Write-Host "       UniversalAudioStudio_${version}_update.zip  (in-app self-update)"
Write-Host "       $outputBase.exe  (fresh install)"
Write-Host ''
Write-Host 'Installed copies then update themselves from that Release.'
