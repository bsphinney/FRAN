# test_bruker_d_copy.ps1
#
#     pwsh -NoProfile -File test_bruker_d_copy.ps1
#
# Tests for bruker_d_copy.ps1. Builds SYNTHETIC .d fixtures in a temp
# directory -- a real acquisition is never touched, and nothing here reads
# /nfs, /quobyte or /Volumes.
#
# The function definitions are pulled out of the shipped .ps1 through the
# PowerShell AST rather than copied, so the tests cannot drift from what
# ships, and the script's top-level pass never executes during the unit
# section. Section 9 then runs the REAL script end to end in -DryRun.
#
# There is no PowerShell on the dev Mac; a portable pwsh from the
# PowerShell/PowerShell release tarball runs this without installing
# anything. pwsh 7 on macOS is not PowerShell 5.1 on Windows -- see the
# "what this cannot test here" note in README.md.

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $here "bruker_d_copy.ps1"
if (-not (Test-Path -LiteralPath $target)) { Write-Host "cannot find $target"; exit 1 }

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref] $tokens, [ref] $errors)
if ($errors -and $errors.Count -gt 0) {
    Write-Host "PARSE ERRORS in $target"
    foreach ($err in $errors) { Write-Host "  line $($err.Extent.StartLineNumber): $($err.Message)" }
    exit 1
}
foreach ($func in $ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    Invoke-Expression $func.Extent.Text
}

$Failures = 0
function Check($Label, $Got, $Want) {
    if ("$Got" -eq "$Want") { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', want '$Want'"; $script:Failures += 1 }
}
function CheckLike($Label, $Got, $Pattern) {
    if ("$Got" -like $Pattern) { Write-Host "  ok   $Label" }
    else { Write-Host "  FAIL $Label -- got '$Got', wanted like '$Pattern'"; $script:Failures += 1 }
}

# The script's own settings, which its functions read off the script scope.
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) "brukerd_$(Get-Random)"
$ScratchDir = Join-Path $sandbox "scratch"
New-Item -ItemType Directory -Path $ScratchDir -Force | Out-Null
$LogFile = Join-Path $sandbox "test.log"
$StatusFile = Join-Path $sandbox "status.txt"
$RoboLog = Join-Path $sandbox "robocopy.log"
$Show = $false
$SettleSeconds = 600
$MinCoverage = 0.90
$MaxTdfCopyMB = 512
$NoLockProbe = $true      # macOS does not enforce the Windows share mode
$NoCoverage = $false
$Threads = 4
$InterPacketGapMs = 0
$Restartable = $false
$LookbackHours = 168
$MaxDepth = 4
$Only = @()
$All = $false
$ScriptVersion = "test"

$V_COPIED     = "copied"
$V_ALREADY    = "skipped-already-copied"
$V_ACQUIRING  = "skipped-still-acquiring"
$V_SETTLING   = "skipped-settling"
$V_HAZARD     = "skipped-hazard"
$V_INCOMPLETE = "skipped-incomplete"
$V_DESTHAZARD = "blocked-dest-hazard"
$V_COPYFAIL   = "copy-failed"
$V_VERIFYFAIL = "verify-failed"
$V_DRYRUN     = "dry-run-would-copy"

$sqlite = ""
$cmd = Get-Command "sqlite3" -ErrorAction SilentlyContinue
if ($cmd) { $sqlite = $cmd.Source }

