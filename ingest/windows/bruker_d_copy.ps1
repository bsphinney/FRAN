<#
.SYNOPSIS
    Archive finished Bruker .d acquisitions. Refuses to copy a run that is
    still being written, and never transports SQLite side files.

.DESCRIPTION
    THE DAMAGE THIS EXISTS TO PREVENT

    A Bruker .d holds analysis.tdf (a SQLite frame index) and analysis.tdf_bin
    (the spectra). While a run acquires, SQLite parks pending pages in
    analysis.tdf-wal beside the database.

    A copier that grabs the folder mid-acquisition lands a finished-LOOKING
    analysis.tdf at the destination with a STALE analysis.tdf-wal next to it.
    Nothing is wrong yet. Then some later program opens that database
    READ-WRITE, SQLite does what SQLite is supposed to do -- checkpoints the
    WAL into the main file -- and the index is truncated back to its
    mid-acquisition size. The frames are gone. analysis.tdf_bin still holds
    every spectrum, but nothing can address them any more.

    Measured on this cluster: 350 .d already destroyed (one has an index
    covering 0.74% of a 2.4 GB tdf_bin -- 1,451 frames over 133 s where the
    intact sibling has 13,736 frames over 21 minutes). 63 of them went in 77
    minutes to a job that only READ them with a bare sqlite3. DIA-NN then read
    one and reported 80 precursors against Spectronaut's 127,842, and exited 0.
    113 more .d are in the hazard state right now: intact index, stale 4.2-4.4
    MB -wal beside it, one read-write open from the same end.

    The copier is where the hazard is created, so this is where it stops.

    WHAT THIS SCRIPT DOES DIFFERENTLY

    1. A .d is not copied until its acquisition has demonstrably finished, and
       "demonstrably" means four independent signals that must ALL agree, not
       one heuristic (see Test-DReady).
    2. The SQLite side files are never transported. robocopy /XF drops them.
    3. The copy is verified afterwards against the source -- file for file, byte
       for byte -- and the destination index is re-checked for coverage. A copy
       that half-completed is reported, not logged as success.
    4. Nothing is ever deleted, on either side. A .d that fails the gate is
       skipped, reported, and looked at again on the next pass.
    5. Every outcome goes to a JSON Lines log a later job can consume.

    NO SQLITE DATABASE IS EVER OPENED IN PLACE.
    The coverage check needs to read analysis.tdf. Rather than open the
    instrument's file or the archive's file -- even read-only, even immutable --
    this copies the (megabyte-scale) index to a local scratch file and opens
    THAT, as file:...?mode=ro&immutable=1. The .d under inspection is only ever
    read as bytes. A copier that damaged what it inspected would be absurd.

.EXAMPLE
    # See what it would do, change nothing:
    .\bruker_d_copy.ps1 -Source D:\Data -Dest R:\Data\raw_data\tTOF_HT -DryRun -Show

.EXAMPLE
    # One manual pass, waiting out the settle interval in-process:
    .\bruker_d_copy.ps1 -Source D:\Data -Dest R:\Data\raw_data\tTOF_HT -Settle -Show

.EXAMPLE
    # What a scheduled task runs (stateful settling, no sleeping):
    .\bruker_d_copy.ps1 -Source D:\Data -Dest R:\Data\raw_data\tTOF_HT

.NOTES
    PowerShell 5.1 compatible -- instrument PCs have nothing newer. Per
    STAN/CLAUDE.md: no + concatenation, no inline ternary, no Where-Object
    pipelines, Join-Path for paths, no PS array passed where .NET wants
    string[]. Tests: test_bruker_d_copy.ps1 beside this file.

    This script does NOT install or schedule itself. See README.md for the
    schtasks line.
#>

param(
    # Root to scan for *.d, at any depth up to -MaxDepth.
    #
    # POSITIONAL, so this drops into the same shape as the robocopy one-liner
    # it replaces:
    #     robocopy      <src> <dst> /E /Z /FFT
    #     bruker_d_copy <src> <dst>
    [Parameter(Mandatory = $true, Position = 0)]
    [string] $Source,

    # Archive root. A run's path relative to -Source is preserved, so
    # D:\Data\Aug26\run.d lands at <Dest>\Aug26\run.d -- the same layout
    # robocopy /E produces. It ACCUMULATES: nothing at the destination is
    # ever removed.
    [Parameter(Mandatory = $true, Position = 1)]
    [string] $Dest,

    # How deep under -Source to look for *.d. The copier this replaces uses
    # /E, which copies the whole tree at any depth, so a shallow scan here
    # would silently archive nothing from a deeper layout.
    [int] $MaxDepth = 4,

    # How long a .d must sit completely unchanged before it is believed
    # finished. Default 10 minutes: long enough to ride out an LC
    # equilibration lull early in a run, which is the window in which a
    # size-stability check alone would archive a truncated acquisition.
    [int] $SettleSeconds = 600,

    # Take both stability probes in this process, sleeping -SettleSeconds
    # between them. For a manual run. A scheduled task should NOT use this:
    # without it the two probes are consecutive passes, and the process lives
    # for seconds rather than minutes.
    [switch] $Settle,

    # Ignore .d whose folder was last touched longer ago than this. Keeps a
    # scheduled pass cheap on a source with years of runs in it.
    [int] $LookbackHours = 168,

    # Consider every .d regardless of age.
    [switch] $All,

    # Restrict the pass to these runs -- a bare folder name ("run.d") or a
    # path relative to -Source ("Aug26\run.d"). Anything else is ignored.
    # This is what makes a staged test against a handful of REAL runs
    # possible without pointing the copier at a whole instrument drive.
    [string[]] $Only = @(),

    # robocopy /MT. 4 threads gets most of the many-small-files win on a .d
    # without pinning a shared SMB mount. Set 0 to leave /MT off entirely.
    [int] $Threads = 4,

    # robocopy /Z, restartable mode. OFF by default -- see Get-RobocopyArgs
    # for the argument. Turn it on only for a link so unreliable that a
    # whole-file re-copy never completes.
    [switch] $Restartable,

    # robocopy /IPG, milliseconds between packets. Off by default. Set it
    # (20 is the value the old copier used) when this runs ON the acquiring
    # PC, to hand bandwidth back to the instrument. MUTUALLY EXCLUSIVE with
    # /MT -- robocopy rejects the pair -- so setting it drops -Threads.
    [int] $InterPacketGapMs = 0,

    # The fraction of analysis.tdf_bin that the frame index must address
    # before a .d is believed complete. The last frame's offset sits at
    # (n-1)/n of the binary, so an intact run scores ~0.999; the damaged
    # example above scores 0.0074. 0.90 leaves room for trailing padding.
    [double] $MinCoverage = 0.90,

    # sqlite3.exe. Found on PATH if not given. Without it the coverage check
    # is recorded as unavailable -- never silently passed.
    [string] $Sqlite3 = "",

    # Skip the coverage check even if sqlite3 is available.
    [switch] $NoCoverage,

    # Skip the exclusive-open probe on analysis.tdf_bin.
    [switch] $NoLockProbe,

    # Never copy an analysis.tdf larger than this to scratch for the
    # coverage check.
    [int] $MaxTdfCopyMB = 512,

    # Run the gate and log every verdict, but copy nothing.
    [switch] $DryRun,

    # The copier itself. Named rather than hard-coded so a pass can be run
    # against a stand-in and the copy-verify-record path actually tested --
    # and so a node with robocopy somewhere unusual can be pointed at it.
    [string] $RobocopyExe = "robocopy.exe",

    # State, logs and scratch. Defaults under %ProgramData%\FRAN.
    [string] $StateDir = "",

    # Re-examine runs already recorded as copied and verified.
    [switch] $Rescan,

    # Echo the log to the console.
    [switch] $Show
)

