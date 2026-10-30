<#
.SYNOPSIS
  PRISM installer for Windows (x64).

.DESCRIPTION
  Downloads the latest release, checks it against the SHA256SUMS published
  beside it, installs it under %LOCALAPPDATA%\PRISM (no admin rights), adds a
  `prism` command to your PATH and a Start menu shortcut.

  Run in PowerShell:
    irm https://raw.githubusercontent.com/rohith-rajeev/PRISM/main/install.ps1 | iex

  A specific release, or removal:
    $env:PRISM_VERSION = "v4.5"; irm https://raw.githubusercontent.com/rohith-rajeev/PRISM/main/install.ps1 | iex
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/rohith-rajeev/PRISM/main/install.ps1))) -Uninstall

.PARAMETER Version
  Release tag to install, e.g. v4.5. Default: the latest (or $env:PRISM_VERSION).

.PARAMETER Uninstall
  Remove what this script installed. Settings in %USERPROFILE%\.prism are kept.
#>
param(
    [string]$Version = $(if ($env:PRISM_VERSION) { $env:PRISM_VERSION } else { "latest" }),
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is far slower with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo    = "rohith-rajeev/PRISM"
$Asset   = "PRISM-windows-x86_64.zip"
$Root    = Join-Path $env:LOCALAPPDATA "PRISM"
$AppDir  = Join-Path $Root "app"                  # PRISM.exe
$BinDir  = Join-Path $Root "bin"                  # prism.cmd, the one folder on PATH
$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "PRISM.lnk"

function Remove-FromUserPath([string]$dir) {
    $cur = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $cur) { return }
    $keep = ($cur -split ";") | Where-Object { $_ -and ($_.TrimEnd("\") -ne $dir.TrimEnd("\")) }
    [Environment]::SetEnvironmentVariable("Path", ($keep -join ";"), "User")
}

if ($Uninstall) {
    Remove-FromUserPath $BinDir
    if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force }
    if (Test-Path $Root)     { Remove-Item $Root -Recurse -Force }
    Write-Host "PRISM removed. Your settings in $env:USERPROFILE\.prism were left alone (delete that folder to remove them too)."
    return
}

if (-not [Environment]::Is64BitOperatingSystem) { throw "PRISM needs 64-bit Windows." }

if ($Version -eq "latest") {
    $Base = "https://github.com/$Repo/releases/latest/download"
} else {
    if ($Version -notlike "v*") { $Version = "v$Version" }
    $Base = "https://github.com/$Repo/releases/download/$Version"
}

$Tmp = Join-Path ([IO.Path]::GetTempPath()) ("prism-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
    Write-Host "Downloading $Asset ($Version) ..."
    try { Invoke-WebRequest -UseBasicParsing "$Base/$Asset" -OutFile (Join-Path $Tmp $Asset) }
    catch { throw "download failed - is '$Version' a real release? See https://github.com/$Repo/releases" }
    Invoke-WebRequest -UseBasicParsing "$Base/SHA256SUMS" -OutFile (Join-Path $Tmp "SHA256SUMS")

    # SHA256SUMS lines look like "<hash>  <file>"
    $line = Get-Content (Join-Path $Tmp "SHA256SUMS") | Where-Object { $_ -match ("\s" + [regex]::Escape($Asset) + "$") } | Select-Object -First 1
    if (-not $line) { throw "$Asset is not listed in SHA256SUMS" }
    $want = ($line -split "\s+")[0].ToLower()
    $got  = (Get-FileHash (Join-Path $Tmp $Asset) -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $want) { throw "checksum mismatch - the download is corrupt or has been tampered with; nothing was installed" }
    Write-Host "Checksum OK."

    Expand-Archive -Path (Join-Path $Tmp $Asset) -DestinationPath (Join-Path $Tmp "x") -Force
    $exe = Join-Path $Tmp "x\PRISM.exe"
    if (-not (Test-Path $exe)) { throw "the archive has no PRISM.exe" }
    Unblock-File $exe

    New-Item -ItemType Directory -Force -Path $AppDir, $BinDir | Out-Null
    $target = Join-Path $AppDir "PRISM.exe"
    # A running PRISM.exe cannot be overwritten, but it can be renamed away.
    if (Test-Path $target) {
        $old = "$target.old"
        if (Test-Path $old) { Remove-Item $old -Force -ErrorAction SilentlyContinue }
        try { Rename-Item $target $old -Force } catch { throw "PRISM is running - close it and run the installer again." }
    }
    Copy-Item $exe $target -Force
    Remove-Item "$target.old" -Force -ErrorAction SilentlyContinue

    # PRISM.exe is a windowed program, so a terminal does not wait for it and
    # the prompt comes straight back. `start /wait` makes `prism run 214` behave
    # like any other command: it holds the prompt until the review ends.
    $shim = "@echo off`r`nstart `"`" /wait `"%~dp0..\app\PRISM.exe`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [IO.File]::WriteAllText((Join-Path $BinDir "prism.cmd"), $shim, [Text.Encoding]::ASCII)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (($userPath -split ";") | Where-Object { $_.TrimEnd("\") -eq $BinDir.TrimEnd("\") })) {
        $parts = @($userPath -split ";" | Where-Object { $_ }) + $BinDir
        [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
        $newPath = $true
    }

    $ws = New-Object -ComObject WScript.Shell
    $lnk = $ws.CreateShortcut($Shortcut)
    $lnk.TargetPath = $target
    $lnk.WorkingDirectory = $env:USERPROFILE
    $lnk.Description = "Review and safely merge AWS CodeCommit pull requests"
    $lnk.Save()
}
finally {
    Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "PRISM installed. Open it from the Start menu (search for PRISM)."
if ($newPath) { Write-Host "The 'prism' command works in NEW terminal windows (this one has the old PATH)." }

$missing = @()
foreach ($tool in "git", "aws", "opencode") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { $missing += $tool }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host ("PRISM also needs these on your PATH, and they were not found: " + ($missing -join ", "))
    if ($missing -contains "git")      { Write-Host "  git       https://git-scm.com/downloads" }
    if ($missing -contains "aws")      { Write-Host "  aws       https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html  (then: aws configure)" }
    if ($missing -contains "opencode") { Write-Host "  opencode  https://opencode.ai  (the reviewer engine)" }
}
Write-Host ""
Write-Host "Next:  cd <a clone of your CodeCommit repo>; prism run <PR id>     (prism --help for everything)"
