"""save_contaminant_flags.py -- before recompute_is_contaminant.py --apply, save the flag it will change.

READ-ONLY. Writes one gzip TSV of (id, search_id, old is_contaminant) for every delimp_proteins row
whose flag the new rule would flip. That file is the whole undo: restoring is
    UPDATE delimp_proteins SET is_contaminant = <old> WHERE id = <id>
for each line (contaminant_class is a new column; undoing it is setting it back to NULL).

A full pg_dump would also cover this, but it pulls the whole database across the wire uncompressed
(about 600 GB, charged to the Library); this pulls a few megabytes.

    python save_contaminant_flags.py --out /path/contaminant_flags_before.tsv.gz
"""
import gzip
import os
import sys

from _common import base_parser, chunks, connect, all_search_ids
from recompute_is_contaminant import NEW_RULE


def main():
    ap = base_parser(__doc__)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.path.exists(a.out):
        sys.exit(f"refusing to overwrite {a.out}")
    conn, cur = connect(False)
    ids = all_search_ids(cur)
    n = to_true = to_false = 0
    part = a.out + ".part"
    with gzip.open(part, "wt") as f:
        f.write("id\tsearch_id\told_is_contaminant\n")
        for i, batch in enumerate(chunks(ids, 25)):
            cur.execute(f"""SELECT p.id, p.search_id, p.is_contaminant FROM delimp_proteins p
                             WHERE p.search_id = ANY(%s::uuid[])
                               AND p.is_contaminant IS DISTINCT FROM {NEW_RULE}""", (batch,))
            for pid, sid, old in cur:
                f.write(f"{pid}\t{sid}\t{'' if old is None else int(old)}\n")
                n += 1
                if old:
                    to_false += 1
                else:
                    to_true += 1
            if i % 20 == 0:
                print(f"  {min((i + 1) * 25, len(ids)):,}/{len(ids):,} searches, {n:,} rows saved", flush=True)
    os.replace(part, a.out)
    print(f"save_contaminant_flags: {n:,} rows saved to {a.out} "
          f"({to_false:,} now flagged that the rule clears, {to_true:,} unflagged that it flags), "
          f"{len(ids):,} searches read")


if __name__ == "__main__":
    main()
