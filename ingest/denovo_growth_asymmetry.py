"""Do 1-substitution matches scale with reference size faster than exact matches do?

dataanalysis-16's diagnostic, and it is a good one: across a 46x reference expansion the hair
cohort's exact matches grew 1.55x while 1-substitution matches grew 2.08x -- 1-sub matches are ~34%
more sensitive to reference size. The logic is that a real biological match should not care much how
many other sequences you added, whereas a near-miss should scale with the density of the space it
is searching. Chance scales; signal does not.

Tested here on crane as an independent cohort. Reference size is varied by missed cleavages
(0/1/2), which is the same lever they used, on the same UP000005640_9606.

Needs no decoy at all, which makes it a second independent line on the question the decoy nulls
answer -- and unlike a decoy it cannot be broken by choosing a bad decoy construction.
"""
import collections
import math
import sys

sys.path.insert(0, "/quobyte/proteomics-grp/brett/zach_hair_gvp")
from denovo_paralog import build_index, load_reference  # noqa: E402

REF = "/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta"
CRANE = "/quobyte/proteomics-grp/brett/teeth_pilot2/crane_peptides.txt"
IL = str.maketrans("I", "L")


def entropy(p):
    c = collections.Counter(p)
    n = len(p)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def counts(pool, refset, idx):
    ex = sub = 0
    for q in pool:
        if q in refset:
            ex += 1
            continue
        h = len(q) // 2
        for c in set(idx.get((len(q), 0, q[:h]), ())) | set(idx.get((len(q), 1, q[h:]), ())):
            if sum(1 for a, b in zip(q, c) if a != b) == 1:
                sub += 1
                break
    return ex, sub


peps = sorted({l.strip().upper().translate(IL) for l in open(CRANE) if l.strip()})
hi = [p for p in peps if entropy(p) >= 3.2]
print(f"crane {len(peps):,} peptides, {len(hi):,} high-complexity\n", file=sys.stderr)

rows = []
for missed in (0, 1, 2):
    ref = load_reference(REF, missed=missed)
    idx = build_index(ref.keys())
    e_all, s_all = counts(peps, ref, idx)
    e_hi, s_hi = counts(hi, ref, idx)
    rows.append((missed, len(ref), e_all, s_all, e_hi, s_hi))
    print(f"  missed={missed}: {len(ref):,} ref peptides", file=sys.stderr)

print(f"\n{'missed':>7}{'ref peps':>12}{'exact':>9}{'1-sub':>8}"
      f"{'exact(hi)':>11}{'1-sub(hi)':>11}")
print("-" * 60)
for m, n, ea, sa, eh, sh in rows:
    print(f"{m:>7}{n:>12,}{ea:>9}{sa:>8}{eh:>11}{sh:>11}")

m0, n0, ea0, sa0, eh0, sh0 = rows[0]
m2, n2, ea2, sa2, eh2, sh2 = rows[-1]
print(f"\nreference grew {n2/n0:.2f}x  ({n0:,} -> {n2:,})")
print(f"\n{'':<22}{'growth':>9}{'':>4}{'interpretation'}")
print("-" * 68)
for lab, a, b in (("exact, all peptides", ea0, ea2),
                  ("1-sub, all peptides", sa0, sa2),
                  ("exact, high-complexity", eh0, eh2),
                  ("1-sub, high-complexity", sh0, sh2)):
    print(f"{lab:<22}{b/a if a else float('nan'):>8.2f}x")

if ea0 and sa0:
    r_all = (sa2 / sa0) / (ea2 / ea0)
    print(f"\n1-sub / exact growth ratio, all peptides    : {r_all:.2f}"
          f"   ({100*(r_all-1):+.0f}% more sensitive to reference size)")
if eh0 and sh0:
    r_hi = (sh2 / sh0) / (eh2 / eh0)
    print(f"1-sub / exact growth ratio, high-complexity : {r_hi:.2f}"
          f"   ({100*(r_hi-1):+.0f}%)")
print("\nhair reported 1.55x exact vs 2.08x 1-sub across a 46x expansion = ratio 1.34.")
print("A ratio > 1 means 1-sub matches scale with reference density -- the signature of chance.")
