"""xic_extractor.py — extract observed ion chromatograms (XICs) from a diaPASEF run.

Given a precursor's m/z, retention time and ion mobility, pull the elution profile of each of its
fragments (and the MS1 isotope channels) straight out of the acquired data. This is the extraction
core used to build training tensors for the FRAN/Retriever-DIA work; the SLURM driver that scans a
whole run and writes shards stays out of this module on purpose, so the extractor can be imported,
tested and run anywhere.

WHICH FRAGMENTS TO EXTRACT — the thing this version changes
-----------------------------------------------------------
The original selection, `top6_by_mz()`, derives six ions from the bare sequence and takes the
HIGHEST-m/z ones. Measured against Spectronaut 21's own exported XICs for the same run, only 26.3%
of those were ions Spectronaut ever traced: they sit at a median 0.86 of peptide length (the
longest y/b ions), while the ions that actually carry intensity sit at 0.47, and this route can
never emit the ~15% of traced ions that are multiply charged.

The observed fragments are already recorded per precursor in FRAN's spectrum Lance lane
(`spectrum_lance.py`), where they reproduce Spectronaut's traced ion set 98.7% exactly. So prefer
`ObservedFragments`; `top6_by_mz()` remains only as the fallback when a precursor isn't in the lane.

*** DECOYS: do not mix the two sources. *** A decoy is a mutated sequence and has no observed
spectrum. Giving targets observed ions while decoys get sequence-predicted ones makes the two arms
separable on ion CHOICE alone — a label leak that has nothing to do with the data. `mirror_ions()`
exists to prevent that: it re-computes the target's own ion identities (y8, b4, ...) on the mutated
sequence, so both arms use identical selection logic and differ only in m/z.

CONFIGURATION — environment, no hard-coded site paths, no credentials.
    FRAN_XIC_PPM      mass tolerance, ppm            (default 15)
    FRAN_XIC_IM_TOL   ion-mobility half-window, 1/K0 (default 0.05)
    FRAN_XIC_RT_HALF  RT half-window, seconds        (default 12)
    FRAN_XIC_POINTS   points per resampled trace     (default 32)
    FRAN_XIC_MS1_CHANNELS  isotope channels M, M+1 ...   (default 5, matching Spectronaut)
This module never opens a database or a credential file.

CACHE FORMAT
    A directory of .npy arrays produced from the vendor .d (see the pipeline's cache builder):
    rt_values, mobility_values, meta, ms1_{mz,int,scan,cycle_idx}, ev_{mz,int,scan},
    cycle_idx, iso_{lo,hi}. Arrays are memory-mapped, so a cache far larger than RAM is fine.

VERIFICATION (v1.4.0, 2026-07-28)
---------------------------------
Validated against Spectronaut 21's own exported chromatograms — its "All XIC" SQLite export for the
same run (11May2026_DIA_60spd_VER_185_S2-B5_1_21766, diaPASEF, dog + yeast entrapment). Both sides
describe the same acquisition, so any disagreement is real. Nothing is fitted. Every one of our
curves is read from the Lance store; the raw .d is not opened during comparison.

  channel              n      r median   r>0.8   apex |d|   area ours/SN
  MS2 fragments   99,500        0.8791   72.1%      0.39s        1.028x
  MS2 loss ions    5,576        0.8608   65.0%      0.39s        1.047x
  mono-isotopic   18,015        0.9861   94.3%      0.37s        0.963x
  M+1             18,015        0.9835   92.7%      0.37s        0.976x
  M+2             18,015        0.9663   87.2%      0.39s        1.035x
  M+3             18,010        0.9259   76.8%      0.39s        1.111x
  M+4             13,086        0.8931   70.2%      0.39s        1.192x

  MS1 agrees better than MS2 for a structural reason: MS1 extracts the precursor itself, with no
  isolation-window ambiguity, while fragments must survive a 12-window diaPASEF gate where
  co-isolated precursors contribute interference.

KNOWN DEFECT — MS2 timestamps run ~0.3 s early (open)
-----------------------------------------------------
Measured over 42,199 clean fragment channels (r>0.9, sub-bin apex): our MS2 apex sits a median
-0.295 s from Spectronaut's, while MS1 sits at -0.032 s. The cause is `cycle_rt[c] = rt[c *
frames_per_cycle]` — every event in a cycle is stamped with the time of that cycle's FIRST frame,
which is the MS1 frame. The 11 MS2 frames are acquired later within the same cycle, so fragment
traces are labelled ~a quarter-cycle early while MS1 lands correctly. It does not alter peak SHAPE
(correlations are unaffected) but it is wrong for anything reading absolute fragment RT, and it is
the likely source of the residual MS1-vs-MS2 co-elution scatter. Fix: offset the MS2 axis by the
mean MS1->MS2 frame gap within a cycle.

TWO CORRECTIONS WORTH REMEMBERING
---------------------------------
1. v1.0.0 documented a 1.238x area excess as INHERENT — Spectronaut integrating within fitted peak
   boundaries while this extractor sums across the gate. It was not inherent. It was an ion-mobility
   window 4.6x wider than the measured peak. A tolerance sweep moved the area ratio to ~1.00 and
   improved every channel. The mechanism was plausible and fit every measurement, which is exactly
   why it survived three verification rounds: the data could not separate "integrates differently"
   from "window too wide". Only a sweep could. Check whether a PARAMETER explains a systematic
   before writing it down as a property.
2. Pairing channels on the bare ion label mispaired 4,187 loss-ion channels (y5-H2O compared against
   plain y5). Fixing it moved the MS2 median only 0.8751 -> 0.8791, because a loss ion and its parent
   are the same peptide at the same retention time — their shapes barely differ. A shape metric was
   nearly blind to the error; an area metric would not have been. Match the metric to the mistake
   you are trying to catch.

  For contrast, the same comparison using top6_by_mz() instead of the observed fragments: r median
  0.679, apex median 1.33 s, and 108 of 400 precursors returned an EMPTY trace. That gap is the
  reason ObservedFragments exists.

PERFORMANCE
    This extractor is I/O bound, not compute bound. A +/-12 s window spans ~6.5M MS2 events of
    which only ~1.8% survive the isolation-window + mobility gate, while the actual m/z matching is
    ~0.5 ms. Locating the slice used to dominate everything: a binary search over a ~300M-row
    memory-mapped index is ~28 random page faults per call on network storage.

    v1.1.0 replaces that with a per-cycle row-offset index, built once per cache (~0.5 s, ~9 KB)
    and persisted next to the arrays, so it costs nothing on later runs and applies to EVERY cache
    automatically -- no flag, no per-file setup. Measured 10.3x faster (941 -> 91 ms per precursor)
    with bit-identical output.

    What remains is the 1.8% keep rate: events are indexed by cycle but not by isolation window, so
    every precursor still reads all ~12 diaPASEF windows to use one. Partitioning events by window
    is the next big win and would need a cache-format change. Parallelising across precursors also
    helps (the pipeline uses a 14-worker pool).
"""
from __future__ import annotations

