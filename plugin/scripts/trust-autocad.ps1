<#
.SYNOPSIS
  Add the ApplicationPlugins folder to AutoCAD's TRUSTEDPATHS so the unsigned
  AcadMcp bundle loads without the "Security - Unsigned Executable" prompt.

.DESCRIPTION
  Why this exists: with SECURELOAD=1 and an empty TRUSTEDPATHS (the state this
  machine was in), AutoCAD prompts for every unsigned DLL. A .NET assembly
  cannot be unloaded from AutoCAD, so each rebuild produces a new file and the
  prompt comes back every single time. The prompt also blocks the load, which
  meant the unattended self-test never saw its log file and timed out with no
  explanation.

  The setting lives in the profile, so it must be written while AutoCAD is
  closed: AutoCAD rewrites its profile back to the registry when it exits and
  would overwrite anything written mid-session.

  Run without parameters to fix every AutoCAD profile on the machine. Exits 0
  when every profile ends up with the folder trusted, 1 when one could not be
  written.
#>
[CmdletBinding()]
param(
    [string]$Path = (Join-Path $env:APPDATA 'Autodesk\ApplicationPlugins'),
    [string]$Log,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

function Write-Line([string]$Text) {
    if ($Log) { Add-Content -Path $Log -Value $Text -Encoding utf8 }
    if (-not $Quiet) { Write-Host $Text }
}

# The folder has to exist for AutoCAD to trust it, and normalising it now keeps
# the comparison below honest (trailing separator, case).
try {
    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
} catch {
    Write-Line "trust: cannot resolve path '$Path': $($_.Exception.Message)"
    exit 1
}
if (-not (Test-Path -LiteralPath $full)) {
    Write-Line "trust: $full does not exist; nothing to trust"
    exit 1
}

# AutoCAD keeps one registry key per release (R25.1 = 2026) holding several
# profiles, and the folder name of the active profile is localised, so every
# profile is discovered by walking the tree rather than by naming one.
$root = 'HKCU:\Software\Autodesk\AutoCAD'
if (-not (Test-Path $root)) {
    Write-Line "trust: no AutoCAD registry settings under $root"
    exit 1
}

$profiles = @()
foreach ($release in Get-ChildItem $root -ErrorAction SilentlyContinue) {
    foreach ($product in Get-ChildItem $release.PSPath -ErrorAction SilentlyContinue) {
        $vars = Join-Path $product.PSPath 'Profiles\*\Variables'
        foreach ($key in Get-ChildItem $vars -ErrorAction SilentlyContinue) {
            # $key is the Variables subkey, so the profile name is its parent.
            # AutoCAD wraps the default profile's name in angle brackets.
            $profileName = (Get-Item (Split-Path $key.PSPath -Parent)).PSChildName
            if ($profileName -match '^<<(.*)>>$') { $profileName = $Matches[1] }
            $profiles += [pscustomobject]@{
                Release = $release.PSChildName
                Name    = $profileName
                Path    = $key.PSPath
            }
        }
    }
}

if ($profiles.Count -eq 0) {
    Write-Line 'trust: AutoCAD has never run for this user, so no profile exists yet;'
    Write-Line '       start AutoCAD once, close it, and run this again.'
    exit 1
}

$failed = $false
foreach ($profile in $profiles) {
    $props = Get-ItemProperty -Path $profile.Path
    $current = [string]$props.TRUSTEDPATHS
    $secure = [string]$props.SECURELOAD

    $entries = @($current -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $already = $entries | Where-Object { $_.TrimEnd('\') -ieq $full }

    if ($already) {
        Write-Line "trust: $($profile.Release)/$($profile.Name) already trusts $full (SECURELOAD=$secure)"
        continue
    }

    # AutoCAD's own path values are written with a trailing separator, and the
    # sysvar accepts it, so the existing style is kept.
    $updated = (@($entries) + $full) -join ';'
    if ($updated -notmatch ';$') { $updated += ';' }

    try {
        Set-ItemProperty -Path $profile.Path -Name TRUSTEDPATHS -Value $updated -Type String
        $check = [string](Get-ItemProperty -Path $profile.Path).TRUSTEDPATHS
        if ($check -notmatch [regex]::Escape($full)) {
            Write-Line "trust: $($profile.Release)/$($profile.Name) write did not stick"
            $failed = $true
            continue
        }
        Write-Line "trust: $($profile.Release)/$($profile.Name) now trusts $full (SECURELOAD=$secure)"
    } catch {
        Write-Line "trust: $($profile.Release)/$($profile.Name) failed: $($_.Exception.Message)"
        $failed = $true
    }
}

if ($failed) { exit 1 }
exit 0
