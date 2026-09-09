"""Is interior reversal degenerate as a decoy for 1-substitution matching? It depends WHICH design.

dataanalysis-16 measured 356 real pairs and 356 decoy pairs, ratio exactly 1.0, and concluded
interior reversal is invalid as a decoy for this metric even on one axis. Their proof: interior
reversal is a POSITION ISOMETRY -- it permutes positions identically for every peptide of a given
length -- so if two peptides differ at exactly one position, their reversals differ at exactly one
(mirrored) position. The relation is preserved.

That proof is correct FOR THE DESIGN THEY MEASURED: counting 1-substitution pairs WITHIN a set,
before and after reversing the whole set. Reversal is a bijection on the set and preserves the
relation, so the counts are identical by construction. Nothing is learned.

But it does NOT obviously carry to a design where only ONE side is reversed and the two sides are
different sets -- de novo peptides against a corpus. There the relation "P is 1-sub from C" says
nothing about "rev(P) is 1-sub from C", because C was not reversed.

My own 38.7% came from a one-sided design, so this decides whether that number stands. Measure all
four combinations plus their within-set case, rather than reasoning about it.
"""
import collections

D = "/private/tmp/claude-501/-Users-brettphinney-Documents-FRAN/a83fb84f-dba3-4ccf-b020-f03f075b5735/scratchpad/"
IL = str.maketrans("I", "L")


def rev(p):
    return p if len(p) < 4 else p[0] + p[-2:0:-1] + p[-1]


def index_of(keys):
    b = collections.defaultdict(list)
    for k in keys:
        h = len(k) // 2
        b[(len(k), 0, k[:h])].append(k)
        b[(len(k), 1, k[h:])].append(k)
    return b


def n_hits(queries, keyset, idx):
    """How many queries sit exactly 1 substitution from some member of keyset."""
    n = 0
    for q in queries:
        if q in keyset:
            continue
        h = len(q) // 2
        for c in set(idx.get((len(q), 0, q[:h]), ())) | set(idx.get((len(q), 1, q[h:]), ())):
            if sum(1 for a, b in zip(q, c) if a != b) == 1:
                n += 1
                break
    return n


print("loading...")
corpus = set()
for line in open(D + "corpus_peptides.txt"):
    s = line.strip().upper()
    if s:
        corpus.add(s.translate(IL))
peps = sorted({l.strip().upper().translate(IL) for l in open(D + "crane_peptides.txt") if l.strip()})

rcorpus = {rev(k) for k in corpus} - corpus
rpeps = [rev(p) for p in peps]
ic, irc = index_of(corpus), index_of(rcorpus)

print(f"corpus {len(corpus):,} · reversed corpus {len(rcorpus):,} · peptides {len(peps):,}\n")

a = n_hits(peps,  corpus,  ic)     # target
b = n_hits(rpeps, corpus,  ic)     # decoy PEPTIDES vs real corpus   (my null A)
c = n_hits(peps,  rcorpus, irc)    # real peptides vs decoy CORPUS   (my null B)
d = n_hits(rpeps, rcorpus, irc)    # BOTH reversed                   (the involution trap)

n = len(peps)
print(f"{'design':<44}{'hits':>8}{'rate':>9}{'ratio vs target':>18}")
print("-" * 80)
for lab, v in (("target: real peptides vs real corpus", a),
               ("null A: REVERSED peptides vs real corpus", b),
               ("null B: real peptides vs REVERSED corpus", c),
               ("trap:   reversed peptides vs reversed corpus", d)):
    print(f"{lab:<44}{v:>8}{100*v/n:>8.2f}%{v/a:>17.3f}")

print("\n--- their measurement: 1-substitution pairs WITHIN one set, before and after reversal ---")
# Take a manageable slice so the O(n^2)-ish pair count is tractable, and use the same blocking.
sample = sorted(list(corpus))[:40000]
si = index_of(set(sample))
pairs = 0
for q in sample:
    h = len(q) // 2
    for cnd in set(si.get((len(q), 0, q[:h]), ())) | set(si.get((len(q), 1, q[h:]), ())):
        if cnd > q and sum(1 for x, y in zip(q, cnd) if x != y) == 1:
            pairs += 1
rsample = [rev(x) for x in sample]
ri = index_of(set(rsample))
rpairs = 0
for q in rsample:
    h = len(q) // 2
    for cnd in set(ri.get((len(q), 0, q[:h]), ())) | set(ri.get((len(q), 1, q[h:]), ())):
        if cnd > q and sum(1 for x, y in zip(q, cnd) if x != y) == 1:
            rpairs += 1
print(f"  within-set 1-sub pairs, real     : {pairs}")
print(f"  within-set 1-sub pairs, reversed : {rpairs}")
print(f"  ratio                            : {rpairs/pairs if pairs else float('nan'):.3f}")
print("\n  ^ if this is 1.0, reversal is degenerate FOR THIS DESIGN -- it is a bijection that")
print("    preserves the relation, so the count cannot change. That is their finding.")
