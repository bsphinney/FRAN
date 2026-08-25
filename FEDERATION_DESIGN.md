# FRAN federation: many labs, one search

**For:** whoever builds multi-lab FRAN.
**Status:** design proposal, 2026-08-24. Decisions taken with Brett: peers see the **public tier as
it already exists**, discovery is a **registry + direct fan-out**, partner labs run their **own
Postgres + Lance**.

⚠️ **What I did not inspect:** the ingest side (`ingest/`), the Lance layout on disk, or PG Farm's
role model. The schema/deployment sections are proposals about *contracts*, not critiques of what
exists.

---

## 0. The one-line answer

**You do not need a new protocol. You need identity, a trust boundary, and merge rules.**

FRAN already exposes a read-only HTTP API and an MCP endpoint (`app/mcp_server.py`) with
`search_peptides`, `search_proteins`, `protein_detail`, `peptide_detail`, `corpus_overview`,
`list_searches`. A peer FRAN is just another client of that API. Federation is therefore not a
transport problem — it is four problems:

| problem | why it is the hard part |
|---|---|
| **Identity** | every id in FRAN is local. `run_id=4471` means nothing once two labs answer. |
| **Trust** | the public tier is already a boundary — but it was drawn against *the world*, not against *a peer program that will scrape it exhaustively*. |
| **Merge** | pooling counts across labs is the §5 STORAGE_DESIGN error one level up. |
| **Version skew** | Lab B will run 18-month-old FRAN forever. That is the steady state, not a transition. |

Everything below is those four.

---

## 1. Node identity

Every FRAN install gets a **`node_id`**: short, stable, lowercase, assigned once and never reused
(`ucdavis-gcpc`, `broad-pxlab`, `ethz-aebersold`). It goes in config, not in the DB, and it is the
namespace for everything that lab emits.

New endpoint, public, unauthenticated, cheap, cacheable:

```
GET /api/federation/node
{
  "node_id": "ucdavis-gcpc",
  "display_name": "UC Davis Genome Center Proteomics Core",
  "base_url": "https://fran.stan-proteomics.org",
  "contact": "…",
  "fran_version": "…",              # from /version
  "schema_version": "fran-public-1",# the CONTRACT version, see §5
  "share_policy": "public-tier",    # public-tier | counts-only | closed
  "capabilities": ["search.peptide","search.protein","aggregate.additive","presence.filter"],
  "corpus": { "precursors": …, "peptides": …, "protein_groups": …,
              "organisms": …, "runs": …, "im_fraction": … },
  "coverage": { "instruments": [...], "organisms_top": [...],
                "date_range": ["2019-03","2026-08"] },
  "license": "CC-BY-4.0",
  "updated_at": "…"
}
```

This one document is the whole handshake. `capabilities` is what makes version skew survivable: a
client asks for what a peer *says* it can do and degrades cleanly otherwise, instead of probing
endpoints and interpreting 404s.

**Namespace every id that crosses the wire.** `run_id` → `"ucdavis-gcpc:4471"`. Same for
`search_id`, `submission_id`, `protein_group` where it is source-database-specific. This is the
single most common federation bug and it is silent: two labs' run 4471 merge into one run and the
aggregate is wrong with no error anywhere. Do it in the serializer, once, so it cannot be forgotten
per-endpoint.

---

## 2. Discovery: a registry that is a git repo, not a service

```
https://github.com/stan-proteomics/fran-registry  →  registry.json
```

```json
{ "registry_version": 1,
  "nodes": [ { "node_id": "ucdavis-gcpc",
               "base_url": "https://fran.stan-proteomics.org",
               "pubkey": "ed25519:…", "tags": ["core-facility","timstof","dia"] } ] }
```

Rules that make this durable:

1. **The registry holds pointers, never data.** No index, no counts, no cached peptides. If it is
   offline, every node keeps working from its cached copy.
2. **Adding a node is a pull request.** Human review, public audit trail, zero hosting, and it
   outlives whoever's grant currently pays for the domain. A registry *service* is a single point of
   failure with a maintainer attached; a registry *file* is not.
3. **Each node re-fetches hourly and caches to disk.** Registry down ≠ federation down.
4. **The local admin still gates it.** `FRAN_PEERS_ALLOW` / `FRAN_PEERS_DENY`, default: allow all
   registry nodes, deny everything not in it. Listing yourself in the registry does not entitle you
   to be queried.
