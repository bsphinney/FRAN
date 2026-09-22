<#
.SYNOPSIS
    Stage the swap from the old raw .d copier to bruker_d_copy.ps1, with a
    test run against real data in between. Reversible at every step.

.DESCRIPTION
    A production change on an instrument-adjacent machine, so it is built to
    stop rather than to finish.

      0  preflight      everything it needs exists, and the test destination
                        is provably not the live archive
      1  capture        export the old task's XML to a timestamped backup on
                        the share -- BEFORE anything is touched. Cannot
                        capture it? Abort.
      2  disable        schtasks /Change /DISABLE. NEVER /Delete: deletion is
                        not reversible in a hurry, disabling is.
      3  test run       the new copier against a few REAL .d, into a TEST
                        destination. Deliberately includes a clean run that
                        must be accepted and, if one exists, a run with a live
                        -wal that must be refused.
      4  verify         the test did what it claimed: source bytes unchanged,
                        no side files transported, verdicts as expected
      5  register       the new task, inheriting the old task's schedule,
                        account and working directory from the captured XML
      6  rollback       write a one-line rollback command beside the backup

    ANY failure stops the sequence. Steps already completed are left as they
    are and the rollback command is printed, because a half-swapped machine
    with the old task disabled and no new task is the one state worse than
    either end.

    IT DOES NOTHING WITHOUT -Execute. The default is a dry run that prints
    the plan, runs the preflight, and changes not one thing.

    IT NEVER deletes the old task, the old script, or the backup XML.
    IT NEVER writes to the live archive.

.EXAMPLE
    # Read the plan. Changes nothing. Do this first.
    .\swap_to_safe_copier.ps1 -Source D:\Data `
        -Dest \\128.120.208.2\protcore\Data\raw_data\tTOF_HT `
        -TestDest D:\swap_test

.EXAMPLE
    # Do it.
    .\swap_to_safe_copier.ps1 -Source D:\Data `
        -Dest \\128.120.208.2\protcore\Data\raw_data\tTOF_HT `
        -TestDest D:\swap_test -Execute

.NOTES
    PowerShell 5.1 compatible. Companion: rollback_safe_copier.ps1.
    Testable helpers are covered by test_swap_to_safe_copier.ps1.
#>

param(
    # The live source the old copier reads.
    [Parameter(Mandatory = $true)] [string] $Source,

    # The live archive. Read to prove the test destination is not inside it.
    # NOTHING IS EVER WRITTEN HERE by this script.
    [Parameter(Mandatory = $true)] [string] $Dest,

    # Where the staged test copies go. Must not be inside -Dest or -Source.
    [Parameter(Mandatory = $true)] [string] $TestDest,

    # The task to retire. Discovered from the task list if not given, and the
    # run aborts rather than guessing when discovery is not conclusive.
    [string] $OldTaskName = "",

    # Substrings that identify the old task by what it runs.
    [string[]] $OldTaskMatch = @("flinders_copy.ps1", "copy_all_data_network.bat", "robocopy"),

    [string] $NewTaskName = "FRAN Bruker .d copy",

    # Timestamped task backups. Defaults to the share, so the backup survives
    # the machine it was taken on.
    [string] $BackupDir = "R:\Data\FRAN_SNE_export\bruker_copy\swap_backups",

    # How many real .d the staged test uses.
    [int] $SampleCount = 4,

    # Settle interval for the TEST ONLY. Shorter than production so a staged
    # swap takes minutes rather than half an hour; the mechanism exercised is
    # identical -- two probes, this far apart. The registered task uses the
    # full -SettleSeconds.
    [int] $TestSettleSeconds = 120,

    # What the registered task will use.
    [int] $SettleSeconds = 600,
    [int] $InterPacketGapMs = 0,

    # Without this, nothing is changed.
    [switch] $Execute
)

$ErrorActionPreference = "Continue"
$ScriptVersion = "1.0.0"

$Here = Split-Path -Parent $PSCommandPath
$Copier = Join-Path $Here "bruker_d_copy.ps1"
$Stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
$Step = 0

