# test_swap_to_safe_copier.ps1
#
#     pwsh -NoProfile -File test_swap_to_safe_copier.ps1
#
# Tests the decision logic in swap_to_safe_copier.ps1 -- the parts that can
# be wrong in a way that matters, and that do not need Windows:
#
#   * is the test destination provably not the live archive
#   * which task is the old one, from what it runs
#   * does the new task XML inherit the old one's schedule and principal
#   * does the sample include something that must be accepted AND something
#     that must be refused
#
# schtasks itself cannot be exercised here. See README.md for what that
# leaves untested.
#
# Functions are pulled out of the shipped .ps1 through the AST, so the tests
# cannot drift, and the script's own top-level sequence never runs.

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Failures = 0
function Check($Label, $Got, $Want) {
    if ("$Got" -eq "$Want") { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', want '$Want'"; $script:Failures += 1 }
}
function CheckLike($Label, $Got, $Pattern) {
    if ("$Got" -like $Pattern) { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', wanted like '$Pattern'"; $script:Failures += 1 }
}

foreach ($name in @("swap_to_safe_copier.ps1", "rollback_safe_copier.ps1")) {
    $path = Join-Path $here $name
    if (-not (Test-Path -LiteralPath $path)) { Write-Host "cannot find $path"; exit 1 }
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref] $tokens, [ref] $errors)
    if ($errors -and $errors.Count -gt 0) {
        Write-Host "PARSE ERRORS in $name"
        foreach ($e in $errors) { Write-Host "  line $($e.Extent.StartLineNumber): $($e.Message)" }
        exit 1
    }
    if ($name -eq "swap_to_safe_copier.ps1") {
        foreach ($f in $ast.FindAll({
            $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst]
        }, $true)) { Invoke-Expression $f.Extent.Text }
    }
}
Write-Host ""
Write-Host "0. both scripts parse"
Write-Host "  ok   swap_to_safe_copier.ps1"
Write-Host "  ok   rollback_safe_copier.ps1"

# ==== 1. the test destination must not be the live archive ==============
Write-Host ""
Write-Host "1. the test destination is not the live archive"