5. **Peers must self-identify.** `User-Agent: FRAN-node/2.1 (ucdavis-gcpc)` plus a signed request
   header; a `node_id` that does not resolve in the registry gets the anonymous rate limit
   (`app/ratelimit.py` already exists), not the peer one.

This is the "registry + fan-out" shape without a hub to run.

---

## 3. Query: scatter-gather with attribution, never a pooled number

### 3.1 Two tiers

**Presence (routing).** "Which nodes have peptide `LVNELTEFAK`?" Cheap, highly cacheable, and the
only call that needs to hit every peer.

**Detail.** Full payload, fetched *only* from the nodes presence said matched. This turns a 12-node
fan-out into a 12-node cheap call plus a 2-node expensive one.

### 3.2 Make presence nearly free with a published filter

Each node publishes, daily, a Bloom filter over its stripped peptide sequences:

```
GET /api/federation/presence/peptides.bloom   # ~3.7 MB for 3.1M peptides @ 1% FPR
```

A client fetches every peer's filter once a day and answers "who might have this peptide" **locally,
in microseconds, with no network call at all**. False positives cost one wasted detail request;
false negatives are impossible. This buys you most of the speed of a central index hub with none of
the hub — and it degrades to plain fan-out for any peer that does not publish one.

### 3.3 Merge rules — the part that will otherwise go wrong

These are STORAGE_DESIGN §4 and §5 restated for the cross-lab case, and they matter more here.

**Return per-node results. Do not merge by default.** A federated peptide search returns a list
keyed by `node_id`, and the UI groups by lab. Any pooled consensus is something the caller *asks*
for over an explicit node+run selection. STORAGE_DESIGN already showed that 16 selected runs beat all
1,552 pooled — a 2021 Orbitrap run and a 2024 manatee serum run voting on a dog peptide's RT is
noise. Federation is that failure mode with more labs, more instruments and more iRT conventions.
Indiscriminate pooling across institutions is strictly worse than the pooling that already lost.

**Ship sums and counts, never means.** Aggregate payloads carry `n, irt_sum, irt_sumsq, im_sum,
im_sumsq` per `(stripped_seq, charge, node_id, run_id)`. Means cannot be recombined or subtracted;
sums can. Excluding a *whole node* from a consensus then becomes a subtraction, not a refetch —
which is exactly what benchmarking will need the first time someone federates with a lab that holds
their held-out test runs. **The exclusion constraint is now cross-node: `(node_id, run_id)` must
survive into every aggregate.**

**Deduplicate on content, not on name.** Multiple labs will have ingested the same public dataset
(PRIDE reanalyses, shared HeLa QC files, the same commercial standard). Without a dedup key,
federated counts double-count and a "consensus" quietly averages one file with itself. Expose a
`raw_content_sha256` per run in aggregate payloads — the XIC pilot already keeps a content-md5
registry, so the habit exists — and dedup on it at merge time. State loudly in the UI when dedup
fired.

**Never put a federated count on the dashboard.** "335M precursors across 7 labs" is a number nobody
can reproduce, audit, or defend, and it will be quoted in a grant. Keep the overview local and
honest. Federation is a *search* feature.

### 3.4 Failure is normal, so make it visible

Hard per-peer deadline (start at 3 s), partial results always, and every federated response carries:

```json
"nodes": [ {"node_id":"broad-pxlab","status":"ok","latency_ms":412,"n":37},
           {"node_id":"ethz-…","status":"timeout","latency_ms":3000},
           {"node_id":"riken-…","status":"skipped","reason":"schema_version fran-public-2 unsupported"} ]
```

A silently-dropped peer is a wrong answer wearing a right answer's clothes. Users must be able to
see "5 of 7 labs answered."

---

## 4. Trust — the section to get right or not ship

The existing boundary in `app/db.py` (read-only sessions, `PUBLIC_TABLES` allowlist, parameterized
queries only) is the right foundation. Federation stresses it in three new ways.

**4.1 The federation router is public-tier, structurally.** A peer request must have no path to set
the `internal` flag — `/api/internal/collaborators`, `/lab/{pi}`, `/people_search`,
`/submission/{id}` are **never** federated, at any trust level. Add a test that asserts the
federation router cannot reach `_INTERNAL_TABLES`, so the guarantee is enforced by CI rather than by
whoever reviews the next PR.

