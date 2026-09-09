"""Do 1-substitution CANDIDATES preferentially land in paralog-dense families, as hair shows?

dataanalysis-16 found an asymmetry in the hair cohort's high-complexity stratum: EXACT reference
matches are predominantly non-keratin (desmosome/follicle proteins, real hair biology) while
1-SUBSTITUTION CANDIDATES are overwhelmingly keratin -- the paralog-dense families -- even where the
chance-match FDR is 0.000. If crane shows it too, "candidates preferentially land in paralog-dense
space" is a general property of 1-substitution de novo matching rather than a hair phenomenon.

Tested here against the SAME gene-annotated reference (UP000005640_9606) so the two cohorts are
directly comparable. Crane is avian, so most human matches are the human hair positive control plus
conserved proteins -- which is fine for this question, since it asks about the RELATIVE paralog
density of exact-match versus candidate space, not about species assignment.

The sharpest form of the test does not depend on gene families at all: compare the mean
n_paralog_neighbours of peptides hit exactly against those hit by one substitution. Family
composition differs between species; paralog density is the mechanism.
"""
import collections
import csv
import math
import sys

sys.path.insert(0, "/quobyte/proteomics-grp/brett/zach_hair_gvp")
from denovo_paralog import build_index, load_reference, paralog_neighbours  # noqa: E402

REF = "/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta"
CRANE = "/quobyte/proteomics-grp/brett/teeth_pilot2/crane_peptides.txt"
IL = str.maketrans("I", "L")


def entropy(p):
    c = collections.Counter(p)
    n = len(p)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


print("loading reference (missed=1, matching the paralog profile)...", file=sys.stderr)
ref = load_reference(REF, missed=1)
idx = build_index(ref.keys())
print(f"  {len(ref):,} peptides, {len({g for gs in ref.values() for g in gs}):,} genes",
      file=sys.stderr)

peps = sorted({l.strip().upper().translate(IL) for l in open(CRANE) if l.strip()})
hi = [p for p in peps if entropy(p) >= 3.2]
print(f"crane peptides {len(peps):,}; high-complexity {len(hi):,}\n", file=sys.stderr)


def classify(pool):
    exact, cand = [], []
    for q in pool:
        if q in ref:
            exact.append(q)
            continue
        h = len(q) // 2
        for c in sorted(set(idx.get((len(q), 0, q[:h]), ())) | set(idx.get((len(q), 1, q[h:]), ()))):
            if sum(1 for a, b in zip(q, c) if a != b) == 1:
                cand.append((q, c))
                break
    return exact, cand


def fam(g):
    return "KRTAP" if g.startswith("KRTAP") else ("KRT" if g.startswith("KRT") else "other")


for label, pool in (("HIGH-COMPLEXITY ONLY (H>=3.2)", hi), ("ALL CRANE PEPTIDES", peps)):
    exact, cand = classify(pool)
    print(f"### {label}   n={len(pool):,}")
    print(f"  exact reference matches : {len(exact):,}")
    print(f"  1-substitution candidates: {len(cand):,}")
    if not exact or not cand:
        print("  (insufficient for the comparison)\n")
        continue

    # --- family composition, the form dataanalysis-16 reported ---
    def famcount(genesets):
        c = collections.Counter()
        for gs in genesets:
            c[fam(sorted(gs)[0]) if gs else "other"] += 1
        return c

    ce = famcount([ref.get(q, set()) for q in exact])
    cc = famcount([ref.get(c, set()) for _, c in cand])
    te, tc = sum(ce.values()), sum(cc.values())
    print(f"  {'':<22}{'exact':>10}{'candidate':>12}")
    for f in ("KRT", "KRTAP", "other"):
        print(f"  {f:<22}{100*ce[f]/te:>9.1f}%{100*cc[f]/tc:>11.1f}%")

    # --- the mechanism, independent of family labels ---
    pe = [paralog_neighbours(q, idx, ref)["n_paralog_neighbours"] for q in exact]
    pc = [paralog_neighbours(c, idx, ref)["n_paralog_neighbours"] for _, c in cand]
    me = sum(pe) / len(pe)
    mc = sum(pc) / len(pc)
    print(f"\n  mean paralog neighbours, EXACT-matched refs    : {me:.3f}")
    print(f"  mean paralog neighbours, CANDIDATE-matched refs : {mc:.3f}")
    print(f"  enrichment in candidate space                  : "
          f"{mc/me if me else float('inf'):.2f}x")
    fe = 100 * sum(1 for x in pe if x) / len(pe)
    fc = 100 * sum(1 for x in pc if x) / len(pc)
    print(f"  with >=1 paralog neighbour: exact {fe:.1f}%  candidate {fc:.1f}%\n")
