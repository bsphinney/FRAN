"""build_xic_trace_lance.py — PILOT builder for the XIC-TRACE lane.

Committed from `/quobyte/proteomics-grp/brett/glendon/build_xic_trace_lance.py`, which is the script
that actually produced the one dataset in `delimp_xic_trace_lane`. It had never been in version
control: `STORAGE_DESIGN.md` and `LANCE_PRIORS_AND_XIC_SPEC.md` both plan around it, and the 6-hour
job that timed out and "wrote nothing" was an untracked script too. Checkpointing cannot be added to
code that is not in the repo, so it lives here now — as-found, apart from the registration path.

⚠️ DO NOT SCALE THIS TO THE CORPUS YET. Three things must be settled first, and the reasons are
recorded in CHANGELOG_xic_extractor.md:

  1. MS2 timestamps run ~0.263 s early. `cycle_rt[c] = rt[c * frames_per_cycle]` stamps every event
     in a cycle with the first (MS1) frame's time. Shape is unaffected; absolute fragment RT is not.
     A corpus built now bakes in a defect that is already known — the avoidable version of the
     mistake this file's own history illustrates.
  2. It uses a DIFFERENT extraction path from `xic_extractor.py`. This calls `build_xic_shard.Cache`
     (Hive-only) for a [9, 32] tensor — 6 fragment + 3 MS1 channels. `xic_extractor.py` is at v1.4.0
     with [11, 32] (5 isotope channels) and IM_TOL 0.030, not 0.05. The two disagree about what a
     trace even is; they must be reconciled before a corpus-scale build.
  3. The 1.8% keep rate. Events are indexed by cycle but not by isolation window, so each precursor
     reads all ~12 diaPASEF windows to use one. Over 1,552 runs that inefficiency is the whole cost
     of the phase.

It also stores far less than the schema in `STORAGE_DESIGN.md` §6 — no `channel_kind[]`, no
`frg_type`/`frg_num`/`frg_charge`, no `cycle_rt[]`, no `apex_cycle`. And `MAXP` caps it at a 6,000
precursor random subsample, so the pilot is a sample of a run, not a run.

Usage (Hive, compute node — never the login node):
    VRUN=<run> VLANCE=<spectrum .lance> VCACHE=<.d valcache> python build_xic_trace_lance.py
"""
import hashlib
import os
import sys
import time

import numpy as np
import pyarrow as pa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/quobyte/proteomics-grp/brett/glendon")

import build_xic_shard as B  # noqa: E402  (Hive-only module)

G = "/quobyte/proteomics-grp/brett/glendon"
OUTDIR = os.environ.get("VOUTDIR", f"{G}/xic_trace_lance")
os.makedirs(OUTDIR, exist_ok=True)
RUN = os.environ["VRUN"]
LANCE = os.environ["VLANCE"]
CACHE = os.environ["VCACHE"]
MAXP = int(os.environ.get("VMAXP", "6000"))

N_CHANNELS, N_CYCLES = 9, 32

t0 = time.time()


def log(m):
    print(f"[{time.time() - t0:5.0f}s] {m}", flush=True)


SCHEMA = pa.schema([
    ("run", pa.string()), ("stripped_seq", pa.string()), ("charge", pa.int16()),
    ("precursor_mz", pa.float32()), ("rt", pa.float32()), ("im", pa.float32()),
    ("q_value", pa.float32()), ("n_frag", pa.int16()),
    ("trace", pa.list_(pa.float32())),   # 288 = 9*32, a flattened [9,32] tensor
])