$ErrorActionPreference = "Continue"

$ScriptVersion = "1.0.0"
$OnWindows = ($env:OS -eq "Windows_NT")

# Verdicts. Every .d examined in a pass produces exactly one of these, in
# the JSON Lines log. Anything consuming that log can switch on this set.
$V_COPIED      = "copied"                  # gate passed, copied, verified
$V_ALREADY     = "skipped-already-copied"  # verified earlier, source unchanged
$V_ACQUIRING   = "skipped-still-acquiring" # tree changed, or the binary is held open
$V_SETTLING    = "skipped-settling"        # stable, but not yet for long enough
$V_HAZARD      = "skipped-hazard"          # live SQLite side file at the SOURCE
$V_INCOMPLETE  = "skipped-incomplete"      # malformed, or the index does not cover the binary
$V_DESTHAZARD  = "blocked-dest-hazard"     # the ARCHIVE copy already carries a side file
$V_COPYFAIL    = "copy-failed"             # robocopy returned a real failure
$V_VERIFYFAIL  = "verify-failed"           # copied, but source and destination disagree
$V_DRYRUN      = "dry-run-would-copy"

# ---------------------------------------------------------------- paths --

if (-not $StateDir) {
    $base = $env:ProgramData
    if (-not $base) { $base = $env:USERPROFILE }
    if (-not $base) { $base = [System.IO.Path]::GetTempPath() }
    $StateDir = Join-Path (Join-Path $base "FRAN") "bruker_d_copy"
}
$ScratchDir = Join-Path $StateDir "scratch"
New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Path $ScratchDir -Force -ErrorAction SilentlyContinue | Out-Null

$LogFile     = Join-Path $StateDir "bruker_d_copy.log"        # for a human
$OutcomeFile = Join-Path $StateDir "outcomes.jsonl"           # for a program
$StatusFile  = Join-Path $StateDir "status.txt"               # proof of life
$ProbeFile   = Join-Path $StateDir "probes.tsv"               # settling state
$CopiedFile  = Join-Path $StateDir "copied.tsv"               # verified archive
$RoboLog     = Join-Path $StateDir "robocopy.log"
$LockFile    = Join-Path $StateDir "pass.lock"

function Get-Stamp { return (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }

function Write-Log($Message) {
    $line = "$(Get-Stamp) $Message"
    try { Add-Content -LiteralPath $LogFile -Value $line -ErrorAction SilentlyContinue } catch {}
    if ($Show) { Write-Host $line }
}

function Set-Status($Message) {
    # Overwritten every pass. Its mtime is the only evidence that the task is
    # still running when the log has been quiet for a week.
    try {
        Set-Content -LiteralPath $StatusFile -Value "$(Get-Stamp)  $Message" -ErrorAction SilentlyContinue
    } catch {}
}

function Write-Outcome($Record) {
    # One JSON object per line. Deliberately not a single JSON array: a pass
    # that dies halfway still leaves every line before it parseable, and a
    # consumer can tail the file instead of re-reading it.
    try {
        $json = ($Record | ConvertTo-Json -Compress -Depth 4)
        Add-Content -LiteralPath $OutcomeFile -Value $json -ErrorAction SilentlyContinue
    } catch {}
}

function New-Outcome($Rel, $Verdict, $Reason) {
    return [ordered]@{
        ts         = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        host       = $env:COMPUTERNAME
        version    = $ScriptVersion
        run        = $Rel
        source     = ""
        dest       = ""
        verdict    = $Verdict
        reason     = $Reason
        files      = 0
        bytes      = 0
        sig        = ""
        frames     = -1
        timeSpanS  = -1
        coverage   = -1
        destFrames = -1
        roboExit   = -1
        seconds    = 0
    }
}

# --------------------------------------------------------- tsv key/value --

function Read-Map($Path) {
    # key<TAB>value. Returned whole: PowerShell unrolls a list on return but
    # not a Hashtable, so this is the one shape that survives.
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        $split = $line.IndexOf("`t")
        if ($split -gt 0) { $map[$line.Substring(0, $split)] = $line.Substring($split + 1) }
    }
    return $map
}

function Write-Map($Path, $Map) {
    $lines = @()
    foreach ($key in $Map.Keys) { $lines += "$key`t$($Map[$key])" }
    try { Set-Content -LiteralPath $Path -Value $lines -ErrorAction SilentlyContinue } catch {}
}

# ------------------------------------------------------- looking at a .d --

function Get-RelPath($Root, $Full) {
    $r = $Root.TrimEnd("\", "/")
    if ($Full.Length -le $r.Length) { return (Split-Path -Leaf $Full) }
    return $Full.Substring($r.Length).TrimStart("\", "/")
}

function Get-SideFile($Root) {
    # Every SQLite companion anywhere in the tree, not just beside
    # analysis.tdf. A timsTOF .d also carries chromatography-data.sqlite, and
    # that has a -wal of its own; an older .d may carry a rollback -journal
    # instead. Matching on the suffix catches all of them, including formats
    # this script has not met yet.
    $out = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n.EndsWith("-wal") -or $n.EndsWith("-shm") -or $n.EndsWith("-journal")) {
            $out += $f
        }
    }
    return $out
}