import os
import re

import numpy as np

VERSION = "1.4.0"

# ── tunables (env-overridable) ───────────────────────────────────────────────────────────────
PPM = float(os.environ.get("FRAN_XIC_PPM", "15"))
# 0.030 (full width 0.060) — measured, not inherited. A sweep against Spectronaut 21's own
# chromatograms over 1,200 precursors x 7 tolerances shows correlation tracing a clean inverted-U
# that peaks at 0.025 on BOTH levels, with the area ratio crossing 1.0 at ~0.030. Spectronaut's own
# window for the same file is +/-0.03 (its IM extraction plot reports median WIDTH 0.06), so two
# independent routes land on the same number. The previous 0.05 was ~4.6x the measured ion-mobility
# peak width (median 0.0216) and admitted co-eluting ions at other mobilities -- that, not any
# difference in integration philosophy, was the 1.2x area inflation earlier versions documented.
IM_TOL = float(os.environ.get("FRAN_XIC_IM_TOL", "0.030"))
RT_HALF = float(os.environ.get("FRAN_XIC_RT_HALF", "12"))
N_POINTS = int(os.environ.get("FRAN_XIC_POINTS", "32"))
# Isotope channels: M, M+1 ... Five because that is what Spectronaut 21 traces (measured on a real
# export: M+3 present for 18,286 of 18,287 precursors, M+4 for 13,290), and the isotope ENVELOPE
# SHAPE discriminates a real precursor from interference -- truncating it discards part of that
# comparison, worst on larger peptides whose envelope centre of mass sits high. Overridable because
# the tensor's first dimension is N_FRAG + N_MS1; downstream code with a hard-coded row count must
# be updated in step (the Lance trace lane stores n_frag/n_ms1/n_points, so readers can adapt).
N_MS1_CHANNELS = int(os.environ.get("FRAN_XIC_MS1_CHANNELS", "5"))
# Configurable like the other extraction constants. Default stays 6 so existing caches and the
# trace lane keep their shape, but DIA-NN traces 12 fragments per precursor on this data, so any
# comparison against it that caps at 6 is only seeing half the evidence.
N_FRAG_CHANNELS = int(os.environ.get("FRAN_XIC_FRAG_CHANNELS", "6"))
C13 = 1.0033548378