def content_md5(tbl):
    """Kept local and identical to the pilot's, so md5s recorded by the original run still verify.
    Note it lacks the combine_chunks() canonicalisation that spectrum_lance.content_md5 needed —
    harmless at 6,000 rows (single chunk), but it WILL mis-verify at corpus scale. Fix when scaling."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, tbl.schema) as w:
        w.write_table(tbl)
    return hashlib.md5(sink.getvalue().to_pybytes()).hexdigest()


def main():
    import lance as L

    ds = L.dataset(LANCE)
    t = ds.scanner(columns=["run", "stripped_seq", "charge", "precursor_mz", "rt", "im",
                            "q_value", "global_q_value", "is_decoy", "frg_mz"],
                   filter=f"run = '{RUN}'").to_table()
    seq = t["stripped_seq"].to_pylist(); ch = t["charge"].to_pylist()
    pm = t["precursor_mz"].to_pylist()
    rt = np.array(t["rt"].to_pylist(), float); im = np.array(t["im"].to_pylist(), float)
    q = [x if x else 1 for x in t["q_value"].to_pylist()]
    gq = [x if x else 1 for x in t["global_q_value"].to_pylist()]
    isd = t["is_decoy"].to_pylist(); fmz = t["frg_mz"].to_pylist()

    keep = [i for i in range(len(seq))
            if seq[i] and not isd[i] and min(q[i], gq[i]) < 0.01
            and fmz[i] and len(fmz[i]) >= 4 and np.isfinite(rt[i]) and np.isfinite(im[i])]
    rng = np.random.default_rng(0); rng.shuffle(keep); keep = keep[:MAXP]

    C = B.Cache(CACHE)
    scale = 60.0 if C.rt.max() / max(np.nanmax(rt[keep]), 1e-9) > 30 else 1.0
    log(f"extracting {len(keep)} traces for {RUN} (scale={scale})")

    rows = dict(run=[], stripped_seq=[], charge=[], precursor_mz=[], rt=[], im=[],
                q_value=[], n_frag=[], trace=[])
    nsig = 0
    for i in keep:
        T = C.xic_tensor(float(pm[i]), int(ch[i]) if ch[i] else 2,
                         np.asarray(fmz[i], float), float(rt[i]) * scale, float(im[i]))
        rows["run"].append(RUN); rows["stripped_seq"].append(str(seq[i]))
        rows["charge"].append(int(ch[i]) if ch[i] else 2)
        rows["precursor_mz"].append(float(pm[i])); rows["rt"].append(float(rt[i]))
        rows["im"].append(float(im[i])); rows["q_value"].append(float(q[i]))
        rows["n_frag"].append(int(min(len(fmz[i]), 6)))
        rows["trace"].append(T.reshape(-1).astype(np.float32))
        nsig += int(T.max() > 0)

    tbl = pa.table({
        "run": pa.array(rows["run"]), "stripped_seq": pa.array(rows["stripped_seq"]),
        "charge": pa.array(rows["charge"], pa.int16()),
        "precursor_mz": pa.array(rows["precursor_mz"], pa.float32()),
        "rt": pa.array(rows["rt"], pa.float32()), "im": pa.array(rows["im"], pa.float32()),
        "q_value": pa.array(rows["q_value"], pa.float32()),
        "n_frag": pa.array(rows["n_frag"], pa.int16()),
        "trace": pa.array([x.tolist() for x in rows["trace"]], pa.list_(pa.float32())),
    }).cast(SCHEMA)

    path = f"{OUTDIR}/{RUN}.lance"
    md5 = content_md5(tbl)
    dsw = L.write_dataset(tbl, path, mode="overwrite")
    log(f"wrote {path}  rows={tbl.num_rows}  has-signal {100 * nsig / max(len(keep), 1):.0f}%  "
        f"md5={md5[:12]}  v={dsw.version}")

    rb = L.dataset(path).to_table().cast(SCHEMA)
    ok = content_md5(rb) == md5
    tr0 = np.asarray(rb["trace"][0].as_py(), np.float32).reshape(N_CHANNELS, N_CYCLES)
    log(f"VALIDATE readback md5 match: {ok}  trace shape {tr0.shape}  fragA apex {tr0[:6].max():.3f}")

    # Registration goes through the shared registry module so this dataset records WHICH extractor
    # produced it. Without that, "does this carry the 0.263 s MS2 shift?" has no answer but a rebuild.
    try:
        import psycopg2  # noqa: F401
        import xic_trace_lance as XTL
        from refresh_leaderboards import _token
        import psycopg2 as pg
        cx = pg.connect(host="pgfarm.library.ucdavis.edu",
                        dbname="uc-davis-genome-center-proteomics-core/delimp",
                        user=os.environ.get("DELIMP_PG_USER", "genome-proteomics-service-account"),
                        password=_token(), sslmode="require", connect_timeout=30)
        XTL.ensure_registry(cx)
        XTL.register(cx, RUN, path, tbl.num_rows, md5, int(dsw.version),
                     n_channels=N_CHANNELS, n_cycles=N_CYCLES)
        cx.close()
        log("registered in delimp_xic_trace_lane (DB)")
    except Exception as e:  # noqa: BLE001
        import json
        reg = dict(lance_path=path, run=RUN, n_precursors=tbl.num_rows, content_md5=md5,
                   lance_version=int(dsw.version), n_channels=N_CHANNELS, n_cycles=N_CYCLES)
        with open(f"{OUTDIR}/trace_lane_registry.jsonl", "a") as f:
            f.write(json.dumps(reg) + "\n")
        log(f"DB unreachable ({str(e)[:60]}); registered in jsonl")
    log("TRACE_LANE_DONE")


if __name__ == "__main__":
    main()
