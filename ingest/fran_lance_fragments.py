"""fran_lance_fragments.py — the pooled per-peptide prior builder. SUPERSEDED; committed for
provenance, because it produced the measured numbers the design docs argue from and had never been
in version control.

⚠️ DO NOT RUN THIS EXPECTING A USABLE PRIOR. Four reasons, all documented in STORAGE_DESIGN.md:

  1. It pools over all 1,552 runs. Re-measured, that is 15.52 s robust sd against 10.57 s for a
     cohort of 16 LC-comparable runs — using more of the corpus made the answer WORSE by 5 s, and
     took 5+ hours instead of 37 seconds.
  2. Line ~25 bakes the 16-run TEST set into the aggregate. STORAGE_DESIGN.md §5 is explicit that
     exclusion must be a query-time parameter: a baked-in exclusion set silently leaks the moment the
     benchmark changes, and produces a circular result that looks excellent.
  3. It drops `frg_loss` entirely — the fragment key is (type, num, charge) only, and only b/y ions
     survive `BT`. Neutral losses are one of the two things the docs identify as FRAN's unique value.
  4. No checkpointing: one pickle at the very end. A resubmit of this ran 6:00:14 against a 6-hour
     wall, timed out, and wrote nothing.

The replacement is additive partial aggregates keyed by run — STORAGE_DESIGN.md §4.
"""
import lance, numpy as np, glob, time, re, os, pickle
G="/quobyte/proteomics-grp/brett/glendon"; t0=time.time()
def log(m): print(f"[{time.time()-t0:.0f}s] {m}",flush=True)
TEST={"21552","21561","21524","21567","21585","21533","21579","21664","21620","21673","21663","21637","21779","21782","21766","21783"}
# library dog peptides (which to collect)
z=np.load(f"{G}/paired_entrap/paired_targets.npz",allow_pickle=True); sp=z["species"]; ps=np.asarray(z["pidstr"],object)
libseq=set(re.sub(r"[0-9]+$","",str(x)) for x in ps[sp==0])
log(f"library dog seqs: {len(libseq)}")
BT={"b":98,"y":121}
# accumulators: per (seq,charge): irt/im sums; per (seq,charge,ftype,fnum,fz): relint sum + count
irt_s={}; im_s={}; pc_={}; frag_s={}
dsets=sorted(glob.glob(f"{G}/spectra_lance/*.lance")); log(f"{len(dsets)} datasets")
cols=["stripped_seq","charge","run","q_value","frg_type","frg_num","frg_charge","frg_measured_relint","irt_empirical","im"]
for di,dpath in enumerate(dsets):
    try:
        ds=lance.dataset(dpath)
        tb=ds.scanner(columns=cols, filter="q_value <= 0.01").to_table()
    except Exception as e:
        log(f"  skip {os.path.basename(dpath)}: {e}"); continue
    d=tb.to_pydict(); n=len(d["stripped_seq"])
    for i in range(n):
        seq=d["stripped_seq"][i]
        if seq not in libseq: continue
        run=str(d["run"][i])
        if any(t in run for t in TEST): continue   # DISJOINT: skip 16 test files
        ce=d["charge"][i]; ie=d["irt_empirical"][i]; ime=d["im"][i]
        if ce is None or ie is None or ime is None: continue
        key=(seq,int(ce))
        irt_s[key]=irt_s.get(key,0.0)+float(ie); im_s[key]=im_s.get(key,0.0)+float(ime); pc_[key]=pc_.get(key,0)+1
        fty=d["frg_type"][i]; fnu=d["frg_num"][i]; fz=d["frg_charge"][i]; fri=d["frg_measured_relint"][i]
        if fty is None or fri is None: continue
        for j in range(len(fty)):
            if fty[j] is None or fnu[j] is None or fz[j] is None or fri[j] is None: continue
            bt=BT.get(str(fty[j]),0)
            if bt==0: continue
            fk=(key[0],key[1],bt,int(fnu[j]),int(fz[j]))
            v=float(fri[j]); sм,c=frag_s.get(fk,(0.0,0)); frag_s[fk]=(sм+v,c+1)
    if di%50==0: log(f"  {di}/{len(dsets)} datasets, {len(pc_)} covered precursors")
log(f"DONE scan: {len(pc_)} covered precursors, {len(frag_s)} fragment entries")
pickle.dump({"irt_s":irt_s,"im_s":im_s,"pc":pc_,"frag_s":frag_s}, open(f"{G}/fran_lance_priors.pkl","wb"))
log("saved fran_lance_priors.pkl"); print("=== LANCE FRAG DONE ===",flush=True)
