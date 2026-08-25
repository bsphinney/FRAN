# Connecting your FRAN to other FRANs

Two labs running FRAN can search each other's corpora. This is the **operator** guide; the design and
its reasoning are in [FEDERATION_DESIGN.md](FEDERATION_DESIGN.md).

**Federation is off by default.** A fresh install shares nothing until you set a policy, a node id
and a salt, *and* mark individual searches as shareable. That is three deliberate acts, and it is
deliberate: forgetting any of them means "share nothing", never "share everything".

---

## What is actually shared

Peptide-level observations, de-identified:

| shared | withheld |
|---|---|
| peptide sequence, modified form, charge | raw filenames and paths |
| m/z, RT, iRT, ion mobility, q-value, intensity | search names, output dirs |
| protein group, gene, organism | PI, client, project, campus |
| platform, instrument **model**, gradient, engine | instrument **serial** |
| a pseudonymous run id and search id | local database ids, LIMS submission ids |

The boundary is a single allowlist in `app/federation.py` (`ALLOWED_FIELDS`). Adding a column to
`delimp_precursors` does **not** make it shareable — someone has to add it there on purpose. That is
enforced by `tests/test_federation_boundary.py`, which asserts no forbidden field and no forbidden
*value* survives a record build. Run it before you enable sharing:

```bash
python tests/test_federation_boundary.py
```

## Why run ids are salted

FRAN already has `raw_files.raw_name_anonymized` — an **unsalted SHA-1** of the raw path. That is
fine for a human browsing a public page. It is not fine against a peer program: raw filenames come
from a small, guessable space (instrument prefix, date, well position), so an unsalted digest can be
confirmed by hashing guesses at scale. Federated pseudonyms therefore use HMAC-SHA256 under a
**per-node secret salt**, so a correct guess still proves nothing.

Consequences worth knowing: pseudonyms are stable *within* a node (so repeated queries agree and
aggregates join), meaningless outside it, and **rotating the salt breaks linkage to everything you
shared before**. Rotate deliberately, not casually.

## Setup

### 1. Apply the federation schema

```bash
psql "$FRAN_DB_URL" -f schema/federation.sql
```

Adds `delimp_searches.federation_visibility` (default `hidden`) and an optional
`federation_release_date`.

### 2. Configure the node

```bash
export FRAN_NODE_ID="yourlab-shortname"        # lowercase, stable, never reused
export FRAN_NODE_DISPLAY_NAME="Your Lab, Your University"
export FRAN_NODE_BASE_URL="https://fran.yourlab.edu"
export FRAN_NODE_CONTACT="proteomics@yourlab.edu"
export FRAN_SHARE_POLICY="counts-only"         # closed | counts-only | public-tier
export FRAN_FED_SALT="$(openssl rand -hex 32)" # SECRET. back it up; rotating breaks past linkage
```

`FRAN_FED_SALT` must be at least 32 characters. If it is missing or short the node **refuses to
build federated records** rather than falling back to an unsalted hash — a weak pseudonym looks
identical to a strong one in the response, so that failure had to be loud.

### 3. Publish only what you mean to

```sql
-- everything starts hidden; promote deliberately
UPDATE delimp_searches SET federation_visibility = 'public'
 WHERE id = '<search-id>';

-- contribute to presence and counts without exposing observations
UPDATE delimp_searches SET federation_visibility = 'counts_only'
 WHERE id = '<search-id>';

SELECT federation_visibility, count(*) FROM delimp_searches GROUP BY 1;
```

### 4. Join the registry

Discovery is a git repo, not a service — nothing to host, nothing to keep running, and a public
audit trail of who joined when:

```
https://github.com/stan-proteomics/fran-registry  →  registry.json
```

Open a pull request adding your node. Each node re-fetches hourly and caches to disk, so the
registry being unreachable does not stop federation.

Being listed does not entitle anyone to query you:

```bash
export FRAN_PEERS_ALLOW="ucdavis-gcpc,broad-pxlab"   # empty = all registry nodes
export FRAN_PEERS_DENY="somenode"
```

### 5. Check yourself

```bash
curl https://fran.yourlab.edu/api/federation/node
```

Confirm `share_policy` and `capabilities` say what you intend before announcing the node.

## Protection against bulk extraction

