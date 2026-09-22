# `bruker_d_copy.ps1` — a Bruker `.d` archiver that cannot copy a live run

A drop-in replacement for the raw `.d` robocopy one-liner. Not installed, not
scheduled, and it modifies no existing script — see
[Deploying it](#deploying-it).

```
robocopy               <src> <dst> /E /Z /FFT     <- what it replaces
bruker_d_copy.ps1      <src> <dst>                <- same shape, gated
copy_raw_d_safe.bat                               <- the same, as a .bat
```

Source and destination are **positional**, in the same order, and a run's path
relative to the source is preserved exactly as `/E` would (`D:\Data\Aug26\run.d`
→ `<dst>\Aug26\run.d`). It **accumulates**; nothing at the destination is ever
removed. `.d` folders are found at **any depth** up to `-MaxDepth` (default 4),
because `/E` copies the whole tree and neither the instrument nor the operator
is obliged to stop at month folders.

| | |
|---|---|
| `bruker_d_copy.ps1` | the copier |
| `copy_raw_d_safe.bat` | one-line wrapper — edit two `set` lines, point the scheduled task at it |
| `swap_to_safe_copier.ps1` | staged, reversible swap from the old task to this one |
| `rollback_safe_copier.ps1` | undo the swap |
| `test_bruker_d_copy.ps1` | 143 assertions against synthetic fixtures |
| `test_swap_to_safe_copier.ps1` | 69 assertions on the swap logic |
| `MANIFEST.txt` | SHA-256 of each file, so a deployed copy can be checked against the repo |

## Where this lives

**The FRAN repo is canonical. The share is a deployment target.**

| | |
|---|---|
| canonical | `FRAN/ingest/windows/` — **edit here** |
| Hive | `/nfs/lssc0/flinders/proteomics/Data/FRAN_SNE_export/bruker_copy/` |
| Mac | `/Volumes/proteomics/Data/FRAN_SNE_export/bruker_copy/` |
| Windows | `R:\Data\FRAN_SNE_export\bruker_copy\` |

This is the share's own **Rule 5** (`FRAN_SNE_export/CLAUDE.md`): *"fix in the
repo → commit → sync out"*, never edit on the share and copy back. That rule
exists because this folder once drifted three months from the repo and the
divergence was unrecoverable in one direction.

`MANIFEST.txt` carries a SHA-256 for every file, so anyone can tell whether a
deployed copy matches the repo:

```sh
cd .../FRAN_SNE_export/bruker_copy && shasum -a 256 -c <(grep -E '^[0-9a-f]{64}' MANIFEST.txt | awk '{print $1"  "$3}')
```

```bat
certutil -hashfile bruker_d_copy.ps1 SHA256
```

A line that does not match means the deployed copy is stale or was edited in
place. **Re-sync from the repo; do not merge.**

---

## The damage this exists to prevent

A Bruker `.d` holds `analysis.tdf` (a SQLite frame index) and `analysis.tdf_bin`
(the spectra). While a run acquires, SQLite parks pending pages in
`analysis.tdf-wal` beside the database.

A copier that grabs the folder mid-acquisition lands a **finished-looking**
`analysis.tdf` at the destination with a **stale `analysis.tdf-wal` next to it**.
Nothing is visibly wrong yet. Then some later program opens that database
**read-write**, SQLite does exactly what it is supposed to do — checkpoints the
WAL into the main file — and the index is truncated back to its mid-acquisition
size. The frames are gone. `analysis.tdf_bin` still holds every spectrum, but
nothing can address them.

Measured on this cluster:

| | |
|---|---|
| `.d` already destroyed | **350** |
| worst example | index covers **0.74%** of a 2.4 GB `tdf_bin` — 1,451 frames over 133 s where the intact sibling has 13,736 frames over 21 minutes |
| how fast it goes | **63 files in 77 minutes**, by a job that merely *read* them with a bare `sqlite3` |
| how it presents | DIA-NN read one and reported **80 precursors** against Spectronaut's **127,842** — and **exited 0** |
| still in the hazard state | **113** `.d` with an intact index and a stale 4.2–4.4 MB `-wal` beside it, one read-write open from the same end |

The copier is where the hazard is created, so this is where it stops.

---

## The copier this replaces

Confirmed by the owner: the raw `.d` copier is essentially
`/proteomics/Data/lab/Robocopy/copy_all_data_network.bat` pointed at different
directories —

```bat
robocopy <instrument raw dir> \\<server>\protcore\Data\raw_data\... /E /Z /FFT
```

Every defect is in that one line.

| | |
|---|---|
| **no `/XF`** | It transports `analysis.tdf-wal`, `-shm`, `-journal`. **This is the mechanism.** A stale mid-run WAL reaches the destination and sits beside a finished-looking `analysis.tdf` waiting for a read-write open. |
| **no completeness gate** | Copies whatever exists when it runs, acquiring or not. |
| **no retry limit** | Default `/R:1000000 /W:30` ≈ **347 days** retrying one locked file — and a file the instrument still holds open is exactly what it will hit. |
| **`/Z`** | Makes it worse, not better. See [the `/Z` decision](#the-z-decision). |
| **`/FFT`** | Coarsens "has this file changed?" to 2 seconds. See [`/FFT` and the stability probe](#fft-and-the-stability-probe). |
| **no logging, no verification** | Nothing records which runs were copied mid-acquisition, which is why this ran unnoticed for five months. |

### The four siblings in that folder

**All five were read and reviewed**; none of them copies `.d`, and none is
replaced here. All are one-liners, all from 2022–2024, **none has a retry
limit** — so any of them can wedge for ~347 days on a single unreachable file.
One of them, flagged below, destroys data:

```
copy_all_data_network.bat       B:/autoSNE -> ...\Forge_apectronaut_archive  /E /Z /FFT
ht htrms copy.bat               network -> D:\ht_projects\batplate1  *.htrms
move_6month_spectronaut_arch..  E:\spectronaut -> ...\Spectronaut_archive  /MOV /MINAGE:120
spectronaut from network.bat    network -> B:/Spectronaut_temp  /E /Z /FFT /mt
spectronaut to network.bat      B:/Spectronaut_temp -> network  /E /Z /FFT /mt
```

Worth flagging to the owner:

> ### ⚠ `move_6month_spectronaut_archive.bat` is actively dangerous
>
> ```bat
> robocopy E:\spectronaut \\169.237.96.1\protcore\Data\lab\Spectronaut_archive /MOV /MINAGE:120
> ```
>
> `/MOV` deletes each source file once it has been copied. There is **no
> `/E` and no `/S`**, so robocopy only ever touches files in the **root** of
> `E:\spectronaut` — but it deletes the ones it moved. A Spectronaut archive
> with subdirectories is therefore **partially moved and partially
> destroyed**, on a schedule, and the two halves end up in different places.
> There is also **no verification** that a copy succeeded before the source
> is deleted, and **no retry limit**, so a transient share failure can delete
> a source whose copy never landed.
>
> **Not fixed here — it is not this project's file and the owner has not
> asked.** Recorded because anyone reading this folder should know it is
> there. Raised with the owner separately.
>
> This script deliberately does not imitate any part of that pattern: it
> never deletes anything, on either side, ever.
- **`copy_all_data_network.bat` writes to `Forge_apectronaut_archive`** — a
  typo for *spectronaut*, unreviewed since November 2023.
- The two `/mt` scripts pass `/mt` with no value, which robocopy reads as
  `/MT:8` — eight threads onto a shared SMB mount.

### `STAN/scripts/flinders_copy.ps1`

There is also a 629-line PowerShell copier in the STAN repo (`D:\Data` → the
Flinders `tTOF_HT` archive, scheduled every 5 minutes). It is **not** the
copier that caused this damage, but it shares the same defects and would cause
it again, so its faults are recorded here.

Much of it is right — bounded retries, `/IPG` pacing,
`BelowNormal` priority, the month-spelling reconciliation, a UNC destination
rather than a drive letter, a status file for proof of life. Four things are
not.

**1. It transports the WAL too.** `flinders_copy.ps1:385-388`

```powershell
$roboArgs = @("`"$($Dir.FullName)`"", "`"$target`"",
              "/E", "/Z", "/FFT", "/R:2", "/W:10", "/IPG:20", ...)
```

`/E` with no `/XF`, exactly like the `.bat`. Same mechanism, second source.

**2. Its stability check never asks how old the previous probe is.**
`flinders_copy.ps1:612`

```powershell
if ($was[$item.Rel] -eq $sig -and -not (Test-StillWriting $item.Dir.FullName)) {
```

`$was` is whatever the *previous pass* recorded. The intent is "five minutes of
no growth", but nothing checks that five minutes actually elapsed. Two passes
20 seconds apart — after a task restart, a resumed laptop, a `-SkipBacklog`, a
`StartWhenAvailable` catch-up firing twice — agree, and the run is copied.

**3. Its fingerprint is `files/bytes` only.** `flinders_copy.ps1:272`

```powershell
return "$($items.Count)/$bytes"
```

Two genuinely different states share that string. The most plausible one here is
a WAL checkpoint: bytes move out of `analysis.tdf-wal` and into `analysis.tdf`,
the file count and the total are unchanged, and the tree reads as "not
growing". (Section 2 of the test suite builds this case and confirms the new
per-file probe catches what the old one cannot.)

**4. Nothing is verified, and "done" is permanent.**
`flinders_copy.ps1:392-397` — robocopy exit `< 8` is success, the run is
appended to `flinders_copied.txt`, and it is never looked at again. A copy that
half-completed is archived forever under a name that says it is fine. The
script's own header admits this: *"copying then would archive a truncated run
PERMANENTLY, because it gets marked done and never looked at again."*

Two things it does **not** get wrong, worth saying because they narrow the
blame: its `Test-StillWriting` opens `analysis.tdf_bin`, never the SQLite
database, so the copier has never damaged a source file; and it never deletes
anything.

### Replace or wrap?

**Replace.** Wrapping cannot fix the `/XF` defect — the WAL is transported by
the very robocopy call a wrapper would be wrapping.

**If it also replaces `flinders_copy.ps1`, it does not carry over `flinders_copy.ps1`'s month-spelling reconciliation.**
That script recognises that the archive's `JUL26`, `july26` and `Jul26` all mean
the same month and files a local `july26` into whichever already exists. This
one would create a second folder beside it. That logic
(`flinders_copy.ps1:207-247`, `Get-MonthDate` / `Get-DestFolder`) is good and
independently tested in `STAN/tests/test_flinders_copy.ps1`; port it in first.
**A deliberate gap, not an oversight** — and irrelevant to the `.bat` this
actually replaces, which has no month logic at all.

---

## The gate

A `.d` is copied only when **every** check passes. Each is fail-closed:
anything that cannot be established is a refusal, never a pass.

| # | Check | Why |
|---|---|---|
| 1 | `analysis.tdf` (or `analysis.baf`) exists and is non-empty | An empty or half-created folder is not "acquiring", it is *not a run*. Copying it puts a shell in the archive that looks like a real acquisition to everything downstream. |
| 2 | **No live SQLite side file** anywhere in the tree | The hazard itself. A `-wal` with any content is refused. A `-shm` or `-journal` is refused *at any size, including zero* — there is no benign reason for one to exist at rest. A **zero-byte `-wal` is allowed**: it holds no pending frames, so a later checkpoint has nothing to write back. Matched on the filename suffix, so `chromatography-data.sqlite-wal` is caught too. |
| 3 | **mtime/size stability** — every file's path, length and mtime identical across two probes **at least `-SettleSeconds` apart** | The elapsed-time requirement is the fix for defect #2 above. Per-file rather than `files/bytes` is the fix for #3. |
| 4 | Nothing holds `analysis.tdf_bin` open | An OS-level fact, not an inference about Bruker's format. timsControl keeps a handle on the binary for the whole acquisition. |
| 5 | **The frame index covers the binary** — `MAX(Frames.TimsId) / size(analysis.tdf_bin) ≥ -MinCoverage` | The strongest check. `TimsId` is a frame's byte offset into the binary, so on an intact run the last frame starts at `(n-1)/n` of the file and the ratio is ~0.999. The damaged `.d` above scores **0.0074**. A `.d` truncated by an earlier bad copy passes checks 1–4 — it is stable, clean and closed — and fails only here. A ratio **above 1.0** is also refused: the index reaches past the end of the spectra, which is the same damage from the other side. |

Check 5 needs `sqlite3`; if it is unavailable the outcome records
`coverage unavailable` and the other four still stand. It is never silently
treated as a pass.

### The database is never opened in place

Check 5 copies the (megabyte-scale) `analysis.tdf` to local scratch and opens
**that**, as `file:///...?mode=ro&immutable=1`. The instrument's file and the
archive's file are only ever read as bytes. This costs a few MB of I/O — the
index is megabytes, the binary is gigabytes and only its size is needed — and
buys an absolute guarantee that inspection cannot damage what it inspects. It
also sidesteps SQLite's refusal to accept a UNC authority in a `file:` URI,
which an archive path usually is.

`immutable=1` is not decoration. It tells SQLite the file cannot change
underneath it, so SQLite skips the WAL and the shared-memory index entirely and
reads the main database as it sits on disk — both the honest answer (the real
index, not a WAL-shadowed view) and a guarantee that not one byte is written.

**`sqlite3` is proved to honour URI filenames before it is trusted.** With
`SQLITE_USE_URI` off, `file:...?mode=ro&immutable=1` is taken as a *literal
filename*: `sqlite3` creates a new empty database with that name and reports
nothing wrong, and every coverage check would pass on an empty database. So
`Initialize-Sqlite` writes a scratch database with a known value in it and
requires the URI open to read that value back. **If the proof fails, the check
is disabled — never downgraded to an unsafe open.**

---

## robocopy flags

| Flag | Why |
|---|---|
| `/E` | Subdirectories including empty ones. A `.d` has them; `/S` would silently change its shape. |
| `/XF analysis.tdf-wal analysis.tdf-shm analysis.tdf-journal *-wal *-shm *-journal` | **The fix for defect #1.** Belt to the gate's braces: the gate should already have refused any `.d` carrying these, but if one arrives it still must not travel. The wildcards catch companions of any other SQLite database in the tree. |
| `/COPY:DAT` | Data, attributes, timestamps. Stated rather than left to the default so nobody later reaches for `/COPYALL`, which tries for ACLs and owner and fails on a share where we do not hold those rights. |
| `/DCOPY:DAT` | The same for the `.d` **folder**. Without it the archived run's mtime becomes the copy date and every downstream "when was this acquired" answer is wrong. `flinders_copy.ps1` omits this. |
| `/FFT` | 2-second timestamp granularity. An SMB or NFS destination does not keep NTFS's resolution, so without it robocopy believes every file differs and re-copies the whole archive forever. |
| ~~`/Z`~~ | **Deliberately absent.** See below. `-Restartable` puts it back. Never `/ZB`: backup mode needs `SeBackupPrivilege` and reads past ACLs. |
| `/R:2 /W:10` | The defaults are `/R:1000000 /W:30` — **~347 days wedged on one locked file**, which is how a copier silently stops making progress, and a file the instrument still holds open is exactly what it will hit. 2×10 s bounds a failure to about 20 s and lets the next pass retry the whole run. |
| `/MT:4` | Without `/MT` robocopy is single-threaded, which is slow on a `.d`'s hundreds of small files; `/MT:8`+ on a shared SMB mount starves every other consumer. 4 takes most of the win without pinning the link. `-Threads 0` turns it off. |
| `/IPG:n` | Off by default. Set it (`flinders_copy.ps1` uses 20) when the copier runs **on the acquiring PC**, to hand bandwidth back to the instrument. **Mutually exclusive with `/MT`** — robocopy rejects the pair — so setting it drops `/MT` and logs that it did. |
| `/NP /NFL /NDL` | No percentages (megabytes of carriage returns in a log file), no file or directory listing. Verification walks both trees itself, which is stronger than trusting robocopy's account of its own work. |
| `/TS /LOG+:` | Timestamps on what it does print; append to the robocopy log. |

Deliberately absent, and they must stay absent:

- **`/MIR`, `/PURGE`** — delete destination files missing from the source. On a
  copier whose entire purpose is not destroying archives, a loaded gun pointed
  at the archive.
- **`/MOV`, `/MOVE`** — delete the source.
- **`/B`, `/ZB`** — backup mode.

The process runs at `BelowNormal` priority so a copy never competes with an
acquisition for CPU.

### The `/Z` decision

**Dropped.** The copier this replaces uses it, and it is the wrong default.

Restartable mode resumes a partially copied file from a recorded offset.
Whether that is safe rests entirely on robocopy noticing that the source
changed since the interrupted attempt — a size and timestamp comparison, which
`/FFT` deliberately coarsens to 2 seconds. A file that grew and was then
resumed from the old offset produces a destination that is two points in time
stitched together, and nothing downstream can tell. That is the same class of
defect as the stale WAL: a file that looks finished and is not.

And it buys nothing here. A pass that dies leaves a short file; the next pass
compares sizes, sees the difference, and copies the whole file again, while
verification reports `verify-failed` in the meantime. The benefit `/Z` offers —
not re-sending a large file — **idempotency already provides**, without
depending on a comparison we just blunted. Paying throughput on every copy for
it is the wrong trade.

`-Restartable` puts it back for a link so unreliable that a whole-file re-copy
never completes. The hazard above is then real, but bounded by the gate, which
has already established that the source is not changing.

### `/FFT` and the stability probe

`/FFT` makes robocopy compare timestamps at 2-second (FAT) granularity, which
is necessary — an SMB or NFS destination does not keep NTFS's resolution, and
without it robocopy believes every file differs and re-copies the archive
forever. But it makes "has this changed?" coarser, so the gate must not lean on
mtime alone.

It does not. The stability probe records **path, length and mtime for every
file**, so a file that grew is caught by its length whatever the timestamp
resolution is. And the two probes are `-SettleSeconds` apart — **600 by
default, 300× the granularity** — so a 2-second ambiguity cannot span them.
Verification compares **sizes only**, never timestamps, so `/FFT` does not
reach it at all.

---

## After the copy

robocopy returning 0 means *robocopy believes it succeeded*. It is not evidence
about what is on the archive. Every copy is then verified:

1. **No side file reached the destination.** If this ever fires, `/XF` has been
   defeated or something else is writing into the archive.
2. **Every source file present at the same size**, and no extras. A truncated
   transfer differs in length — which is both what this catches and what the
   next pass repairs by itself, since robocopy compares size and timestamp.
3. **The destination index still covers the binary**, and reports the same frame
   count as the source. A cross-check on a different axis: it would catch a
   destination file of the right length and the wrong content.

A failed verify is **not** marked done and **nothing at the destination is
touched** — deleting on a failed verify would risk destroying a good archive on
a bad check. The next pass re-runs robocopy, which repairs a short file because
its size differs.

## It also finds hazards already in the archive

Before copying, the destination is checked for side files. A `.d` already
sitting in the hazard state — intact index, stale `-wal` beside it, like the 113
on the cluster now — is reported as `blocked-dest-hazard` with the offending
file named, and the copy is held. **It is not fixed and not deleted**: that is a
human decision. The source's settle clock keeps running meanwhile, so clearing
the archive side is all that is needed to let it through.

---

## Logs

All under `-StateDir` (default `%ProgramData%\FRAN\bruker_d_copy`):

| File | |
|---|---|
| `outcomes.jsonl` | **machine-readable**, one JSON object per line |
| `bruker_d_copy.log` | human-readable, events only |
| `status.txt` | overwritten every pass; its mtime is the proof the task is alive |
| `probes.tsv` | settling state: `rel → firstSeenUnix\|signature` |
| `copied.tsv` | `rel → signature` of what is archived and verified |
| `robocopy.log` | robocopy's own |

JSON Lines rather than one JSON array on purpose: a pass that dies halfway
still leaves every line before it parseable, and a consumer can tail the file
instead of re-reading it.

```json
{"ts":"2026-09-21T18:04:11Z","host":"CBS-GC1414-STAR","run":"Sep26\\x.d",
 "verdict":"skipped-hazard","reason":"live -wal: analysis.tdf-wal is 4404640 bytes",
 "files":5,"bytes":2401234,"frames":-1,"coverage":-1,"roboExit":-1,"seconds":0.4}
```

| Verdict | |
|---|---|
| `copied` | gate passed, copied, verified |
| `skipped-already-copied` | verified earlier, source unchanged |
| `skipped-still-acquiring` | tree changed, or the binary is held open |
| `skipped-settling` | stable, but not yet for long enough |
| `skipped-hazard` | live SQLite side file at the **source** |
| `skipped-incomplete` | malformed, or the index does not cover the binary |
| `blocked-dest-hazard` | the **archive** copy already carries a side file |
| `copy-failed` | robocopy returned ≥ 8 |
| `verify-failed` | copied, but source and destination disagree |
| `dry-run-would-copy` | `-DryRun` |

A run in the steady state (`skipped-already-copied`) writes **no** line:
`outcomes.jsonl` is an event log, and "still archived and still fine" is not an
event — emitting it every pass would add thousands of lines a day and bury the
ones that matter. The last line a run has is already its current state;
`copied.tsv` is the standing record of what is archived.

---

## Options

| | |
|---|---|
| `-Source` / `-Dest` | required, and **positional** — `bruker_d_copy.ps1 <src> <dst>`, the same order as robocopy. |
| `-MaxDepth` | how deep under the source to look for `*.d` (default 4). |
| `-SettleSeconds` | how long a `.d` must sit unchanged. **Default 600.** Long enough to ride out an LC equilibration lull early in a run — the window in which a stability check alone would archive a truncated acquisition. |
| `-Settle` | take both probes in this process, sleeping between them. For a **manual** run. A scheduled task must not use this: without it the two probes are consecutive passes and the process lives for seconds. |
| `-LookbackHours` | ignore `.d` older than this (default 168). `-All` overrides. |
| `-MinCoverage` | default 0.90, leaving room for trailing padding. |
| `-Sqlite3` | path to `sqlite3.exe`; found on PATH otherwise. |
| `-NoCoverage` / `-NoLockProbe` | disable check 5 / check 4. |
| `-Threads` / `-InterPacketGapMs` / `-Restartable` | see the flag table. |
| `-DryRun` | run the gate, log every verdict, copy nothing. |
| `-Rescan` | re-examine runs already recorded as copied. |
| `-RobocopyExe` | the copier to use. Named so the copy path can be tested against a stand-in. |
| `-Show` | echo the log. |

Safe to schedule: one pass at a time via a lock file (stale after 6 hours),
idempotent, and it never deletes anything on either side.

---

## Deploying it

**Not installed and not scheduled by this script**, and no existing `.bat` is
modified — deliberately, so nothing lands on an instrument PC without a human
deciding to put it there.

Drop `bruker_d_copy.ps1` and `copy_raw_d_safe.bat` in the same folder, edit the
two `set` lines at the top of the `.bat` to the exact two paths the old
one-liner had, and point whatever triggers the old `.bat` at the new one. That
is the whole swap — same invocation shape, same source and destination
conventions, nothing else to rewire.

If it is scheduled fresh instead:

```bat
schtasks /Create /TN "FRAN Bruker .d copy" /SC MINUTE /MO 5 /RL LIMITED ^
  /TR "C:\ProgramData\FRAN\copy_raw_d_safe.bat"
```

Run it as the **logged-on user, never SYSTEM**: the copy has to happen in a
session that can reach the share. Store a **UNC** destination, not a drive
letter — mapped drives are per-session and an elevated process cannot see them
at all.

### The staged swap

`swap_to_safe_copier.ps1` retires the old scheduled task and registers this
one, with a test run against real data in between. **It does nothing without
`-Execute`** — the default prints the plan and changes not one thing.

```bat
REM read the plan first
swap_to_safe_copier.ps1 -Source D:\Data -Dest \\128.120.208.2\protcore\Data\raw_data\tTOF_HT -TestDest D:\swap_test
REM then do it
swap_to_safe_copier.ps1 -Source D:\Data -Dest ... -TestDest D:\swap_test -Execute
```

| step | | stops on failure |
|---|---|---|
| **0** | preflight: the copier parses, source and archive are reachable, and the **test destination is provably not the live archive** — checked in both directions, so neither a subfolder of the archive nor a parent containing it is accepted | ✔ |
| **1** | **capture first.** Identify the old task by *what it runs*, not by a guessed name — ambiguous or no match **aborts**. Export its XML to a timestamped backup on the share, re-parse it, and abort if it cannot be restored | ✔ |
| **2** | `schtasks /Change /DISABLE`. **Never `/Delete`** — deletion is not reversible in a hurry. Verified by re-querying | ✔ |
| **3** | test run: the new copier against a few real `.d`, into the **test** destination, two passes a settle interval apart | ✔ |
| **4** | verify: **source `analysis.tdf` SHA-256 unchanged**, no side file transported, every verdict as expected | ✔ |
| **5** | register the new task by rewriting **only the `<Exec>` action** of the captured XML, so schedule, account, logon type and working directory are inherited by construction rather than re-derived | ✔ |
| **6** | write a ready-to-run rollback `.cmd` beside the backup | |

The sample is chosen to prove something: **at least one clean `.d` that must be
accepted, and at least one with a live `-wal` that must be refused.** If the
source holds no hazard run at that moment, it says so loudly rather than
skipping the case — that path is covered by the synthetic fixtures but not by
that machine's real data, and the swap notes the difference. If there is no
*clean* run it aborts outright, because a test that can only refuse proves
nothing.

`-TestSettleSeconds` (default 120) shortens the settle **for the test only**, so
a staged swap takes minutes rather than half an hour; the mechanism exercised is
identical and the registered task gets the full 600.

Inherited settings that are decisions get called out — a task running as
**SYSTEM** is flagged because SYSTEM cannot see mapped network drives, so a
drive-letter destination would silently fail.

### Rollback

`rollback_safe_copier.ps1 -BackupXml <the captured XML> -OldTaskName ... -Execute`,
or just run the `..._ROLLBACK.cmd` the swap wrote beside the backup. Also
dry-run by default.

It re-enables the old task (or recreates it from the XML if someone removed it),
**then** removes the new one — that order, because a window with nothing copying
is the one state worse than either end. It never deletes the backup, the old
script, or the new copier's logs.

It also says the thing that is easy to forget: **the old copier is the one that
transports the WAL.** Rolling back restores the hazard, so once the failure is
understood, disable it again by hand.

### First run

**Run `copy_raw_d_safe.bat -DryRun -Show` for a day and read
`outcomes.jsonl` before letting it copy anything.** `-DryRun` runs the whole
gate and logs every verdict without copying a byte, so it will tell you how
many `.d` the old copier would have taken mid-acquisition.

---

## Tests

```
pwsh -NoProfile -File test_bruker_d_copy.ps1
```

**212 assertions, all passing** — 143 on the copier, 69 on the swap — against
synthetic `.d` fixtures and synthetic Task Scheduler XML, built in a temp
directory.

```
pwsh -NoProfile -File test_bruker_d_copy.ps1
pwsh -NoProfile -File test_swap_to_safe_copier.ps1
```

The swap suite covers the parts that can be wrong in a way that matters and do
not need Windows: the test-destination guard (including the `D:\archive_test`
vs `D:\archive` prefix trap), identifying the old task from `schtasks` CSV
output with its per-folder repeated headers, the `<Exec>`-only XML rewrite
preserving triggers/principal/working directory, sample selection always
including a refusal case when one exists, and both scripts refusing to act
without `-Execute`. No real `.d` is touched; nothing under `/nfs`, `/quobyte` or
`/Volumes` is read. Function definitions are pulled out of the shipped `.ps1`
through the PowerShell AST, so the tests cannot drift from what ships.

There is no PowerShell on the dev Mac; a portable `pwsh` from the
`PowerShell/PowerShell` release tarball runs this without installing anything.

The fixture builder makes a genuine SQLite `analysis.tdf` with a real `Frames`
table, an `analysis.tdf_bin` of a chosen size, and a `-CoverFrac` that puts the
last frame's `TimsId` exactly where asked — so `whole.d` (13,736 frames, ~0.999)
and `wrecked.d` (1,451 frames, 0.0074) reproduce the measured pair.

Sections 9 and 10 run the **real shipped script** end to end. Section 10 drives
it with a `robocopy` stand-in — a shell script implementing just the two
behaviours the script depends on, copy the tree and honour `/XF` — so the
copy-verify-record half of a pass is exercised even though robocopy is
Windows-only.

### What remains untested, because the Windows nodes are down

`win-1`, `win-forge` and `win-2` are all down, so **none of this has run on
Windows**. Specifically untested:

- **robocopy itself.** Every flag is argued from the documentation, not
  observed. `/XF` with six patterns and `/DCOPY:DAT` are the ones to watch on
  the first real run. *Robocopy rejects `/MT` with
  `/IPG`; the script already avoids that pair, but the rejection has not been
  seen.*
- **PowerShell 5.1.** Tests ran under pwsh 7.6.6 on macOS. The script is written
  to STAN's 5.1 rules and audited against them (no `+` concatenation, no inline
  ternary, no `Where-Object`, `Join-Path` throughout, no PS array passed where
  .NET wants `string[]`, no `return ,$array` into an `@()` caller), but 5.1 has
  not executed a line of it.
- **The exclusive-open probe (check 4).** macOS does not enforce the Windows
  share mode, so `Test-BinLocked` is forced off in the tests. Its behaviour
  against a live timsControl handle is unverified — this is the check most
  likely to behave differently on Windows.
- **Real timsTOF data.** Every `.d` here is synthetic. `TimsId` as the frame
  offset and `Frames` as the table name are from the TDF schema; the coverage
  ratio has not been computed against a real intact `.d` on this cluster. The
  schema is probed before use and an unrecognised one is reported rather than
  guessed, so a mismatch fails loudly — but `-MinCoverage` should be sanity-
  checked against a few known-good runs before it is trusted to refuse.
- **UNC destinations, SMB latency, files locked by another process, and
  `%ProgramData%` permissions under a non-admin scheduled task.**

**The entire swap, end to end.** `schtasks` does not exist off Windows, so
every one of these is untested against a real Task Scheduler:

- **task discovery** — `schtasks /query /FO CSV /V`'s real column names and
  quoting. The parser is tested against a hand-built fixture matching the
  documented format, not against live output. **If discovery misfires the swap
  aborts rather than guessing**, which is the safe direction, but it may abort
  on a machine where it should have succeeded. `-OldTaskName` is the way past
  that.
- **`schtasks /Change /DISABLE` and the verification that reads it back.** The
  enabled/disabled check parses `/FO LIST` text; the exact strings are from the
  documentation, not observed.
- **`schtasks /Create /XML`** accepting the rewritten XML. The rewrite is tested
  and preserves everything but `<Exec>`; whether Task Scheduler accepts the
  result is not.
- **The `-Execute` path of either script has never run.** Only the dry-run and
  abort paths have been exercised.
- **The test run inside the swap** — it invokes `powershell.exe`, so it has
  never executed; only the surrounding logic has.

Because of that, run the swap's **dry run first and read all of it**, and keep
the printed rollback command to hand before running `-Execute`.