# monoisotopic residue masses (Da)
AA = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841,
      "T": 101.04768, "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293,
      "D": 115.02694, "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049,
      "H": 137.05891, "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931}
H2O, PROTON = 18.010565, 1.007276
CARBAMIDOMETHYL = 57.021464

_ION_RE = re.compile(r"^([yb])(\d+)$")


# ── fragment m/z from sequence (self-contained; no external fragment library) ─────────────────
def by_ions(seq: str, max_charge: int = 1, mods: dict[int, float] | None = None):
    """b/y ions as (mz, kind, series_number, charge) with kind in {'b','y'}. Cysteine is treated
    as carbamidomethylated unless `mods` says otherwise (the near-universal DIA convention)."""
    n = len(seq)
    masses = [AA.get(a, 0.0) for a in seq]
    for i, a in enumerate(seq):
        if a == "C":
            masses[i] += CARBAMIDOMETHYL
    if mods:
        for pos, dm in mods.items():
            if 0 <= pos < n:
                masses[pos] += dm
    out = []
    run = 0.0
    for i in range(n - 1):                       # b1..b(n-1)
        run += masses[i]
        for z in range(1, max_charge + 1):
            out.append(((run + z * PROTON) / z, "b", i + 1, z))
    run = 0.0
    for i in range(n - 1):                       # y1..y(n-1)
        run += masses[n - 1 - i]
        for z in range(1, max_charge + 1):
            out.append(((run + H2O + z * PROTON) / z, "y", i + 1, z))
    return out


def ion_mz(seq: str, label: str, charge: int = 1):
    """m/z of a NAMED ion ('y8', 'b4') on any sequence. This is what lets a decoy reuse the
    target's observed ion identities instead of a different selection rule."""
    m = _ION_RE.match(str(label or "").strip())
    if not m:
        return None
    kind, num = m.group(1), int(m.group(2))
    for mz, k, n, z in by_ions(seq, 1):
        if k == kind and n == num and z == 1:
            return (mz + (charge - 1) * PROTON) / charge
    return None


def top6_by_mz(seq: str) -> np.ndarray:
    """FALLBACK ONLY — 4 y + 2 b singly-charged ions, highest m/z first. Kept so precursors absent
    from the spectrum lane still extract, but see the module docstring: these are mostly NOT the
    ions the instrument actually measured. Prefer ObservedFragments."""
    gen = [(mz, k, n) for mz, k, n, z in by_ions(seq, 1) if z == 1]
    ys = sorted([mz for mz, k, n in gen if k == "y" and 2 <= n <= len(seq) - 1])[::-1]
    bs = sorted([mz for mz, k, n in gen if k == "b" and 2 <= n <= len(seq) - 1])[::-1]
    return np.array((ys[:4] + bs[:2])[:N_FRAG_CHANNELS], dtype=float)


def mirror_ions(decoy_seq: str, ions: list[tuple[str, int]]) -> np.ndarray:
    """Re-compute an ion series [(label, charge), ...] on a decoy sequence — the leak-safe way to
    give a decoy the same fragment SELECTION as its target."""
    out = [ion_mz(decoy_seq, lab, z) for lab, z in ions]
    return np.array([x for x in out if x], dtype=float)


