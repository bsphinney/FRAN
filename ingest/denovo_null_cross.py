"""The 4-way cross for corpus matching, after denovo_decoy_method.html section 3.

That document validated its de novo -> homology FDR by running TWO independent nulls and showing
they agree:

    query \\ database      real DB (target)   reversed DB (decoy)
    real spectra          14,337 (4.58%)     73 (0.023%)
    decoy spectra (f08)      128 (0.030%)    99 (0.023%)

Agreement between the two off-diagonal cells is the evidence that the null is real rather than an
artefact of how one of them was built. The same design applies to corpus MATCHING, with the axes
renamed:

    query \\ corpus        real corpus        reversed corpus
    real de novo          [target]           [null B: decoy corpus]
    decoy de novo         [null A: decoy peptides]   [chance floor]

Null A (decoy peptides) and null B (reversed corpus) are constructed completely differently, so if
they agree, the chance-match rate is a property of sequence space and not of either construction.
If they disagree, one of them is measuring something else and neither can be trusted yet.
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


def index_of(keys):
    b = collections.defaultdict(list)
    for k in keys:
        h = len(k) // 2
        b[(len(k), 0, k[:h])].append(k)
        b[(len(k), 1, k[h:])].append(k)
    return b


def scan(peps, keyset, idx):
    """-> (n_exact, n_1sub, n_credible) -- the identical pigeonhole search in every cell."""
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

# The reversed corpus. Interior reversal keeps length, composition and both termini, so the
# reversed corpus occupies sequence space with the same shape as the real one -- which is the
# whole point: it is a corpus that CANNOT contain a true match.
rev_keys = {interior_reverse(k) for k in real_keys}
rev_keys -= real_keys                    # a reversed peptide that is also real is not a decoy
print(f"real corpus {len(real_keys):,} · reversed corpus {len(rev_keys):,} "
      f"({len(real_keys) - len(rev_keys):,} palindromic/collision dropped)")

idx_real, idx_rev = index_of(real_keys), index_of(rev_keys)

real_pep = sorted({l.strip().upper().translate(IL) for l in open(D + "crane_peptides.txt") if l.strip()})
decoy_pep = sorted({interior_reverse(p) for p in real_pep} - real_keys)
print(f"real de novo {len(real_pep):,} · decoy de novo {len(decoy_pep):,}\n")

cells = {}
for qlab, q in (("real de novo", real_pep), ("decoy de novo", decoy_pep)):
    for dlab, keys, idx in (("real corpus", real_keys, idx_real),
                            ("reversed corpus", rev_keys, idx_rev)):
        cells[(qlab, dlab)] = scan(q, keys, idx)
        n = len(q)
        ex, sub, cred = cells[(qlab, dlab)]
        print(f"{qlab:<14} vs {dlab:<16} exact {ex:>5}  1-sub {sub:>5} ({100*sub/n:5.2f}%)  "
              f"credible {cred:>5} ({100*cred/n:5.2f}%)")

print("\n" + "=" * 74)
print("THE 4-WAY CROSS  (credible-candidate rate, % of queries)")
print("=" * 74)
print(f"{'query \\ corpus':<18}{'real corpus':>16}{'reversed corpus':>18}")
for qlab, q in (("real de novo", real_pep), ("decoy de novo", decoy_pep)):
    r = 100 * cells[(qlab, "real corpus")][2] / len(q)
    v = 100 * cells[(qlab, "reversed corpus")][2] / len(q)
    print(f"{qlab:<18}{r:>15.2f}%{v:>17.2f}%")

a = 100 * cells[("decoy de novo", "real corpus")][2] / len(decoy_pep)
b = 100 * cells[("real de novo", "reversed corpus")][2] / len(real_pep)
floor = 100 * cells[("decoy de novo", "reversed corpus")][2] / len(decoy_pep)
tgt = 100 * cells[("real de novo", "real corpus")][2] / len(real_pep)
print("=" * 74)
print(f"\nnull A (decoy peptides vs real corpus)   {a:.2f}%")
print(f"null B (real peptides vs reversed corpus) {b:.2f}%")
print(f"chance floor (decoy vs reversed)          {floor:.2f}%")
print(f"target (real vs real)                     {tgt:.2f}%")
agree = abs(a - b) / max(a, b) if max(a, b) else 0
print(f"\nthe two nulls differ by {100*agree:.0f}% of the larger.")
print("They are built completely differently, so agreement means the chance-match rate is a")
print("property of sequence space, not of either construction -- the same argument the crane")
print("method document makes for its decoy-spectra vs decoy-database cross.")
print(f"\nFDR from null A: {100*a/tgt:.1f}%   from null B: {100*b/tgt:.1f}%")
