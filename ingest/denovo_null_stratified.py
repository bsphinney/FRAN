"""Is the crane 38.7% an aggregate over strata with different rates, as hair turned out to be?

dataanalysis-16 stratified the hair cohort by sequence complexity and the muddy aggregate resolved
into three clean regimes: 73-80% chance-match in low-complexity repeats, 24-28% in mid, and 0.000%
in high-complexity -- zero decoy hits in 23,316 peptides by two independent decoy routes. The
aggregate described none of the three.

I have quoted 38.7% for crane repeatedly. If it is the same kind of mixture, then "39% of credible
candidates are chance" is true of the pooled set and false of every part of it, and the candidates
that matter may be far cleaner than I have been saying.

Also adopts their leak diagnostic: count EXACT matches from the decoy set to the real reference. A
decoy that reproduces real sequences is not decoying, and the exact-match count is what reveals it
-- their Markov resampler scored 1,797 exact against 103 for reversal and 181 for shuffle, so its
apparently-higher rate was contamination rather than a better estimate.
"""
import collections
import math
import random

D = "/private/tmp/claude-501/-Users-brettphinney-Documents-FRAN/a83fb84f-dba3-4ccf-b020-f03f075b5735/scratchpad/"
IL = str.maketrans("I", "L")
rng = random.Random(1337)


def rev(p):
    return p if len(p) < 4 else p[0] + p[-2:0:-1] + p[-1]


def shuf(p):
    if len(p) < 4:
        return p
    m = list(p[1:-1])
    rng.shuffle(m)
    return p[0] + "".join(m) + p[-1]


def entropy(p):
    """Shannon entropy over residue composition, in bits. Their stratification axis."""
    c = collections.Counter(p)
    n = len(p)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def index_of(keys):
    b = collections.defaultdict(list)
    for k in keys:
        h = len(k) // 2
        b[(len(k), 0, k[:h])].append(k)
        b[(len(k), 1, k[h:])].append(k)
    return b


def counts(queries, keyset, idx):
    """-> (n_exact, n_1sub). n_exact is the LEAK diagnostic when queries are decoys."""
    ex = sub = 0
    for q in queries:
        if q in keyset:
            ex += 1
            continue
        h = len(q) // 2
        for c in set(idx.get((len(q), 0, q[:h]), ())) | set(idx.get((len(q), 1, q[h:]), ())):
            if sum(1 for a, b in zip(q, c) if a != b) == 1:
                sub += 1
                break
    return ex, sub


print("loading corpus...")
corpus = set()
for line in open(D + "corpus_peptides.txt"):
    s = line.strip().upper()
    if s:
        corpus.add(s.translate(IL))
idx = index_of(corpus)
peps = sorted({l.strip().upper().translate(IL) for l in open(D + "crane_peptides.txt") if l.strip()})
print(f"corpus {len(corpus):,} · crane peptides {len(peps):,}\n")

# --- leak diagnostic, on the whole set ---
print("DECOY LEAK CHECK (exact matches from the decoy set into the real corpus)")
for lab, fn in (("interior-reversed", rev), ("composition-shuffled", shuf)):
    dec = [fn(p) for p in peps]
    ex, _ = counts(dec, corpus, idx)
    print(f"  {lab:<24}{ex:>6} exact  ({100*ex/len(dec):.2f}%)"
          + ("   <- LEAKING, not a decoy" if ex > 0.05 * len(dec) else "   clean"))

# --- stratify by entropy, using their cut points ---
STRATA = [("low  (H<2.5)", lambda h: h < 2.5),
          ("mid  (2.5-3.2)", lambda h: 2.5 <= h < 3.2),
          ("high (H>=3.2)", lambda h: h >= 3.2)]

print(f"\n{'stratum':<18}{'n':>8}{'exact':>8}{'1sub':>7}{'rate':>9}{'FDR(rev)':>11}{'FDR(shuf)':>11}")
print("-" * 74)
for name, test in STRATA:
    sub = [p for p in peps if test(entropy(p))]
    if not sub:
        continue
    ex_r, sub_r = counts(sub, corpus, idx)
    rate = sub_r / len(sub)
    row = []
    for fn in (rev, shuf):
        dec = [fn(p) for p in sub]
        _, ds = counts(dec, corpus, idx)
        row.append((ds / len(dec)) / rate if rate else float("nan"))
    print(f"{name:<18}{len(sub):>8}{ex_r:>8}{sub_r:>7}{rate:>9.4f}"
          f"{row[0]:>11.3f}{row[1]:>11.3f}")

ex_all, sub_all = counts(peps, corpus, idx)
r_all = sub_all / len(peps)
agg = []
for fn in (rev, shuf):
    dec = [fn(p) for p in peps]
    _, ds = counts(dec, corpus, idx)
    agg.append((ds / len(dec)) / r_all)
print("-" * 74)
print(f"{'AGGREGATE':<18}{len(peps):>8}{ex_all:>8}{sub_all:>7}{r_all:>9.4f}"
      f"{agg[0]:>11.3f}{agg[1]:>11.3f}")
print("\nexact-vs-1sub per stratum is their no-decoy-needed sanity check: near-misses OUTNUMBERING")
print("exact hits is the signature of a sequence space dense enough to manufacture neighbours.")
