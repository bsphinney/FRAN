"""Are keratins structurally excluded from the high-complexity stratum?

dataanalysis-16 is running a clean-but-empty check and flagged the framing risk: if the clean
(high-complexity) stratum is clean precisely BECAUSE it excludes keratins, then "hair GVP works" is
really "hair GVP works on the proteins that are not hair" -- a materially different claim.

That question can be answered from the REFERENCE PROTEOME ALONE, with no cohort and no de novo data,
because it is a property of keratin sequence composition. Keratins and KAPs are Gly/Ser/Cys-repeat
rich, which is low entropy by construction. If they concentrate in the low stratum, the exclusion is
structural and their concern is confirmed before their job lands.

Uses the paralog profile already computed over UP000005640_9606 (peptide + own_genes per row).
"""
import collections
import csv
import math

P = "/quobyte/proteomics-grp/brett/zach_hair_gvp/paralog_profile.tsv"


def entropy(p):
    c = collections.Counter(p)
    n = len(p)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


STRATA = [("low  (H<2.5)", lambda h: h < 2.5),
          ("mid  (2.5-3.2)", lambda h: 2.5 <= h < 3.2),
          ("high (H>=3.2)", lambda h: h >= 3.2)]


def family(genes):
    if any(g.startswith("KRTAP") for g in genes):
        return "KRTAP"
    if any(g.startswith("KRT") for g in genes):
        return "KRT"
    return "other"


# stratum -> family -> count
tab = {s[0]: collections.Counter() for s in STRATA}
fam_tot = collections.Counter()

with open(P) as fh:
    for r in csv.DictReader(fh, delimiter="\t"):
        pep = r["peptide"]
        genes = [g for g in r["own_genes"].split(";") if g]
        if not genes:
            continue
        f = family(genes)
        fam_tot[f] += 1
        h = entropy(pep)
        for name, test in STRATA:
            if test(h):
                tab[name][f] += 1
                break

print("WHERE EACH FAMILY'S PEPTIDES SIT, by sequence-complexity stratum")
print(f"{'family':<10}{'total':>10}{'low':>12}{'mid':>12}{'high':>12}")
print("-" * 58)
for f in ("KRT", "KRTAP", "other"):
    t = fam_tot[f]
    if not t:
        continue
    row = [tab[s[0]][f] for s in STRATA]
    print(f"{f:<10}{t:>10,}" + "".join(f"{100*v/t:>11.1f}%" for v in row))

print("\nCOMPOSITION OF EACH STRATUM (what fraction of it is keratin)")
print(f"{'stratum':<18}{'n':>12}{'KRT':>10}{'KRTAP':>10}{'other':>10}")
print("-" * 62)
for name, _ in STRATA:
    n = sum(tab[name].values())
    if not n:
        continue
    print(f"{name:<18}{n:>12,}"
          + f"{100*tab[name]['KRT']/n:>9.2f}%"
          + f"{100*tab[name]['KRTAP']/n:>9.2f}%"
          + f"{100*tab[name]['other']/n:>9.2f}%")

kl = tab["low  (H<2.5)"]["KRT"] + tab["low  (H<2.5)"]["KRTAP"]
kh = tab["high (H>=3.2)"]["KRT"] + tab["high (H>=3.2)"]["KRTAP"]
nl = sum(tab["low  (H<2.5)"].values())
nh = sum(tab["high (H>=3.2)"].values())
if nl and nh and kh:
    print(f"\nkeratin+KAP share of the LOW stratum  : {100*kl/nl:.3f}%")
    print(f"keratin+KAP share of the HIGH stratum : {100*kh/nh:.3f}%")
    print(f"depletion in high vs low              : {(kl/nl)/(kh/nh):.1f}x")
elif nh and not kh:
    print(f"\nkeratin+KAP peptides in the HIGH stratum: ZERO of {nh:,}")
