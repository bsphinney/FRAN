"""The 4th cell of the cross was degenerate. Fix it with an independent decoy construction.

interior_reverse is an INVOLUTION: applying it twice returns the original. So reversing BOTH the
peptides and the corpus cancels, and "decoy peptides vs reversed corpus" is secretly the TARGET
cell -- which is exactly what the numbers showed (7.24% vs a 7.30% target, 5,041 exact matches vs
5,082). A genuine chance floor needs the two axes decoyed by DIFFERENT operations, so this uses
interior SHUFFLE on the peptides against the interior-REVERSED corpus.
"""
import collections
import itertools
import random

D = "/private/tmp/claude-501/-Users-brettphinney-Documents-FRAN/a83fb84f-dba3-4ccf-b020-f03f075b5735/scratchpad/"
IL = str.maketrans("I", "L")

BASES = "TCAG"
AAS = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
CODON = {}
for i, c in enumerate(itertools.product(BASES, repeat=3)):
    CODON.setdefault(AAS[i], []).append("".join(c))

M = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841, "T": 101.04768,
     "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293, "D": 115.02694,
     "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049, "H": 137.05891,
     "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931}


def one_nt(x, y):
    return any(sum(p != q for p, q in zip(cx, cy)) == 1
               for cx in CODON.get(x, ()) for cy in CODON.get(y, ()))


def interior_reverse(p):
    return p if len(p) < 4 else p[0] + p[-2:0:-1] + p[-1]


rng = random.Random(1337)


def interior_shuffle(p):
    if len(p) < 4:
        return p
    mid = list(p[1:-1])
    rng.shuffle(mid)
    return p[0] + "".join(mid) + p[-1]


def index_of(keys):
    b = collections.defaultdict(list)
    for k in keys:
        h = len(k) // 2
        b[(len(k), 0, k[:h])].append(k)
        b[(len(k), 1, k[h:])].append(k)
    return b


def scan(peps, keyset, idx):
    ex = sub = cred = 0
    for q in peps:
        if q in keyset:
            ex += 1
            continue
        h = len(q) // 2
        for c in sorted(set(idx.get((len(q), 0, q[:h]), ())) | set(idx.get((len(q), 1, q[h:]), ()))):
            diff = [i for i, (a, b) in enumerate(zip(q, c)) if a != b]
            if len(diff) == 1:
                i = diff[0]
                frm, to = c[i], q[i]
                sub += 1
                if one_nt(frm, to) and abs(M.get(frm, 0) - M.get(to, 0)) >= 0.06:
                    cred += 1
                break
    return ex, sub, cred


print("loading corpus...")
real_keys = set()
for line in open(D + "corpus_peptides.txt"):
    s = line.strip().upper()
    if s:
        real_keys.add(s.translate(IL))
rev_keys = {interior_reverse(k) for k in real_keys} - real_keys
idx_rev = index_of(rev_keys)

real_pep = sorted({l.strip().upper().translate(IL)
                   for l in open(D + "crane_peptides.txt") if l.strip()})
shuf_pep = sorted({interior_shuffle(p) for p in real_pep} - real_keys)

ex, sub, cred = scan(shuf_pep, rev_keys, idx_rev)
n = len(shuf_pep)
print("\nSHUFFLED peptides vs REVERSED corpus -- independent constructions, no cancellation:")
print(f"  n={n:,}   exact {ex}   1-sub {sub} ({100*sub/n:.2f}%)   credible {cred} ({100*cred/n:.2f}%)")
print(f"\n  -> genuine chance floor: {100*cred/n:.2f}%")
print("     (the degenerate cell claimed 7.24%, which was the target cell in disguise)")