function Test-SideFileHazard($Root) {
    # "" when the .d is clean, otherwise the reason it is not.
    #
    # A zero-byte -wal is allowed: it holds no pending frames, so a later
    # checkpoint has nothing to write back and nothing to truncate. A -wal
    # with ANY content is the hazard itself. A -shm or a -journal means a
    # connection is, or recently was, open -- there is no benign size for
    # either, so their mere presence is refused.
    foreach ($f in @(Get-SideFile $Root)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n.EndsWith("-wal")) {
            if ($f.Length -gt 0) { return "live -wal: $($f.Name) is $($f.Length) bytes" }
            continue
        }
        return "open-connection marker present: $($f.Name)"
    }
    return ""
}

function Get-TreeProbe($Root) {
    # A fingerprint of the whole tree: every file's relative path, length and
    # last-write time. Two probes that differ mean SOMETHING moved.
    #
    # The old copier fingerprinted only "file count / total bytes", which two
    # genuinely different states can share -- most plausibly when SQLite
    # checkpoints the WAL into the tdf and the bytes move from one file to
    # the other. Including per-file timestamps closes that.
    #
    # The side files are deliberately INCLUDED here. A growing -wal is itself
    # evidence of an acquisition in progress, and we want that to count.
    $items = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = 0
    $lines = @()
    foreach ($f in $items) {
        $rel = Get-RelPath $Root $f.FullName
        $bytes += $f.Length
        $lines += "$rel|$($f.Length)|$($f.LastWriteTimeUtc.Ticks)"
    }
    # Directory order is not guaranteed stable between passes, so sort before
    # hashing. [array]::Sort avoids a pipeline and takes the cast explicitly,
    # which is what .NET wants here.
    $sorted = [string[]] $lines
    [array]::Sort($sorted)
    $joined = [string]::Join("`n", $sorted)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $raw = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($joined))
    $sha.Dispose()
    $hex = ([System.BitConverter]::ToString($raw)).Replace("-", "").Substring(0, 16).ToLowerInvariant()
    return [PSCustomObject]@{
        Files = $items.Count
        Bytes = $bytes
        Sig   = "$($items.Count)/$bytes/$hex"
    }
}

function Test-BinLocked($Root) {
    # Does anything still hold analysis.tdf_bin open?
    #
    # timsControl keeps a handle on the binary for the whole acquisition, so
    # an exclusive open either fails -- someone has it, the run is live -- or
    # succeeds because nobody does. We ask for Read access with FileShare
    # None: no byte is written, and the handle is released immediately. We
    # never hold a lock across a live run, because during one we never get
    # the handle at all.
    #
    # A wash or an aborted method can finish without ever writing a binary,
    # so a MISSING analysis.tdf_bin is not read as "still writing" -- those
    # fall to the stability probe alone.
    $bin = Join-Path $Root "analysis.tdf_bin"
    if (-not (Test-Path -LiteralPath $bin)) { return $false }
    try {
        $handle = [System.IO.File]::Open($bin, "Open", "Read", "None")
        $handle.Close()
        $handle.Dispose()
        return $false
    } catch {
        return $true
    }
}

# ---------------------------------------------------------------- sqlite --