**4.2 Free text is the leak, not tables.** Run names, search names and dataset filenames routinely
carry PI names, client names, grant numbers and unpublished project codes — the existence of
`app/service_customer_aliases.json` is evidence this is already a live problem locally. Anonymous
browsing by a human is a different threat model from a peer program that will pull every string you
have, forever, and cache it. So: **federated payloads ship opaque run ids plus structured metadata**
(instrument, gradient_minutes, acquisition_method, organism, tissue, month) and **no raw filenames**
unless the node explicitly opts in per-search.

**4.3 Default-deny with an embargo flag.** Labs need "this data exists but is not shareable until
the paper is out." Add per-search:

```sql
federation_visibility  text  NOT NULL DEFAULT 'hidden'   -- hidden | counts_only | public
```

New ingests default to **hidden**. An admin promotes them. This is slower and it is correct: a lab
that leaks an unpublished dataset once will never federate again, and neither will the three labs
they tell. Optionally add `federation_release_date` so an embargo can expire on its own.

**4.4 Authn, proportionate.** Anonymous for the open tier; ed25519-signed requests for peer-tier
(reuse the registry pubkey); per-peer bearer tokens only where a lab wants a named collaborator to
see more. mTLS is not worth what it costs a partner lab's sysadmin.

---

## 5. The contract that makes "install FRAN" possible

Today the API is coupled to `delimp_*`, which is UC Davis's schema. Federating means freezing
something a partner can implement.

**Freeze the JSON, not the tables.** `fran-public-1` is a versioned spec of the federation endpoints'
request/response shapes. That is what `schema_version` advertises and what a peer promises. A lab
backs it with whatever it likes — including a much smaller store — as long as the shapes hold. Since
partner labs will in fact run Postgres + Lance, also ship:

- `schema/fran_public.sql` — DDL for the public tables only, no `coreomics_*`, no provenance.
- A conformance suite: `pytest --node-url https://fran.otherlab.edu` that a new install runs to prove
  it is a valid `fran-public-1` node before it is added to the registry. This is what keeps the
  registry from filling with broken nodes.

**Version skew is permanent.** Support N and N−1 of the contract, negotiate through `capabilities`,
and skip incompatible peers *loudly* (§3.4). Never let an old peer's response be reinterpreted under
new semantics — that is how a silent wrong number gets into a paper.

---

## 6. Rollout — each phase useful alone

| phase | ships | proves |
|---|---|---|
| **0** | `/api/federation/node` + `registry.json` with one entry | discovery, zero risk, zero query surface |
| **1** | presence-only fan-out, **2 nodes**, results grouped by lab in the UI | the trust boundary and the attribution UX |
| **2** | additive aggregates over the wire + published Bloom filters | scoped cross-lab consensus at usable latency |
| **3** | MCP tools take a `nodes` parameter | agents query the federation, not one lab |

**The two-node pilot is the entire test.** Everything that breaks at 12 nodes except raw latency —
id collisions, dedup, embargo, skew, attribution — breaks at 2. Do not design for 12 before 2 works.

---

## 7. Open questions and the one I would push back on

1. **Ingest, not federation, is the real barrier.** FRAN's value comes from an ingested corpus, and
   ingest is the part that is hard to install. A partner lab that cannot run ingest gets an empty
   FRAN and federation buys them nothing but a search box pointed at you. Worth scoping a **FRAN-lite**
   that ingests DIA-NN `report.parquet` / Spectronaut exports directly into `fran_public.sql` with no
   Lance lane — a node that contributes identifications and RT/IM but no XICs. That is probably the
   difference between 2 nodes and 10.
2. **Who owns the registry after the grant?** A git repo under an org with ≥3 admins. Decide now,
   while it costs nothing.
3. **Citation and credit.** Labs federate when they get cited. Every node's descriptor should carry a
   DOI/citation string, federated results should surface it, and the UI should make "which lab
   produced this observation" impossible to miss. This is a social requirement doing load-bearing
   technical work.
4. **Licensing is not uniform.** Node descriptors carry `license`; a client that pools across
   incompatible licenses should say so rather than silently produce a derived work nobody may
   redistribute.
5. **Unresolved:** whether cross-node RT consensus is *scientifically* sound at all given differing
   iRT calibration sources (STORAGE_DESIGN §3 flags `irt_calibration_source` as genuinely absent).
   Until that field exists and is populated, treat cross-node RT pooling as **unvalidated** — expose
   per-node values and do not offer a pooled prior. This is the same retraction discipline
   STORAGE_DESIGN applied to the Spectronaut parity claim, and it applies here before anyone quotes a
   federated number.
