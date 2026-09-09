"""What is the chance-match rate for the GVP step? Measure it rather than argue about it.

Our lane does NO database search, so classic target-decoy does not apply: there is no search space
to add decoys to. Candidates come from STRING DISTANCE against the corpus. The null we actually
need is therefore: how often does a peptide that is NOT a real variant land exactly one
substitution from a corpus peptide by chance?

That is directly computable. Build decoy peptides matched to the real de novo peptides on length
and amino-acid composition (interior reversal, termini fixed -- the same construction the VM
session used, and for the same reason: it preserves everything except being a real sequence), then
run the identical pigeonhole search and compare hit rates.

  FDR_match = (decoy 1-sub hits / n_decoy) / (real 1-sub hits / n_real)

A second, independent null is NOT covered by this and must not be conflated with it: de novo
SEQUENCING error. That is what decoy spectra (NovoBoard FRAC-0.8, already implemented in
scripts/denovo_decoy_gen.py) measure. This script measures only the matching step.
"""
import collections
import itertools
import random
import statistics

D = "/private/tmp/claude-501/-Users-brettphinney-Documents-FRAN/a83fb84f-dba3-4ccf-b020-f03f075b5735/scratchpad/"
IL = str.maketrans("I", "L")

BASES = "TCAG"
AAS = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
CODON = {}
for i, c in enumerate(itertools.product(BASES, repeat=3)):
    CODON.setdefault(AAS[i], []).append("".join(c))


def one_nt(x, y):
    return any(sum(p != q for p, q in zip(cx, cy)) == 1
               for cx in CODON.get(x, ()) for cy in CODON.get(y, ()))


M = {"G": 57.02146, "A": 71.03711, "S": 87.03203, "P": 97.05276, "V": 99.06841, "T": 101.04768,
     "C": 103.00919, "L": 113.08406, "I": 113.08406, "N": 114.04293, "D": 115.02694,
     "Q": 128.05858, "K": 128.09496, "E": 129.04259, "M": 131.04049, "H": 137.05891,
     "F": 147.06841, "R": 156.10111, "Y": 163.06333, "W": 186.07931}


def interior_reverse(p):
    """Reverse the interior, keep both termini. Preserves length, composition and both ends."""
    return p if len(p) < 4 else p[0] + p[-2:0:-1] + p[-1]


def interior_shuffle(p, rng):
    if len(p) < 4:
        return p
    mid = list(p[1:-1])
    rng.shuffle(mid)
    return p[0] + "".join(mid) + p[-1]


print("loading corpus...")
corpus = {}
for line in open(D + "corpus_peptides.txt"):
    s = line.strip().upper()
    if s:
        corpus.setdefault(s.translate(IL), s)
buckets = collections.defaultdict(list)
for k in corpus:
    h = len(k) // 2
    buckets[(len(k), 0, k[:h])].append(k)
    buckets[(len(k), 1, k[h:])].append(k)

real = sorted({l.strip().upper().translate(IL) for l in open(D + "crane_peptides.txt") if l.strip()})
print(f"corpus {len(corpus):,} peptides · real de novo {len(real):,}\n")


def scan(peps, label):
    """-> (n_queried, n_exact, n_1sub, n_credible) using the identical pigeonhole search."""
    n_exact = n_sub = n_cred = 0
    for q in peps:
        if q in corpus:
            n_exact += 1
            continue
        h = len(q) // 2
        cands = set(buckets.get((len(q), 0, q[:h]), ())) | set(buckets.get((len(q), 1, q[h:]), ()))
        for c in sorted(cands):
            diff = [i for i, (a, b) in enumerate(zip(q, c)) if a != b]
            if len(diff) == 1:
                i = diff[0]
                frm, to = c[i], q[i]
                n_sub += 1
                if one_nt(frm, to) and abs(M.get(frm, 0) - M.get(to, 0)) >= 0.06:
                    n_cred += 1
                break
    print(f"{label:<22} n={len(peps):>6}  exact {n_exact:>5}  1-sub {n_sub:>5}  credible {n_cred:>5}"
          f"   1-sub rate {100*n_sub/len(peps):5.2f}%")
    return len(peps), n_exact, n_sub, n_cred


nr, er, sr, cr = scan(real, "REAL de novo")

rng = random.Random(1337)
decoys_rev = sorted({interior_reverse(p) for p in real})
decoys_rev = [p for p in decoys_rev if p not in corpus]      # a decoy that IS real is not a decoy
nd, ed, sd, cd = scan(decoys_rev, "DECOY interior-rev")

decoys_shuf = sorted({interior_shuffle(p, rng) for p in real})
decoys_shuf = [p for p in decoys_shuf if p not in corpus]
ns, es, ss, cs = scan(decoys_shuf, "DECOY interior-shuf")

print()
print("=" * 72)
for lab, n, s, c in (("interior-reversed", nd, sd, cd), ("interior-shuffled", ns, ss, cs)):
    fdr_sub = (s / n) / (sr / nr) if sr else float("nan")
    fdr_cred = (c / n) / (cr / nr) if cr else float("nan")
    print(f"{lab}:  1-sub FDR {100*fdr_sub:5.1f}%   credible-candidate FDR {100*fdr_cred:5.1f}%")
print("=" * 72)
print("\nA decoy that lands 1 substitution from a corpus peptide is a CHANCE match. If that rate is")
print("close to the real rate, the corpus-matching step carries no information and the candidate")
print("list is noise. If it is far below, the matching step is specific and the residual risk is")
print("de novo sequencing error -- a DIFFERENT null, measured by decoy spectra, not by this.")