# ── the observed fragments, read from FRAN's spectrum Lance lane ──────────────────────────────
class ObservedFragments:
    """The ions actually measured for each precursor, from a `<search>.lance` spectrum dataset.

        obs = ObservedFragments(path)
        mzs, ions = obs.get("SAMPLERPEPTIDEK", 2)      # -> (m/z array, [(label, charge), ...])

    Ordered by measured relative intensity, so [:6] is the six strongest — the opposite of, and the
    correction to, top6_by_mz()."""

    def __init__(self, lance_path: str, limit: int = N_FRAG_CHANNELS):
        import lance
        t = lance.dataset(lance_path).to_table()
        seq = t["stripped_seq"].to_pylist(); chg = t["charge"].to_pylist()
        mz = t["frg_mz"].to_pylist(); ion = t["frg_ion"].to_pylist()
        fz = t["frg_charge"].to_pylist(); ri = t["frg_measured_relint"].to_pylist()
        lo = t["frg_loss"].to_pylist() if "frg_loss" in t.schema.names else [None] * len(seq)
        self._map: dict[tuple[str, int], tuple[np.ndarray, list]] = {}
        for i in range(len(seq)):
            if not seq[i] or not mz[i]:
                continue
            m = list(mz[i]); lab = list(ion[i] or []); z = list(fz[i] or []); r = list(ri[i] or [])
            order = np.argsort([-(r[j] if j < len(r) and r[j] is not None else 0)
                                for j in range(len(m))])[:limit]
            L = list(lo[i] or [])
            self._map[(seq[i], int(chg[i] or 0))] = (
                np.array([float(m[j]) for j in order], dtype=float),
                [(str(lab[j]) if j < len(lab) and lab[j] else None,
                  int(z[j]) if j < len(z) and z[j] else 1,
                  str(L[j]) if j < len(L) and L[j] else "noloss") for j in order])

    def __len__(self):
        return len(self._map)

    def get(self, seq: str, charge: int):
        """(m/z array, [(ion_label, charge, neutral_loss), ...]) ordered by measured intensity."""
        return self._map.get((seq, int(charge)), (None, None))


def spectronaut_trace_label(ion: str, charge: int = 1, loss: str = "noloss") -> str | None:
    """Spectronaut names its exported XIC traces `y5+`, `y19++`, `b6+ -H2O`, `y6+ -NH3`.

    Pair on THIS, never on the bare ion label: 5,756 of 108,852 observed fragments in a real run
    carry a neutral loss, and 34.2% of precursors contain the same ion label more than once
    (y5, y5-NH3, y5-H2O differ only by loss and m/z). Keying on the label alone silently compares a
    loss-ion chromatogram against the plain ion's — it mispaired 4,187 channels before this existed
    and depressed the measured MS2 agreement."""
    if not ion:
        return None
    key = f"{ion}{'+' * max(int(charge or 1), 1)}"
    L = (loss or "noloss").strip()
    return key if L.lower() in ("noloss", "none", "") else f"{key} -{L}"


