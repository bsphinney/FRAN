# Error control for de novo variant calling — measured findings

**Established 2026-09-09** across two independent cohorts by two sessions working as analyst and
reviewer: `dataanalysis-16` (Zach hair, 421 runs / 10.76M PSMs) and `fran-00` (crane feather, 15,512
peptides). Every number here is measured, and most of them corrected a confident wrong claim by one
of us. The companion record is `dataanalysis-16`'s session design document; both exist deliberately,
because between us we got the involution, the within-set degeneracy and the leak-test threshold
wrong *before* measuring them, and a single record would be a single point of failure.

This is not a plan. It is the set of results a future implementer should not have to re-derive.

---

## 1. The chance-match rate is REGIONAL, not a single number

Stratify de novo peptides by Shannon entropy before quoting any FDR. An aggregate describes none of
the strata:

| stratum | crane FDR (rev / shuf) | hair FDR (rev / shuf) |
|---|---|---|
| low, H < 2.5 | **1.221 / 1.183** | 0.735 / 0.802 |
| mid, 2.5 ≤ H < 3.2 | 0.439 / 0.403 | 0.239 / 0.283 |
| high, H ≥ 3.2 | **0.000 / 0.000** | **0.000 / 0.000** |
| *aggregate* | *0.569 / 0.539* | *0.378 / 0.444* |

**Low complexity has no target–decoy separation at all.** On crane the decoys match *more* often
than real peptides. Report that as "no separation", never as a high FDR — a reader seeing 0.8
imagines a threshold could rescue it, and 1.2 makes clear that nothing can.

**High complexity is clean in both cohorts by both decoy routes** — four zeros. Two species, two
sample types, same structure: this is a property of 1-substitution matching against any real
proteome, not of one cohort.

## 2. Five error sources need five different controls

| | Source | Control | Note |
|---|---|---|---|
| E1 | chance match | decoy null, cross-set one-sided | regional, see §1 |
| E2 | de novo sequencing error | database search on the SAME spectra | **cannot** be estimated from exact-reference-match peptides — a set where called == truth contains no confusions by construction, and the selection bias understates error |
| E3 | wrong genotype | carrier prevalence vs allele frequency | removed 65 of 67 candidates in the 2026-07 VM session while the entrapment removed none |
| E4 | unmodelled PTMs | isobaric-pair exclusion | Casanovo's vocabulary is fixed, so an unrepresentable modification is absorbed as a residue call. 30% of crane credible candidates are PTM-isobaric; `A→S` and `F→Y` are +15.995 = **oxidation**, critical for chronically oxidised hair |
| E5 | paralog cross-mapping | direct measurement, not a null | **no decoy can measure it** — decoys are built by destroying the homology that causes it |

**A 0.000 chance-match FDR does not protect against paralogs.** The clean stratum is simultaneously
E1-clean and E5-saturated: hair candidates there sit in **2.1× more paralog-dense space** than exact
matches (n=1,031), and the fraction with ≥1 paralog neighbour more than doubles. One FDR number is
not an adequate error model for this problem.

## 3. Decoy-construction traps

All three were found the hard way, each after a confident wrong claim.

- **Interior reversal is an involution.** Decoying *both* axes cancels and hands back the target cell
  as a "chance floor" — measured 7.24% against a 7.30% target. Use a **different operation per axis**.
- **It is also degenerate for within-set pair counting.** Reversal is a bijection preserving the
  1-substitution relation, so the count cannot change (5,938 before and after). It is **not**
  degenerate cross-set one-sided (0.569 / 0.579), which is the design that works. The boundary is
  exactly: same permutation applied to both members of every compared pair.
- **Leakage must be tested RELATIVELY.** Count exact matches from the decoy set into the real
  reference. A Markov resampler leaked at 1.2% — under any plausible absolute threshold. What
  exposed it was being 10–17× the other routes. **A single decoy cannot self-diagnose leakage.**

## 4. Reference-size growth asymmetry (decoy-free diagnostic)

Across a reference expansion, 1-substitution matches should scale with reference density while exact
matches should not — chance scales, signal does not. Measured **hair 1.10, crane 1.05** over ~4×
expansions.

**Hard constraint: expand by MISSED CLEAVAGES ONLY, never by a digest-rule change.** Semi-tryptic
expansion generates terminal subsequences that differ from one another at their termini *by
construction*, so it inflates the 1-substitution numerator through a mechanism unrelated to chance.
A semi-tryptic "1.34 across 46×" is not a scaled-up missed-cleavage number; it is a different and
biased measurement, and quoting it beside a 4× number suggests a 25× cohort difference where the
honest comparison is 1.10 vs 1.05.

Crane is **underpowered** for this test: matched cross-species, its exact set barely grows
(3,481 → 3,901), so the denominator is nearly fixed. The two tests differ in what they need —
paralog density wants a gene-annotated reference and tolerates cross-species; growth asymmetry needs
a within-species reference where the exact set can actually move.

## 5. Substrate ceiling

Only **~768** human keratin/KAP tryptic peptides are high-complexity — 21% of KRT, 19% of KRTAP,
a **3.0×** depletion against other proteins. No cohort, however deep, yields more, because that is
all there are.

Report **"N available, M observed, D donors"** rather than a single yield number: it separates *the
method is thin* from *this cohort is thin*, which a yield conflates. Donor coverage is the number
that matters for a forensic-adjacent question — a method that works on 8% of individuals is not a
method.

## 6. Standard settings, so cohorts stay comparable

- Digest: **fully-tryptic, 1 missed cleavage.** Semi-tryptic buys ~1.9 points of no-match rate for a
  46× reference and a large increase in chance-match surface.
- Reference: a **gene-annotated, single-species** proteome (UP000005640_9606 for human). Not FRAN's
  corpus, which spans 114 organisms — a peptide one substitution from a bovine or bacterial corpus
  entry is not a human variant.
- Entropy strata: low H < 2.5, mid 2.5–3.2, high H ≥ 3.2.

## Tools

`ingest/denovo_gvp.py` · `denovo_null.py` · `denovo_null_cross.py` · `denovo_null_floor.py` ·
`denovo_null_stratified.py` · `denovo_null_degeneracy.py` · `denovo_paralog.py` ·
`denovo_family_entropy.py` · `denovo_asymmetry.py` · `denovo_growth_asymmetry.py`
