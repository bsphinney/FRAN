"""reconcile_raw_files.py -- copies of the SAME run (raw_basename) in raw_files that disagree.

One acquisition reaches raw_files once per path it was seen at (project drive, R:\\ archive,
FRAN_diann staging, ...), so a run has several rows. They should agree on what the run IS. 370
runs do not agree on `platform`, and 1,382 more disagree on `acquisition_method`.

Measured cause: an Orbitrap run (every copy carries instrument_model 'Orbitrap Exploris 480')
has one copy labelled platform='timstof' / acquisition_method='diaPASEF'. That is the ingest's
fallback GUESS (corpus_ingest._platform_from_disk / _acq_for), written when a Spectronaut report
had no raw header to read -- not a measurement.

PROPOSED RULE (not applied unless you pass --apply, and then only rule 1):
  1. platform: instrument_model is the authority, and it is a property of the RUN, so any copy's
     model speaks for all copies: if the copies that carry a recognised model all imply ONE
     platform (instrument_labels.platform_for_model), every copy whose platform differs is set to
     it, including a sibling with no model of its own. Runs whose models imply two platforms, or
     that have no recognised model on any copy, are listed by name and left alone.
  2. acquisition_method: REPORT ONLY. Which copy's value is right needs the raw header; this script
     lists the conflicts and proposes the value held by the copy that also has a sniffed
     instrument_model, but does not write it.
  3. Everything else (file size, md5, acquisition_date) is reported only.

raw_files is 44k rows, so it is read whole; writes are batched by raw_path, never by table.

    python reconcile_raw_files.py            # DRY RUN
    python reconcile_raw_files.py --apply    # rule 1 only; owner only
"""
from __future__ import annotations

import sys
from collections import defaultdict

from _common import base_parser, chunks, connect, show_examples
from instrument_labels import platform_for_model


def main():
    ap = base_parser(__doc__.split("\n\n")[0])
    a = ap.parse_args()
    conn, cur = connect(a.apply)
    cur.execute("""SELECT raw_basename, raw_path, platform, instrument_model, acquisition_method,
                          acquisition_date, file_size_bytes, md5 FROM raw_files""")
    runs = defaultdict(list)
    for r in cur.fetchall():
        runs[r[0]].append(r[1:])

    plat_conf = acq_conf = other_conf = 0
    fix, plat_examples, acq_examples, fixed_runs, unresolved = [], [], [], set(), []
    for base, rows in runs.items():
        if len(rows) < 2:
            continue
        plats = {r[1] for r in rows}
        if len(plats) > 1:
            plat_conf += 1
            implied = {platform_for_model(r[2]) for r in rows if r[2]} - {None}
            if len(implied) != 1:
                unresolved.append((base, sorted(plats), sorted({r[2] for r in rows if r[2]}) or "no model on any copy"))
            else:
                want = next(iter(implied))
                for path, plat, model, *_ in rows:
                    if plat != want:
                        fix.append((path, plat, want)); fixed_runs.add(base)
                        if len(plat_examples) < 3:
                            plat_examples.append(f"{base}: {path[-50:]!r} platform {plat!r} -> {want!r} "
                                                 f"(this copy's model {model!r}; run implies {want})")
        if len({r[3] for r in rows if r[3]}) > 1:
            acq_conf += 1
            if len(acq_examples) < 3:
                acq_examples.append(f"{base}: acquisition_method values {sorted({r[3] for r in rows if r[3]})}")
        if any(len({r[i] for r in rows if r[i] is not None}) > 1 for i in (4, 5, 6)):
            other_conf += 1

    if a.apply:
        for batch in chunks(fix, 500):
            for path, old, want in batch:
                # `AND platform = old`: a concurrent change to the row is not clobbered
                cur.execute("UPDATE raw_files SET platform = %s WHERE raw_path = %s AND platform = %s", (want, path, old))
            conn.commit()

    print("reconcile_raw_files: " + ("APPLIED rule 1" if a.apply else "DRY RUN (nothing written)"))
    print(f"  runs with >1 copy                              : {sum(1 for r in runs.values() if len(r) > 1):,}")
    print(f"  runs whose copies disagree on platform         : {plat_conf:,}")
    print(f"  copies rule 1 would re-label (model decides)   : {len(fix):,}")
    print(f"  platform-conflict runs rule 1 cannot settle    : {len(unresolved):,}")
    for base, plats, models in unresolved:
        print(f"     {base}: platforms {plats}, models {models}")
    print(f"  runs whose copies disagree on acquisition_method: {acq_conf:,}   (report only)")
    print(f"  runs disagreeing on date / size / md5          : {other_conf:,}   (report only)")
    show_examples("platform re-label", plat_examples)
    show_examples("acquisition_method conflict", acq_examples)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