# ---------------------------------------------------------- fixtures ----
#
# A synthetic .d. Real enough for every check the gate makes: an
# analysis.tdf that is a genuine SQLite database with a real Frames table,
# an analysis.tdf_bin of a chosen size, and the handful of small files a
# timsTOF .d carries beside them.
#
#   -Frames       how many rows in the index
#   -BinBytes     size of analysis.tdf_bin
#   -CoverFrac    what fraction of the binary the last frame's TimsId
#                 addresses. 0.999 is an intact run; 0.0074 is the
#                 measured damage this script exists to prevent.
#   -Wal          bytes of analysis.tdf-wal to leave beside it (-1 = none)
#   -Shm / -Journal   leave one of those markers behind
function New-FixtureD {
    param(
        [string] $Path,
        [int]    $Frames = 1000,
        [int]    $BinBytes = 100000,
        [double] $CoverFrac = 0.999,
        [int]    $Wal = -1,
        [int]    $Shm = -1,
        [int]    $Journal = -1,
        [switch] $NoTdf,
        [switch] $EmptyTdf,
        [switch] $NoBin
    )
    New-Item -ItemType Directory -Path $Path -Force | Out-Null

    if (-not $NoBin) {
        $bytes = New-Object byte[] $BinBytes
        [System.IO.File]::WriteAllBytes((Join-Path $Path "analysis.tdf_bin"), $bytes)
    }

    $tdf = Join-Path $Path "analysis.tdf"
    if ($EmptyTdf) {
        Set-Content -LiteralPath $tdf -Value "" -NoNewline
    } elseif (-not $NoTdf) {
        if (-not $script:sqlite) {
            # No sqlite3: still make a file, so the structural checks work.
            Set-Content -LiteralPath $tdf -Value "SQLite format 3 (stub)"
        } else {
            $maxTims = [int64] ($BinBytes * $CoverFrac)
            $step = 0
            if ($Frames -gt 1) { $step = [int64] [math]::Floor($maxTims / ($Frames - 1)) }
            $sql = @()
            $sql += "PRAGMA journal_mode=DELETE;"
            $sql += "CREATE TABLE Frames(Id INTEGER PRIMARY KEY, Time REAL, TimsId INTEGER, NumScans INTEGER, MsMsType INTEGER);"
            $sql += "CREATE TABLE GlobalMetadata(Key TEXT, Value TEXT);"
            $sql += "INSERT INTO GlobalMetadata VALUES('SchemaType','TDF');"
            $sql += "BEGIN;"
            for ($i = 0; $i -lt $Frames; $i++) {
                $t = [math]::Round($i * 0.0917, 4)
                $tims = $i * $step
                # The last frame lands exactly on -CoverFrac, so the fixture
                # reproduces the measured ratio rather than a rounded one.
                if ($i -eq ($Frames - 1)) { $tims = $maxTims }
                $sql += "INSERT INTO Frames VALUES($($i + 1), $t, $tims, 709, 0);"
            }
            $sql += "COMMIT;"
            $sqlFile = Join-Path $script:sandbox "mk_$(Get-Random).sql"
            Set-Content -LiteralPath $sqlFile -Value $sql
            & $script:sqlite $tdf ".read `"$sqlFile`"" 2>&1 | Out-Null
            Remove-Item -LiteralPath $sqlFile -Force -ErrorAction SilentlyContinue
        }
    }

    # The small companions a real .d carries.
    Set-Content -LiteralPath (Join-Path $Path "analysis.tdf_bin.md5") -Value "stub"
    New-Item -ItemType Directory -Path (Join-Path $Path "Baf2Sql") -Force | Out-Null
    Set-Content -LiteralPath (Join-Path (Join-Path $Path "Baf2Sql") "log.txt") -Value "stub"

    if ($Wal -ge 0) {
        $b = New-Object byte[] $Wal
        [System.IO.File]::WriteAllBytes((Join-Path $Path "analysis.tdf-wal"), $b)
    }
    if ($Shm -ge 0) {
        $b = New-Object byte[] $Shm
        [System.IO.File]::WriteAllBytes((Join-Path $Path "analysis.tdf-shm"), $b)
    }
    if ($Journal -ge 0) {
        $b = New-Object byte[] $Journal
        [System.IO.File]::WriteAllBytes((Join-Path $Path "analysis.tdf-journal"), $b)
    }
    return $Path
}

$fx = Join-Path $sandbox "fixtures"
New-Item -ItemType Directory -Path $fx -Force | Out-Null

# ==== 1. the side-file gate =============================================
Write-Host ""
Write-Host "1. side files -- the hazard itself"

$clean    = New-FixtureD (Join-Path $fx "clean.d")
$liveWal  = New-FixtureD (Join-Path $fx "live_wal.d")  -Wal 4404640
$tinyWal  = New-FixtureD (Join-Path $fx "tiny_wal.d")  -Wal 32
$zeroWal  = New-FixtureD (Join-Path $fx "zero_wal.d")  -Wal 0
$withShm  = New-FixtureD (Join-Path $fx "with_shm.d")  -Shm 32768
$zeroShm  = New-FixtureD (Join-Path $fx "zero_shm.d")  -Shm 0
$withJrn  = New-FixtureD (Join-Path $fx "with_jrn.d")  -Journal 512

Check "clean .d has no hazard"                  (Test-SideFileHazard $clean) ""
CheckLike "a 4.4 MB -wal is refused"            (Test-SideFileHazard $liveWal) "live -wal*4404640 bytes*"
CheckLike "even a 32-byte -wal is refused"      (Test-SideFileHazard $tinyWal) "live -wal*"
Check "a zero-byte -wal is allowed"             (Test-SideFileHazard $zeroWal) ""
CheckLike "a -shm is refused"                   (Test-SideFileHazard $withShm) "open-connection marker*analysis.tdf-shm*"
CheckLike "a ZERO-byte -shm is still refused"   (Test-SideFileHazard $zeroShm) "open-connection marker*"
CheckLike "a -journal is refused"               (Test-SideFileHazard $withJrn) "open-connection marker*analysis.tdf-journal*"

# A second SQLite database inside the .d has its own -wal, and it counts.
$nested = New-FixtureD (Join-Path $fx "nested_wal.d")
Set-Content -LiteralPath (Join-Path $nested "chromatography-data.sqlite") -Value "stub"
[System.IO.File]::WriteAllBytes((Join-Path $nested "chromatography-data.sqlite-wal"), (New-Object byte[] 8192))
CheckLike "chromatography-data.sqlite-wal is refused too" (Test-SideFileHazard $nested) "live -wal*chromatography-data.sqlite-wal*"

$sf = @(Get-SideFile $liveWal)
Check "Get-SideFile finds exactly one in live_wal.d" $sf.Count 1
# Regression: returning ",$out" from Get-SideFile hands the CALLER an array
# wrapping the array, so $f becomes the collection, $f.Length becomes its
# element count, and a 4.4 MB WAL is reported as "1 bytes" -- and passes.
Check "  and the element is a file, with the real size" $sf[0].Length 4404640
Check "Get-SideFile finds none in clean.d"           (@(Get-SideFile $clean)).Count 0

# ==== 2. the stability probe ============================================
Write-Host ""
Write-Host "2. stability probe"

$p1 = Get-TreeProbe $clean
$p2 = Get-TreeProbe $clean
Check "the same tree probes identically" $p1.Sig $p2.Sig
Check "probe counts the files"           $p1.Files 4

# A file growing changes the signature.
$growing = New-FixtureD (Join-Path $fx "growing.d")
$g1 = Get-TreeProbe $growing
[System.IO.File]::WriteAllBytes((Join-Path $growing "analysis.tdf_bin"), (New-Object byte[] 200000))
$g2 = Get-TreeProbe $growing
if ($g1.Sig -ne $g2.Sig) { Write-Host "  ok   a growing tdf_bin changes the signature" }
else { Write-Host "  FAIL a growing tdf_bin changes the signature"; $Failures += 1 }

# The byte-total-only fingerprint the old copier used cannot see a
# checkpoint: bytes move out of the -wal and into the tdf and the total is
# unchanged. The per-file probe does see it.
$ck = New-FixtureD (Join-Path $fx "checkpoint.d")
[System.IO.File]::WriteAllBytes((Join-Path $ck "analysis.tdf-wal"), (New-Object byte[] 4096))
[System.IO.File]::WriteAllBytes((Join-Path $ck "padding.bin"), (New-Object byte[] 4096))
$c1 = Get-TreeProbe $ck
[System.IO.File]::WriteAllBytes((Join-Path $ck "analysis.tdf-wal"), (New-Object byte[] 0))
[System.IO.File]::WriteAllBytes((Join-Path $ck "padding.bin"), (New-Object byte[] 8192))
$c2 = Get-TreeProbe $ck
Check "bytes/count alone would call this unchanged" "$($c1.Files)/$($c1.Bytes)" "$($c2.Files)/$($c2.Bytes)"
if ($c1.Sig -ne $c2.Sig) { Write-Host "  ok   the per-file probe still sees the change" }
else { Write-Host "  FAIL the per-file probe still sees the change"; $Failures += 1 }

# ==== 3. the sqlite URI ==================================================
Write-Host ""
Write-Host "3. sqlite URI construction"

$u = ConvertTo-SqliteUri "C:\Data\Aug26\run.d\analysis.tdf"
Check "a Windows path"  $u "file:///C:/Data/Aug26/run.d/analysis.tdf?mode=ro&immutable=1"
$u2 = ConvertTo-SqliteUri "/tmp/x/analysis.tdf"
Check "a POSIX path"    $u2 "file:///tmp/x/analysis.tdf?mode=ro&immutable=1"
$u3 = ConvertTo-SqliteUri "C:\Data\100%_HeLa#2\analysis.tdf"
Check "percent and hash are encoded" $u3 "file:///C:/Data/100%25_HeLa%232/analysis.tdf?mode=ro&immutable=1"
if ($u -like "*immutable=1*" -and $u -notlike "*mode=rw*") { Write-Host "  ok   always immutable, never read-write" }
else { Write-Host "  FAIL always immutable, never read-write"; $Failures += 1 }

if ($sqlite) {
    $Sqlite3 = $sqlite
    $found = Initialize-Sqlite
    if ($found) { Write-Host "  ok   Initialize-Sqlite proved URI support against a scratch db" }
    else { Write-Host "  FAIL Initialize-Sqlite proved URI support"; $Failures += 1 }

    # A binary that is not sqlite3 at all must be rejected, not trusted.
    $fake = Join-Path $sandbox "notsqlite"
    Set-Content -LiteralPath $fake -Value "#!/bin/sh`necho hello"
    & chmod +x $fake 2>&1 | Out-Null
    $Sqlite3 = $fake
    Check "a binary that fails the URI proof is rejected" (Initialize-Sqlite) ""
    $Sqlite3 = $sqlite
} else {
    Write-Host "  SKIP sqlite3 not on PATH -- coverage tests skipped"
}

# ==== 4. index coverage ==================================================
Write-Host ""
Write-Host "4. index coverage -- the strongest check"

if ($sqlite) {
    $exe = Initialize-Sqlite

    $whole = New-FixtureD (Join-Path $fx "whole.d") -Frames 13736 -BinBytes 2400000 -CoverFrac 0.9999
    $info = Get-TdfInfo $whole $exe
    Check "an intact run reads Ok"        $info.Ok $true
    Check "13,736 frames"                 $info.Frames 13736
    if ($info.Coverage -gt 0.99) { Write-Host "  ok   coverage $($info.Coverage) is near 1" }
    else { Write-Host "  FAIL coverage near 1 -- got $($info.Coverage)"; $Failures += 1 }

    # The measured damage: 1,451 frames addressing 0.74% of the binary.
    $wrecked = New-FixtureD (Join-Path $fx "wrecked.d") -Frames 1451 -BinBytes 2400000 -CoverFrac 0.0074
    $winfo = Get-TdfInfo $wrecked $exe
    Check "the truncated index reads Ok"  $winfo.Ok $true
    Check "1,451 frames"                  $winfo.Frames 1451
    if ($winfo.Coverage -lt 0.01) { Write-Host "  ok   coverage $($winfo.Coverage) is the 0.74% signature" }
    else { Write-Host "  FAIL coverage under 0.01 -- got $($winfo.Coverage)"; $Failures += 1 }

    # The source file must be untouched by having been inspected. This is
    # the property the whole script turns on.
    $before = (Get-Item -LiteralPath (Join-Path $whole "analysis.tdf"))
    $hBefore = (Get-FileHash -LiteralPath $before.FullName -Algorithm SHA256).Hash
    Get-TdfInfo $whole $exe | Out-Null
    Get-TdfInfo $whole $exe | Out-Null
    $hAfter = (Get-FileHash -LiteralPath $before.FullName -Algorithm SHA256).Hash
    Check "inspecting the index does not change it" $hAfter $hBefore
    Check "no -wal was created by inspecting it"    (@(Get-SideFile $whole)).Count 0
    Check "scratch is cleaned up"                   (@(Get-ChildItem -LiteralPath $ScratchDir -File -Force)).Count 0

    # A Frames table without TimsId is a format we do not understand, and
    # saying so beats a confident number from a guess.
    $odd = Join-Path $fx "odd.d"
    New-Item -ItemType Directory -Path $odd -Force | Out-Null
    [System.IO.File]::WriteAllBytes((Join-Path $odd "analysis.tdf_bin"), (New-Object byte[] 1000))
    & $sqlite (Join-Path $odd "analysis.tdf") "create table Frames(Id INTEGER, Time REAL);" 2>&1 | Out-Null
    $oinfo = Get-TdfInfo $odd $exe
    Check "an unknown Frames schema is reported, not guessed" $oinfo.Ok $false
    CheckLike "  and it says why" $oinfo.Error "*no TimsId*"
} else {
    Write-Host "  SKIP no sqlite3"
}

# ==== 5. the gate, end to end ============================================
Write-Host ""
Write-Host "5. the gate"

$now = 1700000000
$exe = ""
if ($sqlite) { $exe = Initialize-Sqlite }

# First sighting: nothing is copied on the strength of one probe.
$g = Test-DReady $clean "clean.d" (Get-TreeProbe $clean) @{} $now $exe
Check "first sighting settles"  $g.Verdict $V_SETTLING

# Stable, but not for long enough.
$probe = Get-TreeProbe $clean
$tooSoon = @{ "clean.d" = "$($now - 30)|$($probe.Sig)" }
$g = Test-DReady $clean "clean.d" (Get-TreeProbe $clean) $tooSoon $now $exe
Check "stable for 30 s of 600 s settles" $g.Verdict $V_SETTLING
CheckLike "  and says how far in"        $g.Reason "stable for 30 s of 600 s"

# THE CASE THE OLD COPIER GOT WRONG: two probes that agree but are only
# seconds apart. Without the elapsed-time check this copies a live run.
$justNow = @{ "clean.d" = "$now|$($probe.Sig)" }
$g = Test-DReady $clean "clean.d" (Get-TreeProbe $clean) $justNow $now $exe
Check "two probes 0 s apart do NOT pass" $g.Verdict $V_SETTLING

# Stable for long enough: accepted.
$settled = @{ "clean.d" = "$($now - 700)|$($probe.Sig)" }
$g = Test-DReady $clean "clean.d" (Get-TreeProbe $clean) $settled $now $exe
Check "a clean, settled .d is ACCEPTED" $g.Verdict "ready"

# Signature moved: still acquiring.
$moved = @{ "clean.d" = "$($now - 700)|999/999/deadbeefdeadbeef" }
$g = Test-DReady $clean "clean.d" (Get-TreeProbe $clean) $moved $now $exe
Check "a changed tree is still-acquiring" $g.Verdict $V_ACQUIRING

# THE HEADLINE: a live -wal is refused even when everything else says go.
$wp = Get-TreeProbe $liveWal
$walSettled = @{ "live_wal.d" = "$($now - 700)|$($wp.Sig)" }
$g = Test-DReady $liveWal "live_wal.d" (Get-TreeProbe $liveWal) $walSettled $now $exe
Check "a settled .d with a 4.4 MB -wal is REFUSED" $g.Verdict $V_HAZARD
CheckLike "  and names the file"                   $g.Reason "*analysis.tdf-wal*"

# A zero-byte -wal does not block an otherwise finished run.
$zp = Get-TreeProbe $zeroWal
$g = Test-DReady $zeroWal "zero_wal.d" (Get-TreeProbe $zeroWal) @{ "zero_wal.d" = "$($now - 700)|$($zp.Sig)" } $now $exe
Check "a zero-byte -wal still passes" $g.Verdict "ready"

# A -shm blocks it.
$sp = Get-TreeProbe $withShm
$g = Test-DReady $withShm "with_shm.d" (Get-TreeProbe $withShm) @{ "with_shm.d" = "$($now - 700)|$($sp.Sig)" } $now $exe
Check "a settled .d with a -shm is REFUSED" $g.Verdict $V_HAZARD

# Structural refusals.
$noTdf = New-FixtureD (Join-Path $fx "no_tdf.d") -NoTdf
$np = Get-TreeProbe $noTdf
$g = Test-DReady $noTdf "no_tdf.d" (Get-TreeProbe $noTdf) @{ "no_tdf.d" = "$($now - 700)|$($np.Sig)" } $now $exe
Check "no analysis.tdf is incomplete" $g.Verdict $V_INCOMPLETE

$emptyTdf = New-FixtureD (Join-Path $fx "empty_tdf.d") -EmptyTdf
$ep = Get-TreeProbe $emptyTdf
$g = Test-DReady $emptyTdf "empty_tdf.d" (Get-TreeProbe $emptyTdf) @{ "empty_tdf.d" = "$($now - 700)|$($ep.Sig)" } $now $exe
Check "a zero-byte analysis.tdf is incomplete" $g.Verdict $V_INCOMPLETE

if ($sqlite) {
    # A .d already truncated by an earlier bad copy: stable, clean, closed --
    # and refused, because its index does not reach its spectra.
    $wr = Get-TreeProbe $wrecked
    $g = Test-DReady $wrecked "wrecked.d" (Get-TreeProbe $wrecked) @{ "wrecked.d" = "$($now - 700)|$($wr.Sig)" } $now $exe
    Check "an already-truncated .d is REFUSED"  $g.Verdict $V_INCOMPLETE
    CheckLike "  and quotes the coverage"       $g.Reason "*0.74% of analysis.tdf_bin*"

    # The mirror-image damage: the index reaches past the end of the binary.
    $shortBin = New-FixtureD (Join-Path $fx "shortbin.d") -Frames 500 -BinBytes 10000 -CoverFrac 1.5
    $sb = Get-TreeProbe $shortBin
    $g = Test-DReady $shortBin "shortbin.d" (Get-TreeProbe $shortBin) @{ "shortbin.d" = "$($now - 700)|$($sb.Sig)" } $now $exe
    Check "an index reaching past the binary is REFUSED" $g.Verdict $V_INCOMPLETE
    CheckLike "  and says the binary is truncated"       $g.Reason "*binary is truncated*"

    $wh = Get-TreeProbe $whole
    $g = Test-DReady $whole "whole.d" (Get-TreeProbe $whole) @{ "whole.d" = "$($now - 700)|$($wh.Sig)" } $now $exe
    Check "the intact sibling is ACCEPTED"      $g.Verdict "ready"
    Check "  with its frame count"              $g.Frames 13736

    # Coverage unavailable must be recorded, never silently treated as a pass
    # of the coverage check.
    $g = Test-DReady $whole "whole.d" (Get-TreeProbe $whole) @{ "whole.d" = "$($now - 700)|$($wh.Sig)" } $now ""
    Check "no sqlite3 still lets the other checks stand" $g.Verdict "ready"
    CheckLike "  but records that coverage was not run"  $g.Reason "*coverage unavailable*"
}

# ==== 6. robocopy flags ==================================================
Write-Host ""
Write-Host "6. robocopy arguments"

$a = [string]::Join(" ", (Get-RobocopyArgs "D:\Data\Aug26\run.d" "R:\arc\Aug26\run.d"))
foreach ($want in @("/E", "/COPY:DAT", "/DCOPY:DAT", "/FFT", "/R:2", "/W:10", "/XF", "/NP", "/MT:4")) {
    if ($a -like "*$want*") { Write-Host "  ok   has $want" }
    else { Write-Host "  FAIL has $want -- got: $a"; $Failures += 1 }
}
foreach ($never in @("analysis.tdf-wal", "analysis.tdf-shm", "analysis.tdf-journal", "*-wal", "*-shm", "*-journal")) {
    if ($a -like "*$never*") { Write-Host "  ok   excludes $never" }
    else { Write-Host "  FAIL excludes $never"; $Failures += 1 }
}
# /Z is the copier-we-replace's flag and is OFF unless asked for: resuming a
# transfer is only safe if robocopy spots that the source changed, and that is
# a size/timestamp compare which /FFT blunts to 2 seconds.
foreach ($banned in @("/MIR", "/PURGE", "/MOVE", "/MOV ", "/ZB", "/COPYALL", "/Z")) {
    if ($a -notlike "*$banned*") { Write-Host "  ok   does NOT use $banned" }
    else { Write-Host "  FAIL does NOT use $banned -- got: $a"; $Failures += 1 }
}
if ($a -notlike "*/R:1000000*") { Write-Host "  ok   not the 1,000,000-retry default" }
else { Write-Host "  FAIL not the 1,000,000-retry default"; $Failures += 1 }
$Restartable = $true
$z = [string]::Join(" ", (Get-RobocopyArgs "a" "b"))
if ($z -like "*/Z*") { Write-Host "  ok   -Restartable puts /Z back when asked" }
else { Write-Host "  FAIL -Restartable puts /Z back"; $Failures += 1 }
$Restartable = $false

# /MT and /IPG are mutually exclusive; setting IPG must drop MT.
$InterPacketGapMs = 20
$b = [string]::Join(" ", (Get-RobocopyArgs "a" "b"))
if ($b -like "*/IPG:20*" -and $b -notlike "*/MT*") { Write-Host "  ok   /IPG set drops /MT (robocopy rejects the pair)" }
else { Write-Host "  FAIL /IPG set drops /MT -- got: $b"; $Failures += 1 }
$InterPacketGapMs = 0
$Threads = 0
$c = [string]::Join(" ", (Get-RobocopyArgs "a" "b"))
if ($c -notlike "*/MT*") { Write-Host "  ok   -Threads 0 leaves /MT off" }
else { Write-Host "  FAIL -Threads 0 leaves /MT off"; $Failures += 1 }
$Threads = 4

# ==== 7. verification ====================================================
Write-Host ""
Write-Host "7. post-copy verification"

$src = New-FixtureD (Join-Path $fx "vsrc.d") -Frames 500 -BinBytes 50000
$dst = Join-Path $fx "vdst.d"

function Copy-Tree($From, $To) {
    # Stands in for robocopy /XF: copies the tree, dropping the side files.
    New-Item -ItemType Directory -Path $To -Force | Out-Null
    foreach ($f in @(Get-ChildItem -LiteralPath $From -Recurse -File -Force)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n.EndsWith("-wal") -or $n.EndsWith("-shm") -or $n.EndsWith("-journal")) { continue }
        $rel = Get-RelPath $From $f.FullName
        $out = Join-Path $To $rel
        $par = Split-Path -Parent $out
        if (-not (Test-Path -LiteralPath $par)) { New-Item -ItemType Directory -Path $par -Force | Out-Null }
        Copy-Item -LiteralPath $f.FullName -Destination $out -Force
    }
}

Copy-Tree $src $dst
$srcInfo = $null
if ($sqlite) { $si = Get-TdfInfo $src $exe; $srcInfo = [PSCustomObject]@{ Frames = $si.Frames; Coverage = $si.Coverage } }
$v = Test-CopyVerified $src $dst $srcInfo $exe
Check "a good copy verifies" $v.Ok $true

# A truncated destination file.
$bad = Join-Path $fx "vbad.d"
Copy-Tree $src $bad
[System.IO.File]::WriteAllBytes((Join-Path $bad "analysis.tdf_bin"), (New-Object byte[] 1234))
$v = Test-CopyVerified $src $bad $srcInfo $exe
Check "a truncated destination file fails"  $v.Ok $false
CheckLike "  and names it"                  $v.Reason "*analysis.tdf_bin*1234 bytes*"

# A missing destination file.
$miss = Join-Path $fx "vmiss.d"
Copy-Tree $src $miss
Remove-Item -LiteralPath (Join-Path $miss "analysis.tdf_bin") -Force
$v = Test-CopyVerified $src $miss $srcInfo $exe
Check "a missing destination file fails"    $v.Ok $false
CheckLike "  and names it"                  $v.Reason "*missing analysis.tdf_bin*"

# A side file that somehow reached the destination.
$sided = Join-Path $fx "vside.d"
Copy-Tree $src $sided
[System.IO.File]::WriteAllBytes((Join-Path $sided "analysis.tdf-wal"), (New-Object byte[] 4404640))
$v = Test-CopyVerified $src $sided $srcInfo $exe
Check "a -wal at the destination fails verification" $v.Ok $false
CheckLike "  and says so"                            $v.Reason "*side file at the destination*"

# A missing destination folder.
$v = Test-CopyVerified $src (Join-Path $fx "not_there.d") $srcInfo $exe
Check "a missing destination folder fails" $v.Ok $false

# The source's own zero-byte -wal must not make the comparison fail.
$zsrc = New-FixtureD (Join-Path $fx "zsrc.d") -Frames 100 -BinBytes 5000 -Wal 0
$zdst = Join-Path $fx "zdst.d"
Copy-Tree $zsrc $zdst
$v = Test-CopyVerified $zsrc $zdst $null $exe
Check "a zero-byte source -wal is not counted against the copy" $v.Ok $true

Check "Get-FileMap leaves side files out" (Get-FileMap $liveWal).Count (Get-FileMap $clean).Count

# ==== 8. candidate discovery =============================================
Write-Host ""
Write-Host "8. candidate discovery"

$scan = Join-Path $sandbox "scan"
New-Item -ItemType Directory -Path $scan -Force | Out-Null
New-FixtureD (Join-Path $scan "top_level.d") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scan "Aug26") -Force | Out-Null
New-FixtureD (Join-Path (Join-Path $scan "Aug26") "in_month.d") | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scan "Aug26") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $scan "notadotd") -Force | Out-Null

