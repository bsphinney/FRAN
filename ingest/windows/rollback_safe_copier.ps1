<#
.SYNOPSIS
    Undo swap_to_safe_copier.ps1: bring the old task back and remove the new
    one.

.DESCRIPTION
    The old task was DISABLED, not deleted, so the normal path is simply to
    re-enable it. If it is not there at all -- somebody removed it by hand --
    it is recreated from the XML captured before the swap.

    Order matters and is deliberate: the old copier comes back FIRST, and
    only then is the new one removed. The reverse order leaves a window with
    nothing copying, which is the state this script exists to get out of.

    IT DOES NOTHING WITHOUT -Execute.
    IT NEVER deletes the backup XML, and it never deletes the new copier's
    script or its logs -- only its scheduled task.

    A caveat worth knowing before you run it: the old copier is the one that
    transports analysis.tdf-wal. Re-enabling it restores the hazard. Roll
    back to stop a bad swap, then disable the old task again by hand once
    whatever went wrong is understood:

        schtasks /Change /TN "<old task>" /DISABLE

.EXAMPLE
    .\rollback_safe_copier.ps1 -BackupXml R:\...\20260921_140000_Task.xml `
        -OldTaskName "STAN Flinders Copy" -NewTaskName "FRAN Bruker .d copy"

.EXAMPLE
    .\rollback_safe_copier.ps1 -BackupXml ... -OldTaskName ... -Execute

.NOTES
    PowerShell 5.1 compatible. Written by swap_to_safe_copier.ps1 as a ready
    -to-run .cmd beside the backup XML.
#>

param(
    # The XML captured in step 1 of the swap. Required even when the task is
    # only disabled: it is the proof the definition can be restored.
    [Parameter(Mandatory = $true)] [string] $BackupXml,

    [Parameter(Mandatory = $true)] [string] $OldTaskName,

    [string] $NewTaskName = "FRAN Bruker .d copy",

    # Without this, nothing is changed.
    [switch] $Execute
)

$ErrorActionPreference = "Continue"
$OnWindows = ($env:OS -eq "Windows_NT")

function Head($m) { Write-Host ""; Write-Host "=== $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "  ok    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  warn  $m" -ForegroundColor Yellow }
function Would($m) {
    if ($Execute) { Write-Host "  ->    $m" }
    else { Write-Host "  WOULD $m" -ForegroundColor DarkGray }
}
function Stop-Rollback($m) {
    Write-Host ""
    Write-Host "ROLLBACK FAILED: $m" -ForegroundColor Red
    Write-Host "Restore the old task by hand with:" -ForegroundColor Red
    Write-Host "  schtasks /Create /TN `"$OldTaskName`" /XML `"$BackupXml`" /F"
    Write-Host "  schtasks /Change /TN `"$OldTaskName`" /ENABLE"
    exit 1
}

function Test-TaskExists($Name) {
    if (-not $OnWindows) { return $false }
    $out = & schtasks /query /TN "$Name" /FO LIST 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) { return $true }
    return $false
}

Write-Host ""
Write-Host "  Rollback: restore the previous .d copier"
if (-not $Execute) { Write-Host "  DRY RUN -- nothing will be changed. Add -Execute to act." -ForegroundColor Yellow }

Head "0. Check the backup"
if (-not (Test-Path -LiteralPath $BackupXml)) { Stop-Rollback "the backup XML is not at $BackupXml" }
$xmlText = Get-Content -LiteralPath $BackupXml -Raw
$probe = New-Object System.Xml.XmlDocument
try { $probe.LoadXml($xmlText) } catch { Stop-Rollback "the backup XML does not parse - do not rely on it" }
Good "backup is present and parses: $BackupXml"

# The old copier first: a window with nothing copying is worse than either
# end state.
Head "1. Bring the old task back"
if (-not $OnWindows) {
    Warn "not Windows - schtasks is unavailable, so this is a plan only"
    Would "re-enable (or recreate from the backup) `"$OldTaskName`""
} elseif (Test-TaskExists $OldTaskName) {
    Good "the old task is still registered - only disabled"
    Would "schtasks /Change /TN `"$OldTaskName`" /ENABLE"
    if ($Execute) {
        & schtasks /Change /TN "$OldTaskName" /ENABLE 2>&1 | Out-Null
        if (-not (Test-TaskExists $OldTaskName)) { Stop-Rollback "the old task vanished while being re-enabled" }
        Good "re-enabled: $OldTaskName"
    }
} else {
    Warn "the old task is GONE, not merely disabled - recreating it from the backup"
    Would "schtasks /Create /TN `"$OldTaskName`" /XML `"$BackupXml`" /F"
    if ($Execute) {
        & schtasks /Create /TN "$OldTaskName" /XML "$BackupXml" /F 2>&1 | Out-Null
        if (-not (Test-TaskExists $OldTaskName)) { Stop-Rollback "could not recreate the old task from the backup" }
        & schtasks /Change /TN "$OldTaskName" /ENABLE 2>&1 | Out-Null
        Good "recreated and enabled: $OldTaskName"
    }
}

Head "2. Remove the new task"
if (-not $OnWindows) {
    Would "disable then delete `"$NewTaskName`""
} elseif (Test-TaskExists $NewTaskName) {
    # Disable before delete: if the delete fails, the new copier is at least
    # not running alongside the one we just restored.
    Would "schtasks /Change /TN `"$NewTaskName`" /DISABLE, then /Delete /F"
    if ($Execute) {
        & schtasks /Change /TN "$NewTaskName" /DISABLE 2>&1 | Out-Null
        & schtasks /Delete /TN "$NewTaskName" /F 2>&1 | Out-Null
        if (Test-TaskExists $NewTaskName) { Warn "the new task is STILL registered - remove it by hand: schtasks /Delete /TN `"$NewTaskName`" /F" }
        else { Good "removed: $NewTaskName" }
    }
} else {
    Good "the new task is not registered - nothing to remove"
}

Write-Host ""
if ($Execute) {
    Write-Host "  Rolled back. The previous copier is running again." -ForegroundColor Green
    Write-Host ""
    Write-Host "  NOTE: that copier is the one that transports analysis.tdf-wal." -ForegroundColor Yellow
    Write-Host "  Every .d it copies mid-acquisition creates a new hazard. Once you" -ForegroundColor Yellow
    Write-Host "  know why the swap failed, disable it again:" -ForegroundColor Yellow
    Write-Host "    schtasks /Change /TN `"$OldTaskName`" /DISABLE"
} else {
    Write-Host "  Dry run complete. Nothing was changed. Add -Execute to roll back." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  The backup XML is kept, not consumed: $BackupXml"
Write-Host ""
exit 0
