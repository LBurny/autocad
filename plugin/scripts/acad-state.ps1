<#
.SYNOPSIS
  Report what the local AutoCAD processes look like from the outside.

.DESCRIPTION
  Needed because "the plugin did not load" has two very different causes that
  look identical from a log file that never appears: the bundle was not found,
  or AutoCAD is sitting behind a modal dialog (a security prompt, or a command
  waiting for input) that has not let the load happen yet.

  A modal dialog in AutoCAD is a child window, so it does not change the
  process's MainWindowTitle. What it does do is disable the main frame window
  while it is up, so IsWindowEnabled on that frame is the reliable signal. The
  visible top-level windows are listed as well, for the cases where the dialog
  is a separate top-level window.

  Output is plain `key=value` lines, one per line, so a shell can grep it.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Add-Type -Namespace AcadProbe -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool IsWindowEnabled(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr hWnd);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(System.IntPtr hWnd, System.Text.StringBuilder text, int count);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(System.IntPtr hWnd, System.Text.StringBuilder text, int count);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(System.IntPtr hWnd, out uint pid);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc callback, System.IntPtr param);
public delegate bool EnumProc(System.IntPtr hWnd, System.IntPtr param);
'@

function Get-Text([IntPtr]$h) {
    $sb = New-Object System.Text.StringBuilder 512
    [void][AcadProbe.Win]::GetWindowTextW($h, $sb, $sb.Capacity)
    $sb.ToString()
}
function Get-Class([IntPtr]$h) {
    $sb = New-Object System.Text.StringBuilder 256
    [void][AcadProbe.Win]::GetClassNameW($h, $sb, $sb.Capacity)
    $sb.ToString()
}

$procs = @(Get-Process acad -ErrorAction SilentlyContinue)
"running=$($procs.Count)"
if ($procs.Count -eq 0) { exit 0 }

# EnumWindows walks top-level windows only; every window belonging to any acad
# process is collected here so a dialog with its own frame is not missed.
$windows = New-Object System.Collections.ArrayList
$callback = [AcadProbe.Win+EnumProc] {
    param([IntPtr]$hWnd, [IntPtr]$param)
    # Declared as uint32 to match the P/Invoke signature: passing an Int32 by
    # ref for an `out uint` parameter throws a binding exception.
    [uint32]$owner = 0
    [void][AcadProbe.Win]::GetWindowThreadProcessId($hWnd, [ref]$owner)
    if ($procs.Id -contains [int]$owner) {
        [void]$windows.Add([pscustomobject]@{
            Handle  = $hWnd
            Pid     = [int]$owner
            Title   = (Get-Text $hWnd)
            Class   = (Get-Class $hWnd)
            Visible = [AcadProbe.Win]::IsWindowVisible($hWnd)
            Enabled = [AcadProbe.Win]::IsWindowEnabled($hWnd)
        })
    }
    return $true
}
[void][AcadProbe.Win]::EnumWindows($callback, [IntPtr]::Zero)

foreach ($p in $procs) {
    # The frame is whichever of the process's titled windows reports the same
    # title as the process itself; falling back to the first titled window keeps
    # this useful while AutoCAD is still showing its splash.
    $own = $windows | Where-Object { $_.Title -and $_.Pid -eq $p.Id }
    $main = $own | Where-Object { $_.Title -eq $p.MainWindowTitle } | Select-Object -First 1
    if (-not $main) { $main = $own | Select-Object -First 1 }
    "process=$($p.Id)"
    "title=$($p.MainWindowTitle)"
    if ($main) {
        "enabled=$($main.Enabled)"
        "class=$($main.Class)"
    } else {
        "enabled=unknown"
    }
}

# Only titled, visible windows are useful here; AutoCAD hides a swarm of
# untitled helper windows and listing them would bury the one that matters.
foreach ($w in $windows | Where-Object { $_.Title -and $_.Visible }) {
    "window=$($w.Title) | $($w.Class) | enabled=$($w.Enabled)"
}