# ── the extractor ─────────────────────────────────────────────────────────────────────────────
class Cache:
    ms2_rt_corrected = False
    """Memory-mapped view of one run's cached spectra."""

    def __init__(self, d: str):
        self.rt = np.load(f"{d}/rt_values.npy")
        self.mob = np.load(f"{d}/mobility_values.npy")
        self.frames_per_cycle = int(np.load(f"{d}/meta.npy")[0])
        mm = lambda n: np.load(f"{d}/{n}.npy", mmap_mode="r")  # noqa: E731
        self.m_mz, self.m_int = mm("ms1_mz"), mm("ms1_int")
        self.m_scn, self.m_cyc = mm("ms1_scan"), mm("ms1_cycle_idx")
        self.e_mz, self.e_int = mm("ev_mz"), mm("ev_int")
        self.e_scn, self.e_cyc = mm("ev_scan"), mm("cycle_idx")
        self.e_lo, self.e_hi = mm("iso_lo"), mm("iso_hi")
        last = int(np.asarray(self.m_cyc).max())
        first_frame = np.clip(np.arange(last + 1) * self.frames_per_cycle, 0, len(self.rt) - 1)
        self.cycle_rt = self.rt[first_frame]
        # OPTIONAL OVERRIDE — cycle_rt_ms2.npy, written by build_thermo_xic_cache.py.
        #
        # The line above is the -0.295 s MS2 defect in one expression: every event in a cycle is
        # stamped with the time of that cycle's FIRST frame, which is the MS1 frame, so fragment
        # traces are labelled early by however long the MS2 scans take. On diaPASEF that is about a
        # quarter cycle. On the Thermo cache it is FAR worse -- measured 1.176 s on a 50-scan K562
        # cycle -- because a long DIA cycle spends most of its time in MS2.
        #
        # Where the cache can state the real per-cycle MS2 time (mzparquet gives every scan its own
        # rt), use it. Fragments are what this extractor exists to produce and what any XIC
        # comparison is about, so the trace axis follows MS2. The MS1 isotope channels in the same
        # tensor are then stamped late by the same lag; that is a known consequence of one shared
        # time axis, not a second bug, and it is why this is opt-in per cache rather than assumed.
        # PER-CHANNEL TIME BASES. Within a cycle the MS1 scan happens FIRST and the MS2 windows
        # follow, so the two channel groups are genuinely sampled at different times -- by a median
        # 1.176 s on this K562 method, and 0.795-1.679 s across methods. One shared axis therefore
        # cannot be right for both: measured against DIA-NN, stamping everything with the MS2 time
        # gives MS2 r 0.843 / MS1 r 0.803, while stamping everything with the MS1 frame time gives
        # MS2 r 0.739 / MS1 r 0.951. Neither is a good trade; keeping both axes gets 0.843 and 0.951
        # at once. `cycle_rt` stays the MS2/default axis so existing callers are unchanged.
        self.cycle_rt_ms1 = self.cycle_rt
        self.cycle_rt_ms2 = self.cycle_rt
        _ms1_rt, _ms2_rt = f"{d}/cycle_rt_ms1.npy", f"{d}/cycle_rt_ms2.npy"
        if os.path.exists(_ms1_rt):
            c1a = np.load(_ms1_rt)
            if len(c1a) >= len(self.cycle_rt):
                self.cycle_rt_ms1 = c1a[:len(self.cycle_rt)]
        if os.path.exists(_ms2_rt):
            cr2 = np.load(_ms2_rt)
            if len(cr2) >= len(self.cycle_rt):
                self.cycle_rt_ms2 = cr2[:len(self.cycle_rt)]
                self.cycle_rt = self.cycle_rt_ms2
                self.ms2_rt_corrected = True
        # A gradient in MINUTES never reaches 200; in SECONDS it always does.
        self.rt_in_seconds = float(self.cycle_rt.max()) > 200.0
        self._ev_off, self._ms1_off = self._cycle_offsets(d)

    # ── cycle -> row offset index ────────────────────────────────────────────────────────────
    def _cycle_offsets(self, d: str):
        """Row range of every cycle, so slicing costs a lookup instead of a binary search.

        WHY: the event arrays are sorted by cycle but hold ~300M rows and are memory-mapped, often
        on network storage. `searchsorted` over them is ~28 random page faults PER CALL, and that
        was ~2/3 of this extractor's total runtime — far more than the actual signal work (~0.5 ms
        of m/z matching). The whole index is one int64 per cycle: ~9 KB for a 21-minute gradient.

        Built once per cache (~0.5 s) and cached to disk next to the arrays so every later run is
        free. Falls back to in-memory when the cache directory is read-only, so a shared or
        node-local cache still benefits. Measured 10.3x faster with bit-identical output."""
        n = len(self.cycle_rt)
        names = ("cycle_offsets_ev.npy", "cycle_offsets_ms1.npy")
        try:
            ev = np.load(os.path.join(d, names[0]))
            ms1 = np.load(os.path.join(d, names[1]))
            if len(ev) == n + 1 and len(ms1) == n + 1:
                return ev, ms1
        except Exception:  # noqa: BLE001 - a missing/stale index just means we rebuild it
            pass
        grid = np.arange(n + 1)
        ev = np.searchsorted(self.e_cyc, grid, "left")
        ms1 = np.searchsorted(self.m_cyc, grid, "left")
        for name, arr in zip(names, (ev, ms1)):
            try:
                np.save(os.path.join(d, name), arr)
            except OSError:
                pass                      # read-only cache: keep it in memory, still 10x faster
        return ev, ms1

    def rt_to_native(self, rt_minutes: float) -> float:
        return rt_minutes * 60.0 if self.rt_in_seconds else rt_minutes

    def cycle_window(self, rt_native: float):
        c = np.where(np.abs(self.cycle_rt - rt_native) <= RT_HALF)[0]
        return (int(c.min()), int(c.max())) if len(c) else (None, None)

    def rt_axis(self, rt_native: float, n: int = N_POINTS, minutes: bool = True,
                channel: str = "ms2"):
        """RT values of the resampled trace returned by `extract`.

        `channel` selects the time base: 'ms2' for the fragment rows (0 .. N_FRAG_CHANNELS-1) and
        'ms1' for the isotope rows that follow them. They differ by however long a cycle spends in
        MS2 before the next MS1 -- about 1.2 s on a 50-window Orbitrap method. Asking for the wrong
        one costs ~0.15 in correlation, which is larger than most differences this tool is used to
        measure."""
        c0, c1 = self.cycle_window(rt_native)
        if c0 is None:
            return None
        base = self.cycle_rt_ms1 if channel == "ms1" else self.cycle_rt_ms2
        ax = np.linspace(base[c0], base[c1], n)
        return ax / 60.0 if (minutes and self.rt_in_seconds) else ax

    def extract(self, precursor_mz: float, charge: int, fragment_mz, rt_native: float,
                im: float, normalize: bool | str = True) -> np.ndarray:
        """[9, N_POINTS] = 6 fragment channels + 3 MS1 isotope channels, resampled onto a common
        axis. Rows beyond the fragments supplied stay zero. `im` is REQUIRED for diaPASEF: the
        mobility gate is what separates co-isolated precursors, and passing a wrong value silently
        returns near-empty traces."""
        T = np.zeros((N_FRAG_CHANNELS + N_MS1_CHANNELS, N_POINTS), np.float32)
        c0, c1 = self.cycle_window(rt_native)
        if c0 is None:
            return T
        n_cycles = c1 - c0 + 1
        raw = np.zeros((N_FRAG_CHANNELS + N_MS1_CHANNELS, n_cycles))

        lo, hi = int(self._ev_off[c0]), int(self._ev_off[c1 + 1])
        if hi > lo:
            emz = np.asarray(self.e_mz[lo:hi]); eint = np.asarray(self.e_int[lo:hi])
            ecyc = np.asarray(self.e_cyc[lo:hi]) - c0
            escn = np.asarray(self.e_scn[lo:hi])
            mob = self.mob[np.clip(escn, 0, len(self.mob) - 1)]
            # only fragments from windows that isolated this precursor, at its mobility
            keep = ((np.asarray(self.e_lo[lo:hi]) <= precursor_mz)
                    & (np.asarray(self.e_hi[lo:hi]) >= precursor_mz)
                    & (np.abs(mob - im) <= IM_TOL))
            kmz, kcyc, kint = emz[keep], ecyc[keep], eint[keep]
            for k, fmz in enumerate(np.asarray(fragment_mz)[:N_FRAG_CHANNELS]):
                sel = np.abs(kmz - fmz) <= fmz * PPM * 1e-6
                if sel.any():
                    np.add.at(raw[k], kcyc[sel], kint[sel])

        lo, hi = int(self._ms1_off[c0]), int(self._ms1_off[c1 + 1])
        if hi > lo:
            mmz = np.asarray(self.m_mz[lo:hi]); mint = np.asarray(self.m_int[lo:hi])
            mcyc = np.asarray(self.m_cyc[lo:hi]) - c0
            mscn = np.asarray(self.m_scn[lo:hi])
            mmob = self.mob[np.clip(mscn, 0, len(self.mob) - 1)]
            for j in range(N_MS1_CHANNELS):
                target = precursor_mz + j * C13 / max(charge, 1)
                sel = (np.abs(mmz - target) <= target * PPM * 1e-6) & (np.abs(mmob - im) <= IM_TOL)
                if sel.any():
                    np.add.at(raw[N_FRAG_CHANNELS + j], mcyc[sel], mint[sel])

        if n_cycles >= 2:
            xs = np.linspace(0, 1, n_cycles); xt = np.linspace(0, 1, N_POINTS)
            for i in range(raw.shape[0]):
                T[i] = np.interp(xt, xs, raw[i])
        elif n_cycles == 1:
            T[:, :] = raw[:, :1]
        # NORMALISATION. MS1 and MS2 are not the same magnitude: measured over 18,015 precursors,
        # MS1 is 5.5x louder at the median and louder in 95.1% of cases (p90 18.8x). So a single
        # global max -- `normalize=True`, kept as the default only for backward compatibility --
        # hands MS1 the full range and squashes the FRAGMENT rows to a median peak of 0.18, below
        # 0.1 for a quarter of precursors. The fragments are the identification evidence.
        #   False       raw counts. Correct for STORAGE: normalisation is lossy and cannot be undone.
        #   "per_block" MS1 and MS2 each scaled to their own max, so both use the full range. The
        #               cross-modality ratio this discards is real information -- carry it alongside
        #               as log10(ms1_max/ms2_max) rather than losing it.
        #   True        legacy single global max.
        if normalize == "per_block":
            f = T[:N_FRAG_CHANNELS]; m = T[N_FRAG_CHANNELS:]
            if f.max() > 0:
                f /= f.max()
            if m.max() > 0:
                m /= m.max()
        elif normalize:
            mx = T.max()
            if mx > 0:
                T /= mx
        return T