Check "a path is inside itself"          (Test-PathInside "D:\arc" "D:\arc") $true
Check "a child is inside its parent"     (Test-PathInside "D:\arc\Aug26" "D:\arc") $true
Check "a sibling is not"                 (Test-PathInside "D:\arc2" "D:\arc") $false
# The prefix trap: "D:\archive_test" starts with "D:\archive" as TEXT, and a
# naive StartsWith would call it inside. It is not.
Check "a name-prefix sibling is not"     (Test-PathInside "D:\archive_test" "D:\archive") $false
Check "separators and case do not matter" (Test-PathInside "D:/ARC/x" "d:\arc") $true
Check "a trailing separator is ignored"  (Test-PathInside "D:\arc\x" "D:\arc\") $true

Check "a distinct test destination is allowed" (Test-TestDestSafe "D:\swap_test" "R:\arc" "D:\Data") ""
CheckLike "THE LIVE ARCHIVE ITSELF IS REFUSED" `
    (Test-TestDestSafe "R:\arc" "R:\arc" "D:\Data") "*inside the live archive*"
CheckLike "a subfolder of the archive is refused" `
    (Test-TestDestSafe "R:\arc\test" "R:\arc" "D:\Data") "*inside the live archive*"
CheckLike "a test destination CONTAINING the archive is refused" `
    (Test-TestDestSafe "R:\" "R:\arc" "D:\Data") "*live archive is inside*"
CheckLike "inside the source is refused" `
    (Test-TestDestSafe "D:\Data\test" "R:\arc" "D:\Data") "*inside the source*"
CheckLike "containing the source is refused" `
    (Test-TestDestSafe "D:\" "R:\arc" "D:\Data") "*source is inside*"

# ==== 2. identifying the old task =======================================
Write-Host ""
Write-Host "2. identifying the old task from what it runs"

# schtasks /query /FO CSV /V, with its header repeated per folder.
$csv = @'
"HostName","TaskName","Next Run Time","Status","Task To Run","Start In"
"PC","\STAN Flinders Copy","21/09/2026 14:05:00","Ready","powershell.exe -File C:\Users\x\STAN\flinders_copy.ps1","C:\Users\x\STAN"
"PC","\Windows\Defrag\ScheduledDefrag","N/A","Ready","%windir%\system32\defrag.exe -c","N/A"
"HostName","TaskName","Next Run Time","Status","Task To Run","Start In"
"PC","\Copy all data","21/09/2026 14:10:00","Ready","R:\Data\lab\Robocopy\copy_all_data_network.bat","N/A"
'@

$hits = @(Find-TaskByCommand $csv @("flinders_copy.ps1"))
Check "finds the task by its script name"  $hits.Count 1
Check "  and returns its full name"        $hits[0] "\STAN Flinders Copy"

$hits = @(Find-TaskByCommand $csv @("copy_all_data_network.bat"))
Check "finds a .bat-launched task"         $hits[0] "\Copy all data"

$hits = @(Find-TaskByCommand $csv @("flinders_copy.ps1", "copy_all_data_network.bat"))
Check "several needles find several tasks" $hits.Count 2
# Ambiguity is the swap script's abort condition, so it must be visible here.
Check "  which the swap treats as ambiguous" ($hits.Count -gt 1) $true

Check "an unrelated task is not matched"   (@(Find-TaskByCommand $csv @("defrag"))).Count 1
Check "no match returns nothing"           (@(Find-TaskByCommand $csv @("nothing_like_this"))).Count 0
Check "empty input returns nothing"        (@(Find-TaskByCommand "" @("x"))).Count 0
Check "junk input does not throw"          (@(Find-TaskByCommand "not,csv,at,all" @("x"))).Count 0

# ==== 3. the new task inherits the old one ==============================
Write-Host ""
Write-Host "3. the new task XML inherits schedule, account and working dir"

$oldXml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>PC\brett</Author>
    <Description>old copier</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT5M</Interval><Duration>P3650D</Duration></Repetition>
      <StartBoundary>2024-01-01T08:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principal id="Author">
    <UserId>PC\brett</UserId>
    <LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel>
  </Principal>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-File C:\Users\x\STAN\flinders_copy.ps1</Arguments>
      <WorkingDirectory>C:\Users\x\STAN</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
'@

$newXml = New-TaskXmlForCommand $oldXml "powershell.exe" "-File C:\FRAN\bruker_d_copy.ps1 D:\Data R:\arc"
if (-not $newXml) { Write-Host "  FAIL the rewrite produced nothing"; $Failures += 1 }
else {
    $doc = New-Object System.Xml.XmlDocument
    $doc.LoadXml($newXml)
    $ns = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")

    CheckLike "the command is now the new copier" `
        ($doc.SelectSingleNode("//t:Actions/t:Exec/t:Arguments", $ns).InnerText) "*bruker_d_copy.ps1*"
    Check "the OLD script is no longer referenced" `
        ($newXml -like "*flinders_copy.ps1*") $false

    # The point of rewriting rather than rebuilding: everything else survives.
    Check "the 5-minute repetition survives" `
        ($doc.SelectSingleNode("//t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", $ns).InnerText) "PT5M"
    Check "the start boundary survives" `
        ($doc.SelectSingleNode("//t:Triggers/t:TimeTrigger/t:StartBoundary", $ns).InnerText) "2024-01-01T08:00:00"
    Check "the account survives" `
        ($doc.SelectSingleNode("//t:Principal/t:UserId", $ns).InnerText) "PC\brett"
    Check "the logon type survives" `
        ($doc.SelectSingleNode("//t:Principal/t:LogonType", $ns).InnerText) "InteractiveToken"
    Check "the WORKING DIRECTORY survives" `
        ($doc.SelectSingleNode("//t:Actions/t:Exec/t:WorkingDirectory", $ns).InnerText) "C:\Users\x\STAN"
    Check "the instance policy survives" `
        ($doc.SelectSingleNode("//t:Settings/t:MultipleInstancesPolicy", $ns).InnerText) "IgnoreNew"
    Check "StartWhenAvailable survives" `
        ($doc.SelectSingleNode("//t:Settings/t:StartWhenAvailable", $ns).InnerText) "true"
    CheckLike "the description says what it is now" `
        ($doc.SelectSingleNode("//t:RegistrationInfo/t:Description", $ns).InnerText) "*safe Bruker*"
}

Check "a task with no Exec action is refused, not guessed at" `
    (New-TaskXmlForCommand "<Task xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task'><Actions/></Task>" "a" "b") ""
Check "unparseable XML is refused"  (New-TaskXmlForCommand "not xml" "a" "b") ""

# Inherited settings worth saying out loud.
Write-Host ""
Write-Host "   inherited settings that get flagged:"
$notes = @(Get-TaskDeviation $oldXml)
Check "a well-formed task flags only the logon type" $notes.Count 1
CheckLike "  which explains why InteractiveToken is right here" $notes[0] "*lets it reach a mapped share*"
$sysXml = $oldXml.Replace("<UserId>PC\brett</UserId>", "<UserId>S-1-5-18</UserId>")
CheckLike "a SYSTEM task warns about mapped drives" `
    ([string]::Join(" ", @(Get-TaskDeviation $sysXml))) "*SYSTEM cannot see mapped network drives*"
$unlimited = $oldXml.Replace("<ExecutionTimeLimit>PT6H</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>")
CheckLike "an unlimited run time is flagged" `
    ([string]::Join(" ", @(Get-TaskDeviation $unlimited))) "*will not be killed by the scheduler*"
$parallel = $oldXml.Replace("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>")
CheckLike "a Parallel instance policy is flagged" `
    ([string]::Join(" ", @(Get-TaskDeviation $parallel))) "*not 'IgnoreNew'*"

# ==== 4. the test sample ================================================
Write-Host ""
Write-Host "4. choosing the test sample"

$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "swaptest_$(Get-Random)"
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
function New-D($Name, [int] $Wal = -1, [switch] $NoDb) {
    $p = Join-Path $sandbox $Name
    New-Item -ItemType Directory -Path $p -Force | Out-Null
    if (-not $NoDb) { Set-Content -LiteralPath (Join-Path $p "analysis.tdf") -Value "x" }
    [System.IO.File]::WriteAllBytes((Join-Path $p "analysis.tdf_bin"), (New-Object byte[] 64))
    if ($Wal -ge 0) { [System.IO.File]::WriteAllBytes((Join-Path $p "analysis.tdf-wal"), (New-Object byte[] $Wal)) }
    return $p
}
Check "a plain .d is clean"              (Get-DHazard (New-D "a.d")) "clean"
Check "a live -wal is a hazard"          (Get-DHazard (New-D "b.d" 4404640)) "hazard"
Check "a zero-byte -wal is still clean"  (Get-DHazard (New-D "c.d" 0)) "clean"
Check "no database at all is 'nodb'"     (Get-DHazard (New-D "d.d" -1 -NoDb)) "nodb"

function Cand($Rel, $State) { return [PSCustomObject]@{ Rel = $Rel; Full = $Rel; State = $State } }

# The case that matters: the sample MUST contain a run that should be
# refused, even when clean runs vastly outnumber hazards.
$many = @(Cand "c1.d" "clean"; Cand "c2.d" "clean"; Cand "c3.d" "clean";
          Cand "c4.d" "clean"; Cand "c5.d" "clean"; Cand "h1.d" "hazard")
$s = @(Select-TestSample $many 4)
Check "the sample is the requested size"  $s.Count 4
$states = @()
foreach ($x in $s) { $states += $x.State }
Check "  and includes the ONE hazard"     ($states -contains "hazard") $true
Check "  and at least one clean"          ($states -contains "clean") $true

# No hazard available: the swap script must warn, not silently skip.
$allClean = @(Cand "c1.d" "clean"; Cand "c2.d" "clean")
$s = @(Select-TestSample $allClean 4)
Check "an all-clean source yields no hazard" (@($s | ForEach-Object { $_.State }) -contains "hazard") $false
Check "  and still samples the clean runs"   $s.Count 2

# A source of nothing but hazards still gets sampled -- and then the swap
# aborts, because there is nothing that SHOULD be accepted.
$allBad = @(Cand "h1.d" "hazard"; Cand "h2.d" "hazard")
$s = @(Select-TestSample $allBad 4)
Check "an all-hazard source is sampled too"  $s.Count 2

Check "runs with no database are not sampled" (@(Select-TestSample @(Cand "x.d" "nodb") 4)).Count 0
Check "an empty source yields an empty sample" (@(Select-TestSample @() 4)).Count 0
Check "the sample never repeats a run" `
    (@(Select-TestSample @(Cand "h1.d" "hazard"; Cand "c1.d" "clean") 4)).Count 2

# ==== 5. the shipped scripts refuse to act without -Execute =============
Write-Host ""
Write-Host "5. neither script acts without -Execute"

$swap = Join-Path $here "swap_to_safe_copier.ps1"
$rb = Join-Path $here "rollback_safe_copier.ps1"
$src = Join-Path $sandbox "src"; New-Item -ItemType Directory -Path $src -Force | Out-Null
# Give the dry run something to sample, or step 3 correctly aborts on an
# empty source and the happy path is never exercised.
$srcD = Join-Path $src "run1.d"
New-Item -ItemType Directory -Path $srcD -Force | Out-Null
Set-Content -LiteralPath (Join-Path $srcD "analysis.tdf") -Value "x"
[System.IO.File]::WriteAllBytes((Join-Path $srcD "analysis.tdf_bin"), (New-Object byte[] 64))
$arc = Join-Path $sandbox "arc"; New-Item -ItemType Directory -Path $arc -Force | Out-Null
$td  = Join-Path $sandbox "testdest"

$out = & $PSHOME/pwsh -NoProfile -File $swap -Source $src -Dest $arc -TestDest $td 2>&1
$text = [string]::Join("`n", @($out))
CheckLike "the swap announces a dry run"     $text "*DRY RUN*"
CheckLike "  and says what it WOULD do"      $text "*WOULD*"
CheckLike "  and ends without changing anything" $text "*Nothing was changed*"
Check "  and creates no test destination"    (Test-Path -LiteralPath $td) $false
CheckLike "  and prints the rollback it would need" $text "*rollback_safe_copier.ps1*"
CheckLike "  and warns there is no hazard run to refuse" $text "*NO .d WITH A LIVE -wal*"

# An abort during a DRY RUN must not claim the machine was left half-swapped.
$out = & $PSHOME/pwsh -NoProfile -File $swap -Source (Join-Path $sandbox "gone") -Dest $arc -TestDest $td 2>&1
$text = [string]::Join("`n", @($out))
CheckLike "a dry-run abort names the step"   $text "*ABORTED at step 0*"
CheckLike "  and says nothing was changed"   $text "*nothing was changed*"
Check "  and does NOT claim the old task is disabled" ($text -like "*task is DISABLED*") $false

# Pointing the test at the live archive must abort in preflight, dry run or
# not. This is the mistake that would turn a rehearsal into the incident.
$out = & $PSHOME/pwsh -NoProfile -File $swap -Source $src -Dest $arc -TestDest $arc 2>&1
$text = [string]::Join("`n", @($out))
CheckLike "aiming the test at the live archive ABORTS" $text "*ABORTED*"
CheckLike "  and says why"                              $text "*inside the live archive*"

$out = & $PSHOME/pwsh -NoProfile -File $rb -BackupXml (Join-Path $sandbox "nope.xml") -OldTaskName "x" 2>&1
CheckLike "rollback refuses a missing backup" ([string]::Join("`n", @($out))) "*ROLLBACK FAILED*"

$goodXml = Join-Path $sandbox "backup.xml"
Set-Content -LiteralPath $goodXml -Value $oldXml
$out = & $PSHOME/pwsh -NoProfile -File $rb -BackupXml $goodXml -OldTaskName "x" 2>&1
$text = [string]::Join("`n", @($out))
CheckLike "rollback dry-runs with a good backup" $text "*DRY RUN*"
CheckLike "  and keeps the backup, not consumes it" $text "*backup XML is kept*"
Check "  and the backup is still there"          (Test-Path -LiteralPath $goodXml) $true

$badXml = Join-Path $sandbox "bad.xml"
Set-Content -LiteralPath $badXml -Value "this is not xml"
$out = & $PSHOME/pwsh -NoProfile -File $rb -BackupXml $badXml -OldTaskName "x" 2>&1
CheckLike "rollback refuses an unparseable backup" ([string]::Join("`n", @($out))) "*does not parse*"

# ========================================================================
Write-Host ""
Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
if ($Failures -eq 0) { Write-Host "all tests passed"; exit 0 }
Write-Host "$Failures test(s) FAILED"
exit 1