function ConvertTo-SqliteUri($Path) {
    # file:///C:/x/analysis.tdf?mode=ro&immutable=1
    #
    # immutable=1 is not decoration. It tells SQLite the file cannot change
    # underneath it, so SQLite skips the WAL and the shared-memory index
    # entirely and reads the main database as it sits on disk. That is both
    # the honest answer -- the real index, not a WAL-shadowed view of it --
    # and a guarantee that not one byte is written. A plain mode=ro open
    # still consults the WAL and can still try to build a -shm; read-write is
    # the defect this whole script exists to eliminate.
    $p = $Path.Replace("\", "/")
    $p = $p.Replace("%", "%25")     # first, or it would double-encode the rest
    $p = $p.Replace("#", "%23")
    $p = $p.Replace("?", "%3f")
    if ($p -match "^[A-Za-z]:/") { $p = "/$p" }
    return "file://$p`?mode=ro&immutable=1"
}

function Invoke-Sqlite($Exe, $Uri, $Sql) {
    # Returns the raw stdout lines, or $null on any failure. Never retries
    # with a bare path: falling back from a URI open to a filename open would
    # silently reintroduce the read-write hazard.
    try {
        $out = & $Exe $Uri $Sql 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        # Joined into ONE string on purpose. PowerShell unrolls a collection
        # on return, so a single-row result would come back as a bare string
        # and the caller's $rows[0] would index its first CHARACTER rather
        # than its first row. A string cannot be unrolled, so it survives.
        $lines = @()
        foreach ($line in @($out)) { $lines += "$line" }
        return [string]::Join("`n", $lines)
    } catch {
        return $null
    }
}

function Split-SqliteLine($Text) {
    # WRAP EVERY CALL IN @(). A one-row result unrolls to a bare string on
    # return, and a bare string answers .Count with 1 and [0] with its first
    # character -- indistinguishable from a real single-row result until the
    # number it yields is wrong.
    if ($null -eq $Text) { return @() }
    $out = @()
    foreach ($line in @(("$Text").Split("`n"))) {
        if (("$line").Trim()) { $out += ("$line").Trim() }
    }
    return $out
}

function Initialize-Sqlite {
    # sqlite3.exe, but only if it is proven to honour URI filenames.
    #
    # With SQLITE_USE_URI off, "file:...?mode=ro&immutable=1" is taken as a
    # LITERAL filename: sqlite3 cheerfully creates a new empty database with
    # that name and reports nothing wrong. Every coverage check would then
    # pass on an empty database. So the support is proved against a scratch
    # database whose content is known before it is trusted -- and if the
    # proof fails, the coverage check is recorded as unavailable rather than
    # downgraded to an unsafe open.
    $exe = $Sqlite3
    if (-not $exe) {
        $cmd = Get-Command "sqlite3" -ErrorAction SilentlyContinue
        if ($cmd) { $exe = $cmd.Source }
    }
    if (-not $exe) { return "" }
    if (-not (Test-Path -LiteralPath $exe)) {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $cmd) { return "" }
        $exe = $cmd.Source
    }

    $probe = Join-Path $ScratchDir "uri_probe_$([System.Guid]::NewGuid().ToString('N')).db"
    try {
        & $exe $probe "create table t(a); insert into t values(4242);" 2>&1 | Out-Null
        if (-not (Test-Path -LiteralPath $probe)) { return "" }
        $uri = ConvertTo-SqliteUri $probe
        $got = Invoke-Sqlite $exe $uri "select a from t;"
        if ($null -eq $got) { return "" }
        if (("$got").Trim() -ne "4242") { return "" }
        return $exe
    } catch {
        return ""
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

function Get-TdfInfo($DPath, $Exe) {
    # Frame count, acquisition time span, and how much of analysis.tdf_bin the
    # index actually addresses.
    #
    # THE INDEX IS COPIED TO SCRATCH AND THE COPY IS WHAT SQLITE OPENS. The
    # instrument's file and the archive's file are only ever read as bytes.
    # This costs a few MB of I/O -- the index is megabytes, the binary is
    # gigabytes and we only stat it -- and buys an absolute guarantee that
    # inspection cannot damage what it inspects. It also sidesteps SQLite's
    # refusal to accept a UNC authority in a file: URI, which is what an
    # archive path usually is.
    #
    # Coverage = MAX(Frames.TimsId) / size(analysis.tdf_bin). TimsId is the
    # byte offset of a frame's record in the binary, so the last frame starts
    # at (n-1)/n of the file: an intact run scores ~0.999. The damaged .d
    # this script exists to prevent scores 0.0074.
    $result = [PSCustomObject]@{
        Ok = $false; Frames = -1; TimeSpan = -1; Coverage = -1; Error = ""
    }
    $tdf = Join-Path $DPath "analysis.tdf"
    $bin = Join-Path $DPath "analysis.tdf_bin"
    if (-not (Test-Path -LiteralPath $tdf)) { $result.Error = "no analysis.tdf"; return $result }
    if (-not $Exe) { $result.Error = "sqlite3 unavailable"; return $result }

    $tdfItem = Get-Item -LiteralPath $tdf -ErrorAction SilentlyContinue
    if (-not $tdfItem) { $result.Error = "analysis.tdf unreadable"; return $result }
    if ($tdfItem.Length -gt ($MaxTdfCopyMB * 1MB)) {
        $result.Error = "analysis.tdf is $([math]::Round($tdfItem.Length / 1MB)) MB, over -MaxTdfCopyMB"
        return $result
    }

    $scratch = Join-Path $ScratchDir "tdf_$([System.Guid]::NewGuid().ToString('N')).tdf"
    try {
        Copy-Item -LiteralPath $tdf -Destination $scratch -Force -ErrorAction Stop
        $uri = ConvertTo-SqliteUri $scratch

        # Ask the schema before assuming it. A .d whose Frames table has no
        # TimsId is a format this check does not understand, and saying so is
        # worth more than a confident number derived from a guess.
        # PRAGMA table_info, not the pragma_table_info() table-valued
        # function: the latter needs SQLite 3.16+, and sqlite3.exe on an
        # instrument PC is whatever someone dropped there years ago.
        $colText = Invoke-Sqlite $Exe $uri "PRAGMA table_info(Frames);"
        if ($null -eq $colText) { $result.Error = "cannot read Frames schema"; return $result }
        $cols = @(Split-SqliteLine $colText)
        if ($cols.Count -eq 0) { $result.Error = "no Frames table"; return $result }
        $hasTimsId = $false
        $hasTime = $false
        foreach ($c in $cols) {
            # cid|name|type|notnull|dflt_value|pk
            $bits = ("$c").Split("|")
            if ($bits.Count -lt 2) { continue }
            if ($bits[1] -eq "TimsId") { $hasTimsId = $true }
            if ($bits[1] -eq "Time") { $hasTime = $true }
        }
        if (-not $hasTimsId) { $result.Error = "Frames has no TimsId column"; return $result }

        $sql = "select count(*), ifnull(max(TimsId),0) from Frames;"
        if ($hasTime) { $sql = "select count(*), ifnull(max(TimsId),0), ifnull(max(Time)-min(Time),-1) from Frames;" }
        $rowText = Invoke-Sqlite $Exe $uri $sql
        $rows = @(Split-SqliteLine $rowText)
        if ($rows.Count -lt 1) { $result.Error = "Frames query failed"; return $result }
        $parts = ("$($rows[0])").Split("|")
        if ($parts.Count -lt 2) { $result.Error = "unexpected Frames result"; return $result }

        $result.Frames = [int64] $parts[0]
        $maxTimsId = [int64] $parts[1]
        if ($parts.Count -ge 3) { $result.TimeSpan = [math]::Round([double] $parts[2], 1) }

        if (Test-Path -LiteralPath $bin) {
            $binLen = (Get-Item -LiteralPath $bin).Length
            if ($binLen -gt 0) {
                $result.Coverage = [math]::Round(($maxTimsId / [double] $binLen), 5)
            } else {
                # No spectra at all. Nothing for the index to cover, and
                # nothing worth archiving either.
                $result.Coverage = 0
            }
        } else {
            # No binary: a wash or an aborted method. Coverage is not a
            # meaningful question, so it is left at -1 and the caller decides.
            $result.Coverage = -1
        }
        $result.Ok = $true
        return $result
    } catch {
        $result.Error = "$($_.Exception.Message)"
        return $result
    } finally {
        Remove-Item -LiteralPath $scratch -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------- the gate --

function Test-DReady($DPath, $Rel, $Probe, $Probes, $Now, $Exe) {
    # The whole point of the script. Returns a verdict plus the evidence.
    #
    # Ordered cheapest-and-most-decisive first, and every check is fail-CLOSED:
    # anything it cannot establish is a refusal, never a pass.
    $r = [PSCustomObject]@{
        Verdict = $V_ACQUIRING; Reason = ""
        Frames = -1; TimeSpan = -1; Coverage = -1
    }

    # 1. Is this even a .d yet? An empty or half-created folder is not
    #    "acquiring", it is "not a run", and copying it would put a shell in
    #    the archive that looks like a real acquisition to everything
    #    downstream.
    $tdf = Join-Path $DPath "analysis.tdf"
    $baf = Join-Path $DPath "analysis.baf"
    $hasTdf = Test-Path -LiteralPath $tdf
    $hasBaf = Test-Path -LiteralPath $baf
    if (-not $hasTdf -and -not $hasBaf) {
        $r.Verdict = $V_INCOMPLETE
        $r.Reason = "no analysis.tdf and no analysis.baf"
        return $r
    }
    if ($hasTdf) {
        $tdfItem = Get-Item -LiteralPath $tdf -ErrorAction SilentlyContinue
        if (-not $tdfItem -or $tdfItem.Length -eq 0) {
            $r.Verdict = $V_INCOMPLETE
            $r.Reason = "analysis.tdf is empty"
            return $r
        }
    }

    # 2. The hazard itself. A live -wal, or a -shm or -journal of any size,
    #    means a SQLite connection is open or was not closed. Refuse before
    #    anything else touches the folder.
    $hazard = Test-SideFileHazard $DPath
    if ($hazard) {
        $r.Verdict = $V_HAZARD
        $r.Reason = $hazard
        return $r
    }

    # 3. Stability. Every file in the tree -- path, length, mtime -- identical
    #    across two probes at least -SettleSeconds apart.
    #
    #    "At least -SettleSeconds apart" is load-bearing and is what the old
    #    copier was missing: it compared this pass with whatever the previous
    #    pass had recorded, without ever checking WHEN that was. Two passes 20
    #    seconds apart after a task restart agreed, and the run was copied.
    $prior = $Probes[$Rel]
    if (-not $prior) {
        $r.Verdict = $V_SETTLING
        $r.Reason = "first sighting, settling for $SettleSeconds s"
        return $r
    }
    $split = ("$prior").IndexOf("|")
    if ($split -lt 1) {
        $r.Verdict = $V_SETTLING
        $r.Reason = "unreadable prior probe, restarting the settle"
        return $r
    }
    $priorAt = [int64] ("$prior").Substring(0, $split)
    $priorSig = ("$prior").Substring($split + 1)
    if ($priorSig -ne $Probe.Sig) {
        $r.Verdict = $V_ACQUIRING
        $r.Reason = "tree changed since the last probe"
        return $r
    }
    $elapsed = $Now - $priorAt
    if ($elapsed -lt $SettleSeconds) {
        $r.Verdict = $V_SETTLING
        $r.Reason = "stable for $elapsed s of $SettleSeconds s"
        return $r
    }

    # 4. Does anything still hold the spectra file open? An OS-level fact,
    #    not an inference about Bruker's format.
    if (-not $NoLockProbe) {
        if (Test-BinLocked $DPath) {
            $r.Verdict = $V_ACQUIRING
            $r.Reason = "analysis.tdf_bin is held open"
            return $r
        }
    }

    # 5. The strongest check: does the frame index actually cover the spectra?
    #    A .d truncated by an earlier bad copy passes all of the above -- it is
    #    stable, clean and closed -- and fails only here.
    if ($hasTdf -and -not $NoCoverage) {
        $info = Get-TdfInfo $DPath $Exe
        if (-not $info.Ok) {
            if ($info.Error -eq "sqlite3 unavailable") {
                # Recorded, not silently passed. The other four checks stand.
                $r.Reason = "coverage unavailable: sqlite3 not found"
            } else {
                $r.Verdict = $V_INCOMPLETE
                $r.Reason = "coverage check failed: $($info.Error)"
                return $r
            }
        } else {
            $r.Frames = $info.Frames
            $r.TimeSpan = $info.TimeSpan
            $r.Coverage = $info.Coverage
            if ($info.Frames -le 0) {
                $r.Verdict = $V_INCOMPLETE
                $r.Reason = "the frame index is empty"
                return $r
            }
            if ($info.Coverage -gt 1.0) {
                # MAX(TimsId) is the START offset of the last frame, so on an
                # intact .d it is always below the binary's length. Above it
                # means analysis.tdf_bin is short -- the same damage from the
                # other side, and just as unusable.
                $r.Verdict = $V_INCOMPLETE
                $r.Reason = "the index addresses past the end of analysis.tdf_bin - the binary is truncated"
                return $r
            }
            if ($info.Coverage -ge 0 -and $info.Coverage -lt $MinCoverage) {
                $r.Verdict = $V_INCOMPLETE
                $r.Reason = "index covers $([math]::Round($info.Coverage * 100, 2))% of analysis.tdf_bin, under $([math]::Round($MinCoverage * 100, 2))%"
                return $r
            }
        }
    }

    $r.Verdict = "ready"
    return $r
}

# ---------------------------------------------------------------- copy ---

function Get-RobocopyArgs($From, $To) {
    # Every flag here is a decision. See README.md for the argument in full.
    $a = @()
    $a += "`"$($From.TrimEnd('\'))`""
    $a += "`"$($To.TrimEnd('\'))`""

    $a += "/E"             # subdirectories, including empty ones: a .d has
                           # them, and /S would silently change its shape.
    $a += "/COPY:DAT"      # data, attributes, timestamps. Stated rather than
                           # left to the default so nobody later adds /COPYALL,
                           # which tries for ACLs and owner and fails on a
                           # share where we do not hold those rights.
    $a += "/DCOPY:DAT"     # the same for the .d FOLDER itself. Without it the
                           # archived run's mtime becomes the copy date and
                           # every downstream "when was this acquired" answer
                           # is wrong.
    $a += "/FFT"           # 2-second timestamp granularity. An SMB or NFS
                           # destination does not keep NTFS's resolution, so
                           # without this robocopy believes every file differs
                           # and re-copies the whole archive forever.
    # /Z IS DELIBERATELY ABSENT. The copier this replaces uses it, and it is
    # the wrong default here.
    #
    # Restartable mode resumes a partially copied file from a recorded
    # offset. Whether that is safe rests entirely on robocopy noticing that
    # the source changed since the interrupted attempt -- a size and
    # timestamp comparison, which /FFT deliberately coarsens to 2 seconds. A
    # file that grew and was then resumed from the old offset produces a
    # destination that is two points in time stitched together, and nothing
    # downstream can tell. That is the same class of defect as the stale WAL.
    #
    # And we do not need it. A pass that dies leaves a short file; the next
    # pass compares sizes, sees the difference, and copies the whole file
    # again, while verification reports verify-failed in the meantime. So the
    # benefit /Z buys -- not re-sending a large file -- we already get from
    # being idempotent, without depending on a comparison we just blunted.
    # -Restartable puts it back for a link where a whole-file re-copy never
    # completes; the hazard above is then real but bounded by the gate, which
    # has already established the source is not changing.
    if ($Restartable) { $a += "/Z" }

    $a += "/R:2"           # 2 retries, not the default 1,000,000...
    $a += "/W:10"          # ...at 10 s, not the default 30. The defaults wedge
                           # a pass for ~347 days on one locked file -- and a
                           # file the instrument still holds open is exactly
                           # what this will hit. 2x10 s bounds a failure to
                           # about 20 s and lets the next pass retry the run.

    # The belt to the gate's braces. The gate should already have refused any
    # .d carrying these; if one somehow arrives, it still must not travel.
    $a += "/XF"
    $a += "analysis.tdf-wal"
    $a += "analysis.tdf-shm"
    $a += "analysis.tdf-journal"
    $a += "*-wal"
    $a += "*-shm"
    $a += "*-journal"

    # /MT and /IPG are mutually exclusive -- robocopy rejects the pair.
    if ($InterPacketGapMs -gt 0) {
        $a += "/IPG:$InterPacketGapMs"
    } elseif ($Threads -gt 0) {
        $a += "/MT:$Threads"
    }

    $a += "/NP"            # no per-file percentage: in a log file that is
                           # megabytes of carriage returns.
    $a += "/NFL"           # no file list, no directory list. Verification
    $a += "/NDL"           # walks both trees itself, which is stronger than
                           # trusting robocopy's account of its own work.
    $a += "/TS"            # timestamps on what it does print.
    $a += "/LOG+:`"$RoboLog`""

    # Deliberately absent, and they must stay absent:
    #   /MIR, /PURGE  delete destination files missing from the source. On a
    #                 copier whose entire purpose is not destroying archives,
    #                 that is a loaded gun pointed at the archive.
    #   /MOV, /MOVE   delete the source.
    #   /B, /ZB       backup mode; needs SeBackupPrivilege and reads past ACLs.
    return $a
}

function Copy-D($From, $To) {
    # Returns the robocopy exit code, or -1 if it could not be started.
    $parent = Split-Path -Parent $To
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force -ErrorAction SilentlyContinue | Out-Null
    }
    $roboArgs = Get-RobocopyArgs $From $To
    try {
        $spawn = @{
            FilePath = $RobocopyExe; ArgumentList = $roboArgs
            PassThru = $true; ErrorAction = "Stop"
        }
        # -WindowStyle is a Windows-only parameter and throws elsewhere.
        if ($OnWindows) { $spawn["WindowStyle"] = "Hidden" }
        $proc = Start-Process @spawn
        # Below normal so a copy never competes with an acquisition for CPU.
        try { $proc.PriorityClass = "BelowNormal" } catch {}
        $proc.WaitForExit()
        return $proc.ExitCode
    } catch {
        Write-Log "  robocopy could not start: $($_.Exception.Message)"
        return -1
    }
}

# -------------------------------------------------------------- verify ---

function Get-FileMap($Root) {
    # relative path (lowercased, separators normalised) -> length, with the
    # side files left out so the two sides are comparable: the source may
    # legitimately hold a zero-byte -wal that the destination must not.
    $map = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $n = $f.Name.ToLowerInvariant()
        if ($n.EndsWith("-wal") -or $n.EndsWith("-shm") -or $n.EndsWith("-journal")) { continue }
        $rel = (Get-RelPath $Root $f.FullName).Replace("\", "/").ToLowerInvariant()
        $map[$rel] = $f.Length
    }
    return $map
}

function Test-CopyVerified($From, $To, $SourceInfo, $Exe) {
    # A copy is only finished when the destination has been looked at. robocopy
    # returning 0 means robocopy believes it succeeded; it is not evidence
    # about what is on the archive.
    $r = [PSCustomObject]@{ Ok = $false; Reason = ""; DestFrames = -1; DestCoverage = -1 }

    if (-not (Test-Path -LiteralPath $To -PathType Container)) {
        $r.Reason = "destination folder is not there"
        return $r
    }

    # 1. Nothing hazardous arrived. If this ever fires, /XF has been defeated
    #    or something else is writing into the archive.
    $destHazard = Test-SideFileHazard $To
    if ($destHazard) { $r.Reason = "side file at the destination: $destHazard"; return $r }
    if (@(Get-SideFile $To).Count -gt 0) { $r.Reason = "a SQLite side file reached the destination"; return $r }

    # 2. Every source file present, at the same size. A truncated transfer
    #    differs in length, which is exactly what this catches -- and, because
    #    robocopy compares size and timestamp, what the next pass then repairs
    #    by itself.
    $srcMap = Get-FileMap $From
    $dstMap = Get-FileMap $To
    $missing = 0
    $short = 0
    $firstBad = ""
    foreach ($key in $srcMap.Keys) {
        if (-not $dstMap.ContainsKey($key)) {
            $missing += 1
            if (-not $firstBad) { $firstBad = "missing $key" }
            continue
        }
        if ($dstMap[$key] -ne $srcMap[$key]) {
            $short += 1
            if (-not $firstBad) { $firstBad = "$key is $($dstMap[$key]) bytes, source has $($srcMap[$key])" }
        }
    }
    if ($missing -gt 0 -or $short -gt 0) {
        $r.Reason = "$missing missing, $short wrong size ($firstBad)"
        return $r
    }
    if ($dstMap.Count -ne $srcMap.Count) {
        $r.Reason = "destination has $($dstMap.Count) files, source has $($srcMap.Count)"
        return $r
    }

    # 3. The index at the destination still covers the binary, and still
    #    describes the same run. Size equality already implies this, so it is
    #    a cross-check on a different axis rather than a repeat -- it would
    #    catch a destination file that is the right length and the wrong
    #    content.
    if ($Exe -and -not $NoCoverage) {
        $destInfo = Get-TdfInfo $To $Exe
        if ($destInfo.Ok) {
            $r.DestFrames = $destInfo.Frames
            $r.DestCoverage = $destInfo.Coverage
            if ($destInfo.Coverage -ge 0 -and $destInfo.Coverage -lt $MinCoverage) {
                $r.Reason = "destination index covers only $([math]::Round($destInfo.Coverage * 100, 2))%"
                return $r
            }
            if ($SourceInfo -and $SourceInfo.Frames -ge 0 -and $destInfo.Frames -ne $SourceInfo.Frames) {
                $r.Reason = "destination has $($destInfo.Frames) frames, source has $($SourceInfo.Frames)"
                return $r
            }
        }
    }

    $r.Ok = $true
    return $r
}

# ----------------------------------------------------------- candidates --

function Get-Candidate {
    # Every *.d under the scan root, to -MaxDepth. Breadth-first rather than
    # a fixed two levels: the copier this replaces uses /E, so a .d can sit at
    # any depth and a shallow scan would silently archive nothing from a
    # deeper layout. timsControl acquires into month folders, but neither the
    # instrument nor the operator is obliged to stop there.
    #
    # We never descend INTO a .d: it is the unit we are looking for, and it is
    # full of files.
    $out = @()
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return $out }
    $cutoff = (Get-Date).AddHours(-$LookbackHours)
    $frontier = @((Get-Item -LiteralPath $Source))
    $depth = 0
    while ($frontier.Count -gt 0 -and $depth -lt $MaxDepth) {
        $next = @()
        foreach ($dir in $frontier) {
            foreach ($child in @(Get-ChildItem -LiteralPath $dir.FullName -Directory -Force -ErrorAction SilentlyContinue)) {
                if ($child.Extension -eq ".d") {
                    $out += $child
                    continue
                }
                $next += $child
            }
        }
        $frontier = $next
        $depth += 1
    }
    if ($Only.Count -gt 0) {
        $wanted = @()
        foreach ($d in $out) {
            $rel = (Get-RelPath $Source $d.FullName).Replace("\", "/")
            foreach ($o in $Only) {
                $want = ("$o").Replace("\", "/").Trim("/")
                # -eq on strings is case-insensitive, which Windows needs.
                if ($rel -eq $want -or $d.Name -eq $want) {
                    $wanted += $d
                    break
                }
            }
        }
        # An explicit list overrides the age filter: naming a run is a
        # stronger statement of intent than its mtime.
        return $wanted
    }
    if ($All) { return $out }
    $recent = @()
    foreach ($d in $out) {
        # A .d directory's own mtime does not move when a file inside it
        # grows, so the folder stamp alone would drop a long acquisition out
        # of the lookback. The files that DO grow -- analysis.tdf,
        # analysis.tdf_bin -- sit at the .d root, so a shallow listing is
        # enough and costs one directory read rather than a recursive walk of
        # every candidate on every pass.
        #
        # This is an age filter and nothing more. It is never a completeness
        # signal: that is what the probe and the rest of the gate are for.
        $newest = $d.LastWriteTime
        foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -File -Force -ErrorAction SilentlyContinue)) {
            if ($f.LastWriteTime -gt $newest) { $newest = $f.LastWriteTime }
        }
        if ($newest -ge $cutoff) { $recent += $d }
    }
    return $recent
}

# ------------------------------------------------------------ one pass ---

function Invoke-Pass {
    $started = Get-Date
    $now = [int64] ([datetime]::UtcNow - (Get-Date "1970-01-01 00:00:00Z").ToUniversalTime()).TotalSeconds

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        Set-Status "NO SOURCE - $Source is not reachable"
        Write-Log "source $Source is not reachable, nothing examined"
        return 1
    }
    if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
        # Refuse rather than create it. A destination that has vanished is far
        # more likely to be an unmounted share than a folder we should invent,
        # and inventing it archives runs into a local directory nobody reads.
        Set-Status "NO DESTINATION - $Dest is not reachable"
        Write-Log "destination $Dest is not reachable, nothing copied"
        return 1
    }

    $exe = ""
    if (-not $NoCoverage) {
        $exe = Initialize-Sqlite
        if (-not $exe) {
            Write-Log "sqlite3 with URI filename support was not found - the index-coverage check is OFF for this pass"
        }
    }

    $probes = Read-Map $ProbeFile
    $copied = Read-Map $CopiedFile
    $nextProbes = @{}

    $nCopied = 0; $nSkipped = 0; $nFailed = 0; $nAlready = 0

    foreach ($d in @(Get-Candidate)) {
        $rel = Get-RelPath $Source $d.FullName
        $target = Join-Path $Dest $rel
        $t0 = Get-Date
        $rec = New-Outcome $rel $V_ACQUIRING ""
        $rec.source = $d.FullName
        $rec.dest = $target

        # One tree walk per candidate per pass. Taken here, before any exit
        # path, because the settle clock lives in this probe: a branch that
        # returns without writing it back drops the entry from probes.tsv and
        # the run restarts at "first sighting" on the next pass, forever.
        $probe = Get-TreeProbe $d.FullName
        $rec.files = $probe.Files
        $rec.bytes = $probe.Bytes
        $rec.sig = $probe.Sig
        $nextProbes[$rel] = "$now|$($probe.Sig)"
        $prior = $probes[$rel]
        if ($prior) {
            # Carry the ORIGINAL time this signature was first seen. Stamping
            # it with "now" every pass would restart the settle clock each
            # time and nothing would ever age past it.
            $split = ("$prior").IndexOf("|")
            if ($split -gt 0 -and ("$prior").Substring($split + 1) -eq $probe.Sig) {
                $nextProbes[$rel] = $prior
            }
        }

        # Already archived and verified, and the source has not moved since.
        # Keyed on the signature rather than the name, so a run re-acquired
        # under a name that is already in the archive is not silently skipped.
        if (-not $Rescan -and $copied.ContainsKey($rel)) {
            if ($copied[$rel] -eq $probe.Sig) {
                # Deliberately writes NO outcome line. outcomes.jsonl is an
                # event log, and "this run is still archived and still fine"
                # is not an event -- emitting it every pass would add
                # thousands of lines a day and bury the ones that matter. The
                # last line a run has in the log is already its current state,
                # and copied.tsv is the standing record of what is archived.
                $nAlready += 1
                continue
            }
            Write-Log "$rel changed since it was archived - re-examining"
        }

        # The archive copy is already carrying the hazard: an intact index
        # with a stale -wal beside it, one read-write open from being
        # truncated. We do not fix it and we certainly do not delete it --
        # we name it, so a human can. 113 .d are in this state right now.
        #
        # This runs AFTER the probe so the source's settle clock keeps
        # running while somebody deals with the archive.
        if (Test-Path -LiteralPath $target -PathType Container) {
            $destHazard = Test-SideFileHazard $target
            if ($destHazard) {
                $rec.verdict = $V_DESTHAZARD
                $rec.reason = $destHazard
                $rec.seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
                Write-Outcome $rec
                Write-Log "DEST HAZARD $rel - $destHazard"
                $nSkipped += 1
                continue
            }
        }

        if ($Settle) {
            # Manual mode: the probe above is the first of the pair. Wait, then
            # re-probe, and hand the gate a prior entry that is genuinely
            # -SettleSeconds old. A scheduled task takes its two probes from
            # two consecutive passes instead and never sleeps.
            Write-Log "settling $rel for $SettleSeconds s"
            Start-Sleep -Seconds $SettleSeconds
            $probes[$rel] = "$($now - $SettleSeconds)|$($probe.Sig)"
            $probe = Get-TreeProbe $d.FullName
            $rec.files = $probe.Files
            $rec.bytes = $probe.Bytes
            $rec.sig = $probe.Sig
            $nextProbes[$rel] = "$now|$($probe.Sig)"
        }

        $gate = Test-DReady $d.FullName $rel $probe $probes $now $exe
        $rec.frames = $gate.Frames
        $rec.timeSpanS = $gate.TimeSpan
        $rec.coverage = $gate.Coverage

        if ($gate.Verdict -ne "ready") {
            $rec.verdict = $gate.Verdict
            $rec.reason = $gate.Reason
            $rec.seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
            Write-Outcome $rec
            if ($gate.Verdict -eq $V_HAZARD -or $gate.Verdict -eq $V_INCOMPLETE) {
                Write-Log "REFUSED $rel - $($gate.Reason)"
            }
            $nSkipped += 1
            continue
        }

        if ($DryRun) {
            $rec.verdict = $V_DRYRUN
            $rec.reason = "gate passed"
            $rec.seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
            Write-Outcome $rec
            Write-Log "WOULD COPY $rel -> $target"
            continue
        }

        Write-Log "copying $rel -> $target"
        $exit = Copy-D $d.FullName $target
        $rec.roboExit = $exit
        # robocopy: 0-7 is success of some flavour, 8 and up is a real
        # failure. Bit 4 (value 4) means "mismatched files or directories"
        # and is worth naming even though it is not fatal.
        if ($exit -lt 0 -or $exit -ge 8) {
            $rec.verdict = $V_COPYFAIL
            $rec.reason = "robocopy exit $exit"
            $rec.seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
            Write-Outcome $rec
            Write-Log "FAILED $rel - robocopy exit $exit"
            $nFailed += 1
            continue
        }

        $sourceInfo = $null
        if ($gate.Frames -ge 0) {
            $sourceInfo = [PSCustomObject]@{ Frames = $gate.Frames; Coverage = $gate.Coverage }
        }
        $verify = Test-CopyVerified $d.FullName $target $sourceInfo $exe
        $rec.destFrames = $verify.DestFrames
        $rec.seconds = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        if (-not $verify.Ok) {
            # Not marked done, and nothing at the destination is touched.
            # Deleting on a failed verify would risk destroying a good archive
            # on a bad check; the next pass re-runs robocopy, which repairs a
            # short file because its size differs.
            $rec.verdict = $V_VERIFYFAIL
            $rec.reason = $verify.Reason
            Write-Outcome $rec
            Write-Log "VERIFY FAILED $rel - $($verify.Reason)"
            $nFailed += 1
            continue
        }

        $rec.verdict = $V_COPIED
        $rec.reason = ""
        Write-Outcome $rec
        Write-Log "done $rel ($($rec.files) files, $($rec.bytes) bytes, $($rec.frames) frames)"
        $copied[$rel] = $probe.Sig
        $nCopied += 1
    }

    # Only what we saw this pass is written back, so neither file grows
    # without bound on a source that is rotated.
    Write-Map $ProbeFile $nextProbes
    Write-Map $CopiedFile $copied

    $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    $summary = "ok - $nCopied copied, $nSkipped skipped, $nFailed failed, $nAlready already archived, $($elapsed)s"
    if ($nFailed -gt 0) { $summary = "ATTENTION - $summary" }
    Set-Status $summary
    if ($nCopied -gt 0 -or $nFailed -gt 0) { Write-Log $summary }
    if ($nFailed -gt 0) { return 2 }
    return 0
}

# ---------------------------------------------------------------- main ---

# One pass at a time. A scheduled task can be told IgnoreNew, but this script
# does not register its own task, so it cannot rely on that being set.
$haveLock = $false
try {
    if (Test-Path -LiteralPath $LockFile) {
        $lockAge = ((Get-Date) - (Get-Item -LiteralPath $LockFile).LastWriteTime).TotalSeconds
        # A pass that died leaves the file behind. Six hours is well past the
        # longest honest pass and well short of leaving a node stuck forever.
        if ($lockAge -lt 21600) {
            Set-Status "another pass has been running for $([math]::Round($lockAge)) s - standing down"
            exit 0
        }
        Write-Log "clearing a stale lock ($([math]::Round($lockAge)) s old)"
        Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue
    }
    Set-Content -LiteralPath $LockFile -Value "$PID $(Get-Stamp)" -ErrorAction SilentlyContinue
    $haveLock = $true

    # Establish that there is something to copy WITH before examining
    # anything. Finding out halfway through a pass that robocopy is missing
    # produces a log full of copy-failed against runs that are perfectly fine.
    if (-not $DryRun) {
        $haveRobo = $false
        if (Get-Command $RobocopyExe -ErrorAction SilentlyContinue) { $haveRobo = $true }
        elseif (Test-Path -LiteralPath $RobocopyExe) { $haveRobo = $true }
        if (-not $haveRobo) {
            Set-Status "NO COPIER - $RobocopyExe was not found"
            Write-Log "$RobocopyExe was not found - nothing copied. Use -DryRun to exercise the gate without it."
            exit 1
        }
    }
    if ($InterPacketGapMs -gt 0 -and $Threads -gt 0) {
        Write-Log "-InterPacketGapMs is set, so /MT is dropped: robocopy rejects the two together"
    }

    $code = Invoke-Pass
    exit $code
} finally {
    if ($haveLock) { Remove-Item -LiteralPath $LockFile -Force -ErrorAction SilentlyContinue }
    # Scratch is only ever this script's own temporary index copies.
    foreach ($f in @(Get-ChildItem -LiteralPath $ScratchDir -File -Force -ErrorAction SilentlyContinue)) {
        if (((Get-Date) - $f.LastWriteTime).TotalHours -gt 6) {
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}