function Say($Message)  { Write-Host $Message }
function Head($Message) {
    Write-Host ""
    Write-Host "=== $Message" -ForegroundColor Cyan
}
function Good($Message) { Write-Host "  ok    $Message" -ForegroundColor Green }
function Warn($Message) { Write-Host "  warn  $Message" -ForegroundColor Yellow }

function Stop-Swap($Message) {
    Write-Host ""
    # $($script:Step) and not $script:Step -- in an expandable string the
    # parser takes the trailing colon as part of the variable name, so
    # "$script:Step:" is the unknown variable "Step:" and prints as nothing.
    Write-Host "ABORTED at step $($script:Step): $Message" -ForegroundColor Red
    if (-not $Execute) {
        Write-Host "  (dry run - nothing was changed)" -ForegroundColor Yellow
        exit 1
    }
    # Only when the old task was ACTUALLY disabled. Keying this off the step
    # number told a dry run it had left the machine with nothing copying,
    # which was both false and alarming.
    if ($script:Disabled) {
        Write-Host ""
        Write-Host "The old task is DISABLED and the new one is not registered." -ForegroundColor Red
        Write-Host "Nothing is copying. Roll back with:" -ForegroundColor Red
        Write-Host "  powershell -File `"$(Join-Path $Here 'rollback_safe_copier.ps1')`" ``"
        Write-Host "      -BackupXml `"$script:BackupPath`" -OldTaskName `"$script:OldTaskName`" ``"
        Write-Host "      -NewTaskName `"$NewTaskName`" -Execute"
    }
    exit 1
}

function Would($Message) {
    # In a dry run this is the whole output; under -Execute it is the log.
    if ($Execute) { Write-Host "  ->    $Message" }
    else { Write-Host "  WOULD $Message" -ForegroundColor DarkGray }
}

# ------------------------------------------------- testable helpers ------

function Test-PathInside($Child, $Parent) {
    # Is $Child at or beneath $Parent? Compared on normalised, lowercased
    # text: the real paths may not both exist yet, so GetFullPath is as far
    # as this can go. A trailing separator is added to both so that
    # "D:\archive_test" is NOT judged to be inside "D:\archive".
    if (-not $Child -or -not $Parent) { return $false }
    try {
        $c = [System.IO.Path]::GetFullPath($Child).Replace("/", "\").TrimEnd("\").ToLowerInvariant()
        $p = [System.IO.Path]::GetFullPath($Parent).Replace("/", "\").TrimEnd("\").ToLowerInvariant()
    } catch {
        $c = ("$Child").Replace("/", "\").TrimEnd("\").ToLowerInvariant()
        $p = ("$Parent").Replace("/", "\").TrimEnd("\").ToLowerInvariant()
    }
    if ($c -eq $p) { return $true }
    return $c.StartsWith("$p\")
}

function Test-TestDestSafe($TestPath, $LivePath, $SourcePath) {
    # "" when the test destination is safe, otherwise why it is not.
    #
    # The one mistake that would turn a rehearsal into the incident: pointing
    # the test at the live archive. Checked in both directions -- a test
    # destination that CONTAINS the archive is just as bad.
    if (Test-PathInside $TestPath $LivePath) { return "the test destination is inside the live archive" }
    if (Test-PathInside $LivePath $TestPath) { return "the live archive is inside the test destination" }
    if (Test-PathInside $TestPath $SourcePath) { return "the test destination is inside the source" }
    if (Test-PathInside $SourcePath $TestPath) { return "the source is inside the test destination" }
    return ""
}

function Get-DHazard($DPath) {
    # "clean" / "hazard" / "nodb". Only picks the test sample -- the real
    # verdict is the copier's, and that is what the test reads back.
    $hasDb = $false
    $hazard = $false
    foreach ($f in @(Get-ChildItem -LiteralPath $DPath -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n -eq "analysis.tdf" -or $n -eq "analysis.baf") { $hasDb = $true }
        if ($n.EndsWith("-shm") -or $n.EndsWith("-journal")) { $hazard = $true }
        if ($n.EndsWith("-wal") -and $f.Length -gt 0) { $hazard = $true }
    }
    if (-not $hasDb) { return "nodb" }
    if ($hazard) { return "hazard" }
    return "clean"
}

function Select-TestSample($Candidates, $Count) {
    # A sample that actually tests something: at least one run that must be
    # ACCEPTED and, if the source holds one, at least one that must be
    # REFUSED. A sample of four clean runs proves only that the copier can
    # copy.
    $clean = @()
    $hazard = @()
    foreach ($c in $Candidates) {
        if ($c.State -eq "clean") { $clean += $c }
        elseif ($c.State -eq "hazard") { $hazard += $c }
    }
    $picked = @()
    if ($hazard.Count -gt 0) { $picked += $hazard[0] }
    if ($clean.Count -gt 0) { $picked += $clean[0] }
    foreach ($c in $clean) {
        if ($picked.Count -ge $Count) { break }
        $already = $false
        foreach ($p in $picked) { if ($p.Rel -eq $c.Rel) { $already = $true } }
        if (-not $already) { $picked += $c }
    }
    foreach ($c in $hazard) {
        if ($picked.Count -ge $Count) { break }
        $already = $false
        foreach ($p in $picked) { if ($p.Rel -eq $c.Rel) { $already = $true } }
        if (-not $already) { $picked += $c }
    }
    return $picked
}

function Find-TaskByCommand($CsvText, $Needles) {
    # Task names whose "Task To Run" mentions one of $Needles.
    #
    # schtasks /query /FO CSV /V repeats its header row once per folder, so
    # the header rows are dropped by value rather than by position.
    $names = @()
    if (-not $CsvText) { return $names }
    $rows = @()
    try { $rows = @($CsvText | ConvertFrom-Csv) } catch { return $names }
    foreach ($row in $rows) {
        $name = "$($row.TaskName)"
        $run = "$($row.'Task To Run')"
        if (-not $name -or $name -eq "TaskName") { continue }
        foreach ($needle in $Needles) {
            if ($run -and $run.ToLowerInvariant().Contains(("$needle").ToLowerInvariant())) {
                $already = $false
                foreach ($n in $names) { if ($n -eq $name) { $already = $true } }
                if (-not $already) { $names += $name }
                break
            }
        }
    }
    return $names
}

function New-TaskXmlForCommand($OldXml, $Command, $Arguments) {
    # The new task IS the old task with only its <Exec> action replaced.
    #
    # Rebuilding a task from scratch means re-deriving its triggers, its
    # principal, its logon type and its working directory, and getting any
    # one of them wrong changes when or as whom the copy runs. Taking the
    # captured XML and swapping the two elements that must change inherits
    # all of it by construction. Returns "" if the XML has no Exec action.
    $doc = New-Object System.Xml.XmlDocument
    $doc.PreserveWhitespace = $true
    try { $doc.LoadXml($OldXml) } catch { return "" }
    $ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $exec = $doc.SelectSingleNode("//t:Actions/t:Exec", $ns)
    if (-not $exec) { return "" }

    $cmdNode = $exec.SelectSingleNode("t:Command", $ns)
    if (-not $cmdNode) {
        $cmdNode = $doc.CreateElement("Command", $ns.LookupNamespace("t"))
        $exec.AppendChild($cmdNode) | Out-Null
    }
    $cmdNode.InnerText = $Command

    $argNode = $exec.SelectSingleNode("t:Arguments", $ns)
    if (-not $argNode) {
        $argNode = $doc.CreateElement("Arguments", $ns.LookupNamespace("t"))
        $exec.AppendChild($argNode) | Out-Null
    }
    $argNode.InnerText = $Arguments

    # Say in the task itself what it is and where it came from, so whoever
    # finds it in six months does not have to guess.
    $desc = $doc.SelectSingleNode("//t:RegistrationInfo/t:Description", $ns)
    if ($desc) {
        $desc.InnerText = "FRAN safe Bruker .d copier. Replaced the previous copier on $((Get-Date).ToString('yyyy-MM-dd')); its definition is backed up as XML. See bruker_copy/README.md."
    }
    return $doc.OuterXml
}

function Get-TaskDeviation($OldXml) {
    # Things inherited from the old task that are worth saying out loud,
    # because inheriting them is a decision even when it is the right one.
    $notes = @()
    $doc = New-Object System.Xml.XmlDocument
    try { $doc.LoadXml($OldXml) } catch { return $notes }
    $ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")

    $multi = $doc.SelectSingleNode("//t:Settings/t:MultipleInstancesPolicy", $ns)
    if ($multi -and $multi.InnerText -ne "IgnoreNew") {
        $notes += "MultipleInstancesPolicy is '$($multi.InnerText)', not 'IgnoreNew'. Inherited. The copier holds its own lock file, so overlapping passes stand down anyway."
    }
    $limit = $doc.SelectSingleNode("//t:Settings/t:ExecutionTimeLimit", $ns)
    if ($limit -and $limit.InnerText -eq "PT0S") {
        $notes += "ExecutionTimeLimit is unlimited. Inherited. A pass that wedges will not be killed by the scheduler; the copier's own lock goes stale after 6 hours."
    }
    $user = $doc.SelectSingleNode("//t:Principal/t:UserId", $ns)
    if ($user) {
        $u = $user.InnerText.ToLowerInvariant()
        if ($u.EndsWith("system") -or $u -eq "s-1-5-18") {
            $notes += "The old task runs as SYSTEM. Inherited, BUT SYSTEM cannot see mapped network drives. If the destination is a drive letter rather than a UNC path, the copy will fail. CHECK THIS."
        }
    }
    $logon = $doc.SelectSingleNode("//t:Principal/t:LogonType", $ns)
    if ($logon -and $logon.InnerText -eq "InteractiveToken") {
        $notes += "LogonType is InteractiveToken: the task only runs while that account is logged on. Inherited -- it is also what lets it reach a mapped share."
    }
    return $notes
}

# ---------------------------------------------------------- the steps ----

$BackupPath = ""
$Disabled = $false      # set only by a disable that was verified to have taken
$OnWindows = ($env:OS -eq "Windows_NT")

Write-Host ""
Write-Host "  Staged swap to the safe Bruker .d copier   (v$ScriptVersion)"
if (-not $Execute) {
    Write-Host "  DRY RUN -- nothing will be changed. Add -Execute to act." -ForegroundColor Yellow
} else {
    Write-Host "  EXECUTING." -ForegroundColor Yellow
}

# ---- 0. preflight -------------------------------------------------------
$Step = 0
Head "0. Preflight"

if (-not $OnWindows) {
    Say "  This machine is not Windows. Preflight only; schtasks is unavailable."
}
if (-not (Test-Path -LiteralPath $Copier)) { Stop-Swap "bruker_d_copy.ps1 is not beside this script ($Copier)" }
$perr = $null
$ptok = $null
[System.Management.Automation.Language.Parser]::ParseFile($Copier, [ref] $ptok, [ref] $perr) | Out-Null
if ($perr -and $perr.Count -gt 0) { Stop-Swap "bruker_d_copy.ps1 does not parse" }
Good "the copier is present and parses"

if (-not (Test-Path -LiteralPath $Source -PathType Container)) { Stop-Swap "source $Source is not reachable" }
Good "source reachable: $Source"
if (-not (Test-Path -LiteralPath $Dest -PathType Container)) { Stop-Swap "live archive $Dest is not reachable" }
Good "live archive reachable: $Dest  (nothing is written here)"

$unsafe = Test-TestDestSafe $TestDest $Dest $Source
if ($unsafe) { Stop-Swap "refusing to run: $unsafe" }
Good "test destination is not the live archive: $TestDest"

if ($OnWindows -and -not (Get-Command "robocopy.exe" -ErrorAction SilentlyContinue)) {
    Stop-Swap "robocopy.exe not found"
}
if (Get-Command "sqlite3" -ErrorAction SilentlyContinue) { Good "sqlite3 found - the index-coverage check will run" }
else { Warn "sqlite3 not on PATH - the index-coverage check will be recorded as unavailable. The other four gate checks still apply." }

if (-not (Test-Path -LiteralPath $BackupDir)) {
    Would "create the backup directory $BackupDir"
    if ($Execute) {
        New-Item -ItemType Directory -Path $BackupDir -Force -ErrorAction SilentlyContinue | Out-Null
        if (-not (Test-Path -LiteralPath $BackupDir)) { Stop-Swap "cannot create the backup directory $BackupDir" }
    }
} else {
    Good "backup directory: $BackupDir"
}

# ---- 1. capture ---------------------------------------------------------
$Step = 1
Head "1. Capture the existing task"

if (-not $OnWindows) {
    Warn "not Windows - task discovery, capture, disable and register are all skipped"
} else {
    if (-not $OldTaskName) {
        $csv = & schtasks /query /FO CSV /V 2>$null
        $found = @(Find-TaskByCommand ([string]::Join("`n", @($csv))) $OldTaskMatch)
        if ($found.Count -eq 0) {
            Say ""
            Say "  No scheduled task runs any of: $([string]::Join(', ', $OldTaskMatch))"
            Say "  Name it explicitly with -OldTaskName, or check whether the copier is"
            Say "  triggered some other way (a shortcut, a login script, by hand)."
            Stop-Swap "the old task could not be identified, and this will not guess"
        }
        if ($found.Count -gt 1) {
            Say ""
            Say "  More than one task matches:"
            foreach ($f in $found) { Say "    $f" }
            Stop-Swap "ambiguous - name the one to retire with -OldTaskName"
        }
        $OldTaskName = $found[0]
        Good "identified by what it runs: $OldTaskName"
    } else {
        Good "named on the command line: $OldTaskName"
    }

    $BackupPath = Join-Path $BackupDir "$($Stamp)_$(($OldTaskName -replace '[\\/:*?""<>|]', '_')).xml"
    Would "export $OldTaskName to $BackupPath"
    if ($Execute) {
        $xml = & schtasks /query /TN "$OldTaskName" /XML 2>$null
        $xmlText = [string]::Join("`r`n", @($xml))
        if (-not $xmlText -or $xmlText.Trim().Length -lt 50) { Stop-Swap "the task XML came back empty - refusing to touch a task that cannot be restored" }
        $probe = New-Object System.Xml.XmlDocument
        try { $probe.LoadXml($xmlText) } catch { Stop-Swap "the exported task XML does not parse - refusing to proceed without a restorable backup" }
        Set-Content -LiteralPath $BackupPath -Value $xmlText -Encoding UTF8
        if (-not (Test-Path -LiteralPath $BackupPath)) { Stop-Swap "the backup did not get written to $BackupPath" }
        Good "captured and verified: $BackupPath"
        foreach ($note in @(Get-TaskDeviation $xmlText)) { Warn $note }
    }
}