$Source = $scan
$All = $true
$found = @(Get-Candidate)
Check "finds both the top-level and the month-folder .d" $found.Count 2
$names = @()
foreach ($f in $found) { $names += $f.Name }
$sortedNames = [string[]] $names
[array]::Sort($sortedNames)
Check "  by name" ([string]::Join(",", $sortedNames)) "in_month.d,top_level.d"
Check "relative path keeps the month folder" ((Get-RelPath $scan (Join-Path (Join-Path $scan "Aug26") "in_month.d")).Replace("\","/")) "Aug26/in_month.d"

# The copier this replaces uses /E, so a .d can sit at any depth. A fixed
# two-level scan would silently archive nothing from a deeper layout.
$deep = Join-Path (Join-Path (Join-Path $scan "2026") "Sep") "operator"
New-Item -ItemType Directory -Path $deep -Force | Out-Null
New-FixtureD (Join-Path $deep "deep.d") | Out-Null
Check "finds a .d four levels down"       (@(Get-Candidate)).Count 3
$MaxDepth = 2
Check "-MaxDepth 2 stops short of it"     (@(Get-Candidate)).Count 2
$MaxDepth = 4
# And never descends INTO a .d, whatever it holds.
New-Item -ItemType Directory -Path (Join-Path (Join-Path $scan "top_level.d") "inner.d") -Force | Out-Null
Check "never descends into a .d"          (@(Get-Candidate)).Count 3

# -Only: a targeted run, which is what the staged swap test needs.
$Only = @("in_month.d")
Check "-Only takes a bare folder name"    (@(Get-Candidate)).Count 1
$Only = @("Aug26/in_month.d")
Check "-Only takes a relative path"       (@(Get-Candidate)).Count 1
$Only = @("Aug26\in_month.d")
Check "-Only takes a backslash path"      (@(Get-Candidate)).Count 1
$Only = @("in_month.d", "top_level.d")
Check "-Only takes several"               (@(Get-Candidate)).Count 2
$Only = @("nope.d")
Check "-Only with no match selects none"  (@(Get-Candidate)).Count 0
$LookbackHours = 0
$Only = @("in_month.d")
Check "-Only overrides the age filter"    (@(Get-Candidate)).Count 1
$Only = @()
$LookbackHours = 168

$All = $false
$LookbackHours = 168
Check "a fresh .d is inside the lookback" (@(Get-Candidate)).Count 3
$LookbackHours = 0
Check "a zero lookback excludes everything" (@(Get-Candidate)).Count 0
$LookbackHours = 168

# Positional invocation: the drop-in shape, `script <src> <dst>`.
$posState = Join-Path $sandbox "pos_state"
& $PSHOME/pwsh -NoProfile -File $target $scan (Join-Path $sandbox "pos_dst_missing") `
    -StateDir $posState -DryRun 2>&1 | Out-Null
CheckLike "positional <src> <dst> binds like robocopy" `
    (Get-Content -LiteralPath (Join-Path $posState "status.txt") -Raw) "*NO DESTINATION*"
$LookbackHours = 168

# ==== 9. the real script, end to end, in -DryRun =========================
Write-Host ""
Write-Host "9. the shipped script, one whole pass (-DryRun)"

$e2eSrc = Join-Path $sandbox "e2e_src"
$e2eDst = Join-Path $sandbox "e2e_dst"
$e2eState = Join-Path $sandbox "e2e_state"
New-Item -ItemType Directory -Path $e2eSrc -Force | Out-Null
New-Item -ItemType Directory -Path $e2eDst -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $e2eSrc "Sep26") -Force | Out-Null

New-FixtureD (Join-Path (Join-Path $e2eSrc "Sep26") "good.d")    -Frames 2000 -BinBytes 200000 -CoverFrac 0.9995 | Out-Null
New-FixtureD (Join-Path (Join-Path $e2eSrc "Sep26") "livewal.d") -Frames 2000 -BinBytes 200000 -Wal 4404640 | Out-Null
New-FixtureD (Join-Path (Join-Path $e2eSrc "Sep26") "broken.d")  -Frames 1451 -BinBytes 2400000 -CoverFrac 0.0074 | Out-Null
New-FixtureD (Join-Path (Join-Path $e2eSrc "Sep26") "notdf.d")   -NoTdf | Out-Null

# An archive entry already sitting in the hazard state, like the 113 on the
# cluster right now.
New-Item -ItemType Directory -Path (Join-Path $e2eDst "Sep26") -Force | Out-Null
New-FixtureD (Join-Path (Join-Path $e2eDst "Sep26") "good.d") -Frames 2000 -BinBytes 200000 -Wal 4300000 | Out-Null

$common = @("-Source", $e2eSrc, "-Dest", $e2eDst, "-StateDir", $e2eState,
            "-SettleSeconds", "0", "-NoLockProbe", "-All", "-DryRun")
& $PSHOME/pwsh -NoProfile -File $target @common 2>&1 | Out-Null
& $PSHOME/pwsh -NoProfile -File $target @common 2>&1 | Out-Null   # second pass: settled

$outFile = Join-Path $e2eState "outcomes.jsonl"
if (-not (Test-Path -LiteralPath $outFile)) {
    Write-Host "  FAIL the JSON Lines outcome log was written"; $Failures += 1
} else {
    Write-Host "  ok   the JSON Lines outcome log was written"
    $last = @{}
    foreach ($line in @(Get-Content -LiteralPath $outFile)) {
        if (-not $line.Trim()) { continue }
        $o = $line | ConvertFrom-Json
        $last[($o.run.Replace("\", "/"))] = $o
    }
    Check "every .d has an outcome" $last.Count 4
    Check "the live -wal run is refused"      $last["Sep26/livewal.d"].verdict $V_HAZARD
    Check "the truncated run is refused"      $last["Sep26/broken.d"].verdict  $V_INCOMPLETE
    Check "the malformed run is refused"      $last["Sep26/notdf.d"].verdict   $V_INCOMPLETE
    # good.d is blocked because the ARCHIVE copy carries a stale -wal.
    Check "a hazardous ARCHIVE copy blocks the run" $last["Sep26/good.d"].verdict $V_DESTHAZARD
    CheckLike "  and names the stale -wal"          $last["Sep26/good.d"].reason "*analysis.tdf-wal*"

    # Clear the destination hazard and the same run should now pass the gate.
    Remove-Item -LiteralPath (Join-Path (Join-Path (Join-Path $e2eDst "Sep26") "good.d") "analysis.tdf-wal") -Force
    & $PSHOME/pwsh -NoProfile -File $target @common 2>&1 | Out-Null
    $last2 = @{}
    foreach ($line in @(Get-Content -LiteralPath $outFile)) {
        if (-not $line.Trim()) { continue }
        $o = $line | ConvertFrom-Json
        $last2[($o.run.Replace("\", "/"))] = $o
    }
    Check "with the archive clean, the good run passes the gate" $last2["Sep26/good.d"].verdict $V_DRYRUN
    Check "  and reports its frame count"                        $last2["Sep26/good.d"].frames 2000

    Check "a status file was written" (Test-Path -LiteralPath (Join-Path $e2eState "status.txt")) $true
    Check "no lock file is left behind" (Test-Path -LiteralPath (Join-Path $e2eState "pass.lock")) $false
    Check "-DryRun copied nothing"    (Test-Path -LiteralPath (Join-Path (Join-Path $e2eDst "Sep26") "livewal.d")) $false

    # Nothing the pass looked at was modified.
    Check "no -wal appeared in the clean source run" (@(Get-SideFile (Join-Path (Join-Path $e2eSrc "Sep26") "good.d"))).Count 0
}

# A missing destination is refused rather than invented.
$noDest = Join-Path $sandbox "no_such_dest"
& $PSHOME/pwsh -NoProfile -File $target -Source $e2eSrc -Dest $noDest -StateDir (Join-Path $sandbox "e2e_state2") -DryRun 2>&1 | Out-Null
Check "an unreachable destination is not created" (Test-Path -LiteralPath $noDest) $false

# ==== 10. copy, verify and record, against a robocopy stand-in ==========
Write-Host ""
Write-Host "10. the copy path (robocopy stand-in)"

# robocopy is Windows-only, so the copy-verify-record half of a pass could
# not otherwise be exercised here at all. This shim implements just the two
# behaviours the script depends on -- copy the tree, honour /XF -- and lets
# the REAL script drive it. -Truncate makes it corrupt one file so the
# verification step has something to catch.
function New-RoboShim($Path, [switch] $Truncate) {
    $body = @(
        '#!/bin/sh',
        'src=$1; shift; dst=$1; shift',
        'src=`echo "$src" | tr -d ''"'' `',
        'dst=`echo "$dst" | tr -d ''"'' `',
        'mkdir -p "$dst"',
        'cd "$src" || exit 16',
        'find . -type f | while read -r f; do',
        '  case "$f" in *-wal|*-shm|*-journal) continue;; esac',
        '  mkdir -p "$dst/`dirname "$f"`"',
        '  cp "$f" "$dst/$f"',
        'done'
    )
    if ($Truncate) { $body += 'printf "x" > "$dst/analysis.tdf_bin"' }
    $body += 'exit 1'
    Set-Content -LiteralPath $Path -Value $body
    & chmod +x $Path 2>&1 | Out-Null
    return $Path
}

$cpSrc   = Join-Path $sandbox "cp_src"
$cpDst   = Join-Path $sandbox "cp_dst"
$cpState = Join-Path $sandbox "cp_state"
New-Item -ItemType Directory -Path $cpSrc -Force | Out-Null
New-Item -ItemType Directory -Path $cpDst -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $cpSrc "Sep26") -Force | Out-Null
# A finished run, carrying the zero-byte -wal a clean close can leave behind.
New-FixtureD (Join-Path (Join-Path $cpSrc "Sep26") "run1.d") -Frames 800 -BinBytes 80000 -CoverFrac 0.999 -Wal 0 | Out-Null
# And one still acquiring.
New-FixtureD (Join-Path (Join-Path $cpSrc "Sep26") "run2.d") -Frames 800 -BinBytes 80000 -Wal 4404640 | Out-Null

$shim = New-RoboShim (Join-Path $sandbox "roboshim.sh")
$cpArgs = @("-Source", $cpSrc, "-Dest", $cpDst, "-StateDir", $cpState,
            "-SettleSeconds", "0", "-NoLockProbe", "-All", "-RobocopyExe", $shim)

& $PSHOME/pwsh -NoProfile -File $target @cpArgs 2>&1 | Out-Null   # pass 1: settling
& $PSHOME/pwsh -NoProfile -File $target @cpArgs 2>&1 | Out-Null   # pass 2: copies

function Get-LastOutcomes($StateRoot) {
    $map = @{}
    $f = Join-Path $StateRoot "outcomes.jsonl"
    if (-not (Test-Path -LiteralPath $f)) { return $map }
    foreach ($line in @(Get-Content -LiteralPath $f)) {
        if (-not $line.Trim()) { continue }
        $o = $line | ConvertFrom-Json
        $map[($o.run.Replace("\", "/"))] = $o
    }
    return $map
}

$o = Get-LastOutcomes $cpState
Check "the finished run is copied"        $o["Sep26/run1.d"].verdict $V_COPIED
Check "the acquiring run is not"          $o["Sep26/run2.d"].verdict $V_HAZARD
Check "the archive has the run"           (Test-Path -LiteralPath (Join-Path (Join-Path $cpDst "Sep26") "run1.d")) $true
Check "the archive does NOT have the acquiring run" (Test-Path -LiteralPath (Join-Path (Join-Path $cpDst "Sep26") "run2.d")) $false

# THE WHOLE POINT: the source's -wal did not travel, so nothing downstream
# can ever checkpoint a stale WAL into the archived index.
Check "no side file reached the archive"  (@(Get-SideFile (Join-Path (Join-Path $cpDst "Sep26") "run1.d"))).Count 0
Check "  not even the zero-byte -wal"     (Test-Path -LiteralPath (Join-Path (Join-Path (Join-Path $cpDst "Sep26") "run1.d") "analysis.tdf-wal")) $false
Check "the source keeps its own -wal"     (Test-Path -LiteralPath (Join-Path (Join-Path (Join-Path $cpSrc "Sep26") "run1.d") "analysis.tdf-wal")) $true
Check "the archived index still has its frames" $o["Sep26/run1.d"].destFrames 800

# Idempotent: a third pass must not copy it again. A run in the steady state
# writes no outcome line at all, so the evidence is that the log did not grow
# and the archived file was not rewritten.
$archived = Join-Path (Join-Path (Join-Path $cpDst "Sep26") "run1.d") "analysis.tdf"
$before = (Get-Item -LiteralPath $archived).LastWriteTimeUtc
# run1 only: run2 is still acquiring and correctly appends a line each pass.
function Count-RunLines($StateRoot, $Needle) {
    $n = 0
    foreach ($line in @(Get-Content -LiteralPath (Join-Path $StateRoot "outcomes.jsonl"))) {
        if ($line -like "*$Needle*") { $n += 1 }
    }
    return $n
}
$linesBefore = Count-RunLines $cpState "run1.d"
& $PSHOME/pwsh -NoProfile -File $target @cpArgs 2>&1 | Out-Null
$linesAfter = Count-RunLines $cpState "run1.d"
Check "a re-run appends nothing for a settled archive" $linesAfter $linesBefore
$after = (Get-Item -LiteralPath $archived).LastWriteTimeUtc
Check "  and the archived file is untouched" $after $before
$o3 = Get-LastOutcomes $cpState
Check "  and its last recorded state is still 'copied'" $o3["Sep26/run1.d"].verdict $V_COPIED

# A source that CHANGED after being archived is re-examined, not skipped on
# the strength of its name.
Add-Content -LiteralPath (Join-Path (Join-Path (Join-Path $cpSrc "Sep26") "run1.d") "analysis.tdf_bin.md5") -Value "changed"
& $PSHOME/pwsh -NoProfile -File $target @cpArgs 2>&1 | Out-Null
& $PSHOME/pwsh -NoProfile -File $target @cpArgs 2>&1 | Out-Null
$o4 = Get-LastOutcomes $cpState
Check "a changed source is re-examined, not skipped by name" $o4["Sep26/run1.d"].verdict $V_COPIED
Check "  and the log grew again" ((Count-RunLines $cpState "run1.d") -gt $linesAfter) $true
Check "a run still acquiring DOES append every pass" ((Count-RunLines $cpState "run2.d") -ge 4) $true
$cpCopied = Read-Map (Join-Path $cpState "copied.tsv")
Check "copied.tsv records the run"        $cpCopied.Count 1
Check "  keyed on the relative path"      ([string]::Join(",", @($cpCopied.Keys)).Replace("\\", "/")) "Sep26/run1.d"

# The source is never modified, renamed or deleted.
Check "the source run is still there"     (Test-Path -LiteralPath (Join-Path (Join-Path $cpSrc "Sep26") "run1.d")) $true
Check "  with every file"                 (@(Get-ChildItem -LiteralPath (Join-Path (Join-Path $cpSrc "Sep26") "run1.d") -Recurse -File -Force)).Count 5

# A copier that truncates must be CAUGHT, not recorded as success.
$badSrc   = Join-Path $sandbox "bad_src"
$badDst   = Join-Path $sandbox "bad_dst"
$badState = Join-Path $sandbox "bad_state"
New-Item -ItemType Directory -Path $badSrc -Force | Out-Null
New-Item -ItemType Directory -Path $badDst -Force | Out-Null
New-FixtureD (Join-Path $badSrc "wonky.d") -Frames 800 -BinBytes 80000 -CoverFrac 0.999 | Out-Null
$badShim = New-RoboShim (Join-Path $sandbox "roboshim_trunc.sh") -Truncate
$badArgs = @("-Source", $badSrc, "-Dest", $badDst, "-StateDir", $badState,
             "-SettleSeconds", "0", "-NoLockProbe", "-All", "-RobocopyExe", $badShim)
& $PSHOME/pwsh -NoProfile -File $target @badArgs 2>&1 | Out-Null
& $PSHOME/pwsh -NoProfile -File $target @badArgs 2>&1 | Out-Null
$ob = Get-LastOutcomes $badState
Check "a truncating copy is caught"       $ob["wonky.d"].verdict $V_VERIFYFAIL
CheckLike "  and says which file"         $ob["wonky.d"].reason "*analysis.tdf_bin*"
Check "  and is NOT recorded as archived" (Test-Path -LiteralPath (Join-Path $badState "copied.tsv")) $true
$badCopied = Read-Map (Join-Path $badState "copied.tsv")
Check "  copied.tsv stays empty"          $badCopied.Count 0
Check "  and the bad destination is left alone, not deleted" (Test-Path -LiteralPath (Join-Path $badDst "wonky.d")) $true

# A missing copier is refused up front rather than logged per run.
$noRoboState = Join-Path $sandbox "norobo_state"
& $PSHOME/pwsh -NoProfile -File $target -Source $cpSrc -Dest $cpDst -StateDir $noRoboState `
    -SettleSeconds 0 -NoLockProbe -All -RobocopyExe "definitely_not_a_copier" 2>&1 | Out-Null
Check "a missing copier writes no per-run failures" (Test-Path -LiteralPath (Join-Path $noRoboState "outcomes.jsonl")) $false
CheckLike "  and says so in the status"   (Get-Content -LiteralPath (Join-Path $noRoboState "status.txt") -Raw) "*NO COPIER*"

# ========================================================================
Write-Host ""
Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
if ($Failures -eq 0) { Write-Host "all tests passed"; exit 0 }
Write-Host "$Failures test(s) FAILED"
exit 1
