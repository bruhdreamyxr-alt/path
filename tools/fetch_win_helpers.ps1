#Requires -Version 5.1
<#
.SYNOPSIS
    Download the Windows helper binaries that get bundled into the app.

.DESCRIPTION
    UniversalAudioStudio.spec bundles ffmpeg.exe / ffprobe.exe / yt-dlp.exe
    (plus aria2c.exe when it is present) from the repository root. Those files
    are deliberately NOT committed:

        ffmpeg.exe   227 MB
        ffprobe.exe  227 MB   <- GitHub rejects any single file over 100 MB

    so this script fetches them. It is run both locally (via
    tools/build_windows.ps1) and by .github/workflows/release.yml on the Windows
    runner, which starts from a fresh checkout with none of them.

    Verified download sources (all static, no installer required):
        ffmpeg / ffprobe : gyan.dev release-essentials, BtbN as the fallback
        yt-dlp           : official yt-dlp release asset (yt-dlp.exe)
        aria2c           : official aria2 win-64bit build (pinned version)

.PARAMETER SkipAria2
    Do not fetch aria2c.exe. The app then falls back to yt-dlp's built-in
    downloader: slightly slower, completely fine.

.PARAMETER Force
    Re-download helpers that are already present.

.EXAMPLE
    pwsh -File tools/fetch_win_helpers.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipAria2,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Invoke-WebRequest renders a progress bar by default, which makes it several
# times slower - and the ffmpeg archive is ~110 MB.
$ProgressPreference = 'SilentlyContinue'

if ($PSVersionTable.PSEdition -eq 'Desktop') {
    # Windows PowerShell 5.1 can still default to TLS 1.0, which GitHub and
    # gyan.dev both refuse.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

$RepoRoot = Split-Path -Parent $PSScriptRoot

$FFMPEG_URLS = @(
    'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip'
)
$YTDLP_URL = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe'
$ARIA2_URL = 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip'

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message"
}

function Fail {
    param([string]$Message)
    # The annotation makes the run page name the real problem instead of
    # surfacing a bare "Process completed with exit code 1".
    Write-Host "::error::fetch_win_helpers.ps1: $Message"
    exit 1
}

function Get-RemoteFile {
    param([string]$Url, [string]$Destination)
    Write-Host "    GET $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Destination -MaximumRedirection 10
}

function Expand-ZipInto {
    param([string]$ZipPath, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $Destination) {
        Remove-Item -Recurse -Force $Destination
    }
    # .NET's extractor is markedly faster than Expand-Archive on large archives.
    [System.IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $Destination)
}

function Get-HelperFromZip {
    param(
        [string[]]$Urls,
        [string]$FileName,
        [string]$Label
    )

    $target = Join-Path $RepoRoot $FileName
    if ((Test-Path $target) -and -not $Force) {
        Write-Host "    $FileName already present - skipping (pass -Force to re-download)"
        return
    }

    $temp = Join-Path ([IO.Path]::GetTempPath()) ('uas-helpers-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temp | Out-Null
    try {
        $zip = Join-Path $temp 'payload.zip'
        $downloaded = $false
        foreach ($url in $Urls) {
            try {
                Get-RemoteFile -Url $url -Destination $zip
                $downloaded = $true
                break
            } catch {
                Write-Host "    !! mirror failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
            }
        }
        if (-not $downloaded) {
            throw "could not download $Label from any known mirror"
        }

        $extracted = Join-Path $temp 'extracted'
        Expand-ZipInto -ZipPath $zip -Destination $extracted
        $found = Get-ChildItem -Path $extracted -Recurse -File -Filter $FileName |
            Select-Object -First 1
        if (-not $found) {
            throw "'$FileName' was not found inside the downloaded archive"
        }
        Copy-Item -Path $found.FullName -Destination $target -Force
    } finally {
        Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
    }
}

function Assert-HelperRuns {
    param(
        [string]$FileName,
        [string[]]$VersionArgs
    )

    $path = Join-Path $RepoRoot $FileName
    if (-not (Test-Path $path)) {
        Fail "$FileName is missing after the fetch step"
    }
    $sizeMb = [Math]::Round((Get-Item $path).Length / 1MB, 1)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $firstLine = (& $path @VersionArgs 2>&1 | Select-Object -First 1)
    } catch {
        $firstLine = $null
    } finally {
        $ErrorActionPreference = $previous
    }

    if ([string]::IsNullOrWhiteSpace($firstLine)) {
        Fail "$FileName is present but will not run - every download for the user would fail"
    }
    Write-Host ("    {0,-12} {1,7} MB  {2}" -f $FileName, $sizeMb, $firstLine.Trim())
}

Write-Step "Fetching Windows helper binaries into $RepoRoot"

Write-Host '    ffmpeg + ffprobe'
Get-HelperFromZip -Urls $FFMPEG_URLS -FileName 'ffmpeg.exe' -Label 'ffmpeg'
Get-HelperFromZip -Urls $FFMPEG_URLS -FileName 'ffprobe.exe' -Label 'ffprobe'

Write-Host '    yt-dlp'
$ytdlpTarget = Join-Path $RepoRoot 'yt-dlp.exe'
if ((Test-Path $ytdlpTarget) -and -not $Force) {
    Write-Host '    yt-dlp.exe already present - skipping (pass -Force to re-download)'
} else {
    Get-RemoteFile -Url $YTDLP_URL -Destination $ytdlpTarget
}

Write-Host '    aria2c (optional accelerator)'
if ($SkipAria2) {
    Write-Host '    aria2c: skipped by request; downloads fall back to yt-dlp' -ForegroundColor DarkYellow
} else {
    try {
        Get-HelperFromZip -Urls @($ARIA2_URL) -FileName 'aria2c.exe' -Label 'aria2c'
    } catch {
        # Never fatal: aria2 only accelerates downloads, and the app already
        # falls back to yt-dlp's own downloader when it is absent.
        Write-Host "    !! aria2c unavailable ($($_.Exception.Message)) - continuing without it" -ForegroundColor DarkYellow
    }
}

Write-Step 'Verifying every bundled helper actually runs'
Assert-HelperRuns -FileName 'ffmpeg.exe' -VersionArgs @('-version')
Assert-HelperRuns -FileName 'ffprobe.exe' -VersionArgs @('-version')
Assert-HelperRuns -FileName 'yt-dlp.exe' -VersionArgs @('--version')
if (Test-Path (Join-Path $RepoRoot 'aria2c.exe')) {
    Assert-HelperRuns -FileName 'aria2c.exe' -VersionArgs @('--version')
}

Write-Host ''
Write-Host 'Done. Now build the app:  pwsh -File tools/build_windows.ps1'
