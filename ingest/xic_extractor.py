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
This module never opens a database or a credential file.

CACHE FORMAT
    A directory of .npy arrays produced from the vendor .d (see the pipeline's cache builder):
    rt_values, mobility_values, meta, ms1_{mz,int,scan,cycle_idx}, ev_{mz,int,scan},
    cycle_idx, iso_{lo,hi}. Arrays are memory-mapped, so a cache far larger than RAM is fine.

VERIFICATION (v1.0.0, 2026-07-27)
---------------------------------
Validated against Spectronaut 21's own exported chromatograms — its "All XIC" SQLite export for the
same run (11May2026_DIA_60spd_VER_185_S2-B5_1_21766, diaPASEF, dog + yeast entrapment), ingested
into FRAN's XIC Lance lane. Both sides describe the same acquisition, so any disagreement is real.
Nothing below is fitted; 400 precursors sampled per test.

  Summed MS2 profile, per precursor, using the observed fragments:
      Pearson r   median 0.915   (78% > 0.8, 4.5% < 0.5)
      apex RT     median 0.55 s  (74% within one 1.1 s acquisition cycle)
      empty traces 0 / 400
  Ion-for-ion, each fragment against Spectronaut's trace for that same ion (2,325 pairs):
      Pearson r   median 0.833   apex median 0.59 s   (66% within one cycle)
  Intensity, raw counts both sides (2,312 pairs):
      area ours/SN   median 1.238   (47% within +/-20%, 81% within 2x)
      peak ours/SN   median 0.886

  KNOWN SYSTEMATIC: more area but a lower apex means our peaks are slightly BROADER than
  Spectronaut's. Expected — Spectronaut integrates within fitted peak boundaries, while this
  extractor sums everything passing the m/z + mobility gate across the whole RT window, so more
  shoulder and interference is included. Shape and position agree; treat absolute areas as
  comparable to ~1.2x, not interchangeable.

  For contrast, the same test with top6_by_mz() instead of the observed fragments: r median 0.679,
  apex median 1.33 s, and 108 of 400 precursors returned an EMPTY trace. That gap is the reason
  ObservedFragments exists.

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

VERSION = "1.1.0"

# ── tunables (env-overridable) ───────────────────────────────────────────────────────────────
PPM = float(os.environ.get("FRAN_XIC_PPM", "15"))
IM_TOL = float(os.environ.get("FRAN_XIC_IM_TOL", "0.05"))
RT_HALF = float(os.environ.get("FRAN_XIC_RT_HALF", "12"))
N_POINTS = int(os.environ.get("FRAN_XIC_POINTS", "32"))
N_MS1_CHANNELS = 3          # monoisotope, M+1, M+2
N_FRAG_CHANNELS = 6
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
        self._map: dict[tuple[str, int], tuple[np.ndarray, list]] = {}
        for i in range(len(seq)):
            if not seq[i] or not mz[i]:
                continue
            m = list(mz[i]); lab = list(ion[i] or []); z = list(fz[i] or []); r = list(ri[i] or [])
            order = np.argsort([-(r[j] if j < len(r) and r[j] is not None else 0)
                                for j in range(len(m))])[:limit]
            self._map[(seq[i], int(chg[i] or 0))] = (
                np.array([float(m[j]) for j in order], dtype=float),
                [(str(lab[j]) if j < len(lab) and lab[j] else None,
                  int(z[j]) if j < len(z) and z[j] else 1) for j in order])

    def __len__(self):
        return len(self._map)

    def get(self, seq: str, charge: int):
        return self._map.get((seq, int(charge)), (None, None))


# ── the extractor ─────────────────────────────────────────────────────────────────────────────
class Cache:
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

    def rt_axis(self, rt_native: float, n: int = N_POINTS, minutes: bool = True):
        """RT values of the resampled trace returned by `extract` — needed to compare against any
        other tool's chromatogram."""
        c0, c1 = self.cycle_window(rt_native)
        if c0 is None:
            return None
        ax = np.linspace(self.cycle_rt[c0], self.cycle_rt[c1], n)
        return ax / 60.0 if (minutes and self.rt_in_seconds) else ax

    def extract(self, precursor_mz: float, charge: int, fragment_mz, rt_native: float,
                im: float, normalize: bool = True) -> np.ndarray:
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
        if normalize:
            mx = T.max()
            if mx > 0:
                T /= mx
        return T