A peer is a *program*, and a program is patient. `app/ratelimit.py` is per-worker, in-process and
windowed in seconds — it stops a tight loop, but not a peer asking politely for one peptide per
second, which retrieves ~2.6M observations a month while every individual request looks reasonable.

`app/federation_guard.py` adds four independent controls, which fail in different ways on purpose:

| control | what it stops |
|---|---|
| **Shape** | Enumeration is not expressible. No empty selectors, no wildcards or patterns, a minimum sequence length, no `offset` sweeping past the lookup. The cheapest control: if the API cannot say "give me everything", nothing downstream has to catch it. |
| **Response cap** | Hard row ceiling per call (default 1,000). Bounds any single request. |
| **Cumulative budget** | Durable per-peer row allowance per day and per 30 days. **This is the control that actually bounds exfiltration** — it survives restarts, spans workers, and does not care how slowly you ask. |
| **Novelty** | Research repeats and clusters; enumeration is a stream of never-before-seen keys. A peer whose queries are almost entirely novel across a large daily volume is walking the corpus, whatever its rate. |

### What the defaults mean concretely

200,000 rows/day and 2,000,000 rows/30 days against a 416M-precursor corpus:

* Copying the corpus at the cap would take **~208 months — roughly 17 years.**
* A real peptide lookup returns a few hundred rows, so **~600+ lookups a day** remain available.

Generous for research, useless for bulk transfer. That asymmetry is the whole design.

### The ledger is the budget

Spend is `SUM(n_rows)` over `delimp_federation_access_log` rather than a counter, so the budget can
never disagree with the audit trail, and "what did that peer actually take?" is answerable after the
fact. **Denials are logged too** — a peer hammering a closed door is visible rather than silent.

### Tuning

Raise limits **per peer**, not globally — a global limit loosened once is never tightened again:

```sql
INSERT INTO delimp_federation_quota (peer, rows_per_day, rows_per_month, note)
VALUES ('broad-pxlab', 1000000, 10000000, 'joint project, agreed 2026-09');
```

Defaults are env-tunable: `FRAN_FED_MAX_ROWS`, `FRAN_FED_ROWS_PER_DAY`,
`FRAN_FED_ROWS_PER_MONTH`, `FRAN_FED_MIN_SEQ_LEN`, `FRAN_FED_NOVELTY_MAX_RATIO`.

### Watching for abuse

```sql
-- spend and novelty per peer, last 24h. A novelty ratio near 1.0 at volume is enumeration.
SELECT peer, sum(n_rows) AS rows_served, count(*) AS requests,
       count(DISTINCT query_hash) AS distinct_queries,
       round(count(DISTINCT query_hash)::numeric / greatest(count(*),1), 3) AS novelty
  FROM delimp_federation_access_log
 WHERE at > now() - interval '1 day' AND decision = 'allow'
 GROUP BY peer ORDER BY rows_served DESC;

-- who is being refused, and why
SELECT peer, decision, count(*) FROM delimp_federation_access_log
 WHERE at > now() - interval '7 days' AND decision <> 'allow'
 GROUP BY 1,2 ORDER BY 3 DESC;
```

Verify the controls before enabling sharing:

```bash
python tests/test_federation_guard.py      # 26 checks, incl. "1 req/s for 24h is stopped"
```

## Rules that keep federated numbers honest

From the design doc, and worth repeating because they are easy to violate by accident:

* **Results are grouped by node, never silently pooled.** A 2021 Orbitrap run and a 2024 manatee
  serum run voting on a dog peptide's RT is noise — the same failure that already lost at
  single-lab scale, with more instruments and more iRT conventions.
* **Aggregates ship sums and counts, never means.** Sums can be recombined and subtracted; means
  cannot. Excluding a node from a consensus is then arithmetic, not a refetch.
* **Deduplicate on content, not on name.** Several labs will have ingested the same public dataset.
* **No federated count on the dashboard.** "335M precursors across 7 labs" cannot be reproduced or
  audited, and it will end up in a grant.
* **Partial results are normal and must be visible.** Every response reports per-peer status so a
  user can see "5 of 7 labs answered". A silently dropped peer is a wrong answer wearing a right
  answer's clothes.
* **Cross-node RT pooling is unvalidated** while `irt_calibration_source` is unpopulated. Expose
  per-node values; do not offer a pooled prior.