# ---- 2. disable ---------------------------------------------------------
$Step = 2
Head "2. Disable the old task (never delete it)"
$oldLabel = $OldTaskName
if (-not $oldLabel) { $oldLabel = "<discovered in step 1>" }
Would "schtasks /Change /TN `"$oldLabel`" /DISABLE"
if ($Execute -and $OnWindows) {
    & schtasks /Change /TN "$OldTaskName" /DISABLE 2>&1 | Out-Null
    $after = & schtasks /query /TN "$OldTaskName" /FO LIST 2>$null
    $stillOn = $false
    foreach ($line in @($after)) {
        if ($line -match "^\s*Scheduled Task State:\s*Enabled" -or $line -match "^\s*Status:\s*Ready") { $stillOn = $true }
    }
    if ($stillOn) { Stop-Swap "the task still reports as enabled after /DISABLE" }
    $Disabled = $true
    Good "disabled (its definition is untouched and backed up)"
}

# ---- 3. test run --------------------------------------------------------
$Step = 3
Head "3. Test run against real .d, into the TEST destination"

$cands = @()
foreach ($d in @(Get-ChildItem -LiteralPath $Source -Directory -Recurse -Depth 3 -Force -ErrorAction SilentlyContinue)) {
    if ($d.Extension -ne ".d") { continue }
    $rel = $d.FullName.Substring($Source.TrimEnd("\", "/").Length).TrimStart("\", "/")
    $cands += [PSCustomObject]@{ Rel = $rel; Full = $d.FullName; State = (Get-DHazard $d.FullName) }
}
Say "  $($cands.Count) .d found under the source"

$sample = @(Select-TestSample $cands $SampleCount)
if ($sample.Count -eq 0) { Stop-Swap "no .d found under $Source to test against" }

$haveHazard = $false
$haveClean = $false
foreach ($s in $sample) {
    Say "    $($s.State.PadRight(7)) $($s.Rel)"
    if ($s.State -eq "hazard") { $haveHazard = $true }
    if ($s.State -eq "clean") { $haveClean = $true }
}
if (-not $haveClean) { Stop-Swap "the sample has no clean .d, so there is nothing that SHOULD be accepted - the test would prove nothing" }
if (-not $haveHazard) {
    Warn "NO .d WITH A LIVE -wal EXISTS UNDER THIS SOURCE RIGHT NOW."
    Warn "The refusal case is therefore NOT covered by this run. It is covered by"
    Warn "test_bruker_d_copy.ps1 against synthetic fixtures, but not against this"
    Warn "machine's real data. Re-run this during an acquisition to cover it."
}

# Checksum before. A copier that altered what it inspected would be absurd,
# and this is how that claim gets checked rather than asserted.
$before = @{}
foreach ($s in $sample) {
    $tdf = Join-Path $s.Full "analysis.tdf"
    if (Test-Path -LiteralPath $tdf) {
        $before[$s.Rel] = (Get-FileHash -LiteralPath $tdf -Algorithm SHA256).Hash
    }
}
Good "checksummed $($before.Count) source analysis.tdf before the run"

$only = @()
foreach ($s in $sample) { $only += $s.Rel }
$testState = Join-Path $TestDest "_state"
$pass = @("-Source", $Source, "-Dest", $TestDest, "-StateDir", $testState,
          "-SettleSeconds", "$TestSettleSeconds", "-All", "-Show", "-Only") + $only

Would "create $TestDest"
Would "pass 1 of 2: bruker_d_copy.ps1 $([string]::Join(' ', $pass))"
Would "wait $TestSettleSeconds s (the settle interval), then pass 2"
Say   "        the registered task will use the full -SettleSeconds $SettleSeconds;"
Say   "        $TestSettleSeconds s here only so a staged swap takes minutes, not half an hour."

if ($Execute) {
    New-Item -ItemType Directory -Path $TestDest -Force -ErrorAction SilentlyContinue | Out-Null
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Copier @pass | Out-Null
    Say "  settling for $TestSettleSeconds s ..."
    Start-Sleep -Seconds $TestSettleSeconds
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Copier @pass | Out-Null
    Good "two passes complete"
}

# ---- 4. verify ----------------------------------------------------------
$Step = 4
Head "4. Verify the test run"

if (-not $Execute) {
    Would "confirm every source analysis.tdf checksum is unchanged"
    Would "confirm no -wal/-shm/-journal reached $TestDest"
    Would "confirm each clean .d was 'copied' and each hazard .d 'skipped-hazard'"
} else {
    $changed = @()
    foreach ($rel in $before.Keys) {
        $tdf = Join-Path (Join-Path $Source $rel) "analysis.tdf"
        $now = ""
        if (Test-Path -LiteralPath $tdf) { $now = (Get-FileHash -LiteralPath $tdf -Algorithm SHA256).Hash }
        if ($now -ne $before[$rel]) { $changed += $rel }
    }
    if ($changed.Count -gt 0) { Stop-Swap "THE SOURCE CHANGED: $([string]::Join(', ', $changed)). Stop and investigate - nothing here should write to the source." }
    Good "every source analysis.tdf is byte-identical after the run"

    $side = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $TestDest -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n.EndsWith("-wal") -or $n.EndsWith("-shm") -or $n.EndsWith("-journal")) { $side += $f.FullName }
    }
    if ($side.Count -gt 0) { Stop-Swap "a SQLite side file reached the test destination: $($side[0])" }
    Good "no -wal, -shm or -journal reached the test destination"

    $outcomes = @{}
    $jsonl = Join-Path $testState "outcomes.jsonl"
    if (-not (Test-Path -LiteralPath $jsonl)) { Stop-Swap "the copier wrote no outcomes at $jsonl" }
    foreach ($line in @(Get-Content -LiteralPath $jsonl)) {
        if (-not $line.Trim()) { continue }
        try { $o = $line | ConvertFrom-Json } catch { continue }
        $outcomes[("$($o.run)").Replace("\", "/")] = $o
    }

    $bad = @()
    foreach ($s in $sample) {
        $key = $s.Rel.Replace("\", "/")
        $o = $outcomes[$key]
        if (-not $o) { $bad += "$($s.Rel): no outcome recorded"; continue }
        Say "    $($o.verdict.PadRight(24)) $($s.Rel)  $($o.reason)"
        if ($s.State -eq "clean" -and $o.verdict -ne "copied") {
            $bad += "$($s.Rel) is clean but came back '$($o.verdict)' ($($o.reason))"
        }
        if ($s.State -eq "hazard" -and $o.verdict -ne "skipped-hazard") {
            $bad += "$($s.Rel) has a live side file but came back '$($o.verdict)' - THE GATE DID NOT REFUSE IT"
        }
    }
    if ($bad.Count -gt 0) {
        foreach ($b in $bad) { Write-Host "  BAD   $b" -ForegroundColor Red }
        Stop-Swap "the test run did not behave as expected - do not register the new task"
    }
    Good "every verdict is what it should be"
}

# ---- 5. register --------------------------------------------------------
$Step = 5
Head "5. Register the new task"

$newArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Copier`" `"$Source`" `"$Dest`" -SettleSeconds $SettleSeconds -InterPacketGapMs $InterPacketGapMs"
Would "register `"$NewTaskName`" running: powershell.exe $newArgs"
Say   "        inheriting the old task's schedule, account, logon type and"
Say   "        working directory by rewriting only its <Exec> action."

if ($Execute -and $OnWindows) {
    $oldXml = Get-Content -LiteralPath $BackupPath -Raw
    $newXml = New-TaskXmlForCommand $oldXml "powershell.exe" $newArgs
    if (-not $newXml) { Stop-Swap "the captured task has no <Exec> action to rewrite - register the new task by hand" }
    $newXmlPath = Join-Path $BackupDir "$($Stamp)_NEW_$(($NewTaskName -replace '[\\/:*?""<>|]', '_')).xml"
    Set-Content -LiteralPath $newXmlPath -Value $newXml -Encoding UTF8
    & schtasks /Create /TN "$NewTaskName" /XML "$newXmlPath" /F 2>&1 | Out-Null
    $check = & schtasks /query /TN "$NewTaskName" /FO LIST 2>$null
    if (-not $check) { Stop-Swap "the new task is not there after registering it" }
    Good "registered: $NewTaskName"
    Good "its definition: $newXmlPath"
}

# ---- 6. rollback --------------------------------------------------------
$Step = 6
Head "6. Rollback"

$rollbackCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Here 'rollback_safe_copier.ps1')`" -BackupXml `"$BackupPath`" -OldTaskName `"$OldTaskName`" -NewTaskName `"$NewTaskName`" -Execute"
Would "write the rollback command beside the backup"
if ($Execute -and $OnWindows) {
    $rbPath = Join-Path $BackupDir "$($Stamp)_ROLLBACK.cmd"
    Set-Content -LiteralPath $rbPath -Value @("@echo off", "REM Undo the swap staged at $Stamp.", $rollbackCmd)
    Good "rollback: $rbPath"
}

Write-Host ""
if ($Execute) {
    Write-Host "  Swap complete." -ForegroundColor Green
    Write-Host "  The old task is DISABLED, not deleted, and backed up at:"
    Write-Host "    $BackupPath"
    Write-Host "  To undo everything:"
} else {
    Write-Host "  Dry run complete. Nothing was changed." -ForegroundColor Yellow
    Write-Host "  Re-run with -Execute to do it. The rollback would be:"
}
Write-Host "    $rollbackCmd"
Write-Host ""
exit 0
