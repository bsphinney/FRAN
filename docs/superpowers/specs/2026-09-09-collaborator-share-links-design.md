# Share links: giving a collaborator their data without a UC login

**Date:** 2026-09-09
**Status:** design, pending Brett's review. **Not started.**

## The problem

Brett's words: *"I want to build in a sharing link like how artifacts and notion pages work so I can
send collaborators a direct link to their search in FRAN without them having to log in, as some will
be off campus and not in the UC CAS system."*

FRAN's tiered portal already models exactly this person. `app/auth.py:resolve_access()` returns one
of three scopes:

| tier | who | sees |
|---|---|---|
| `full` | Core staff (Entra group / allow-list) | everything |
| `lab` | a logged-in user whose email matches a CoreOmics `pi_email`/`submitter_email` | **only their own submissions**, carried as `submission_ids` |
| `public` | anonymous, or logged in and matched to nothing | sanitised aggregate corpus |

So the *concept* is built: a collaborator seeing only their own data is the `lab` tier, and it is
already scoped by an explicit `submission_ids` list. The gap is purely the **door**: `lab` requires an
Entra/CAS principal, which an off-campus collaborator cannot get.

**A share link is therefore not a new authorisation model. It is a second way to arrive at a scope
that already exists.** That framing is the whole design, and it is why this is small.

## The alternative Brett raised, and why it is rejected

> *"Another alternative is to use their coreomics username and password. https://ucdavis.coreomics.com/"*

Rejected, on a measured fact rather than a preference. FRAN authenticates to CoreOmics with a
**Django REST service token** (`ingest/coreomics_import.py:48` — `Authorization: Token <40 hex>`,
read from a file, never a flag). That token identifies **FRAN**, not an end user. It can fetch
submission records; it cannot verify that the person in front of us is who they claim to be.

Making CoreOmics credentials work would need one of:

- **FRAN collects the password and proxies it** to `ucdavis.coreomics.com` to check. This means a
  second system handling users' plaintext passwords for a system it does not own. Not building that.
- **CoreOmics exposes OAuth / OIDC.** If it does, this becomes a genuinely good option and is worth
  revisiting — a collaborator would log in *at CoreOmics*, FRAN would never see the password, and
  the email in the returned claims feeds straight into the existing `lab` matching. **OPEN QUESTION
  for Brett: does CoreOmics support OAuth, or is a Django REST token the only auth it offers?** If
  OAuth exists, spec a second option before building this one.

Share links avoid credentials entirely, so they work regardless of that answer.

## Design

### 1. A token is a stored grant, not a signed claim

```
delimp_share_link
    token           TEXT PRIMARY KEY   -- 32+ bytes from secrets.token_urlsafe(), never sequential
    scope_kind      TEXT               -- 'search' | 'submission'
    scope_id        TEXT               -- search_id, or coreomics submission_id
    label           TEXT               -- free text: who it was made for, shown in the admin list
    created_by      TEXT               -- the full-tier email that minted it
    created_at      TIMESTAMPTZ
    expires_at      TIMESTAMPTZ        -- NOT NULL. No unlimited links.
    revoked_at      TIMESTAMPTZ        -- NULL until revoked
    last_used_at    TIMESTAMPTZ
    n_uses          INTEGER
```

**Stored random token, not a signed JWT.** A JWT is stateless and cannot be revoked without a
denylist, which is a stored table anyway — so the stateless version buys nothing and costs the
ability to answer "who did I share this with, and can I turn it off". Revocation is the feature.

`expires_at` is `NOT NULL` by construction. A share link with no expiry is a permanent credential
sitting in someone's inbox forever. Default 90 days, settable at mint time, hard maximum one year.

### 2. `resolve_access()` gains one branch

The token arrives as a path segment: `/s/<token>`, which sets a scoped session cookie and redirects
to the target page. **Not a query parameter** — those land in server logs, browser history and
`Referer` headers. Serve the redirect with `Referrer-Policy: no-referrer`.

`resolve_access()` then returns, for a valid unexpired unrevoked token:

```python
{"tier": "lab", "email": None, "submission_ids": [<the granted ids>], "via": "share"}
```

That is the shape it already returns for a matched lab user, so **every existing consumer of the
scope works unchanged** — this is the reason to reuse the tier rather than invent a fourth. The
`via` field exists so the UI can say "you are viewing a shared link" and so audit can distinguish.

Fail-closed exactly as today: an unknown, expired or revoked token yields `public`, never an error
that confirms the token existed.

### 3. What a share-link holder can see

Precisely what a `lab` user with the same `submission_ids` sees, and nothing more. No widening.

Two things to decide deliberately rather than inherit:

- **Filename privacy.** A `lab` user currently gets `privacy.set_reveal()` true, so they see real
  acquisition filenames. That is right for the PI whose samples they are. It is equally right for a
  collaborator they chose to share with — but it must be a decision, not an accident, because the
  same mechanism is what keeps filenames off the public site.
- **The three exports.** `/api/export/diann_report/{search_id}` and the two brief endpoints are
  `is_full()`-gated today, i.e. staff-only. A collaborator receiving a link to their search will
  reasonably want the `report.parquet`. **OPEN QUESTION for Brett:** should a share link carry the
  data exports? It is the single most useful thing on the page for a collaborator, and also the most
  complete copy of their data.

### 4. Minting and revoking

A `full`-tier-only admin surface: mint a link for the search or submission currently open, with a
label and an expiry; list every live link with its label, target, creator, expiry and use count;
revoke with one click. Revocation must be immediate — a stored token makes that a single UPDATE.

## Constraints

- **Bearer semantics, stated plainly in the UI at mint time.** Anyone holding the URL has the access.
  It cannot be otherwise for a link that works without login. The mitigations are narrow scope,
  mandatory expiry, revocability and audit — not secrecy of the URL.
- **Never in a query string.** Path segment only, `Referrer-Policy: no-referrer`, `noindex`.
- **Constant-time comparison** on token lookup, and no distinction in the response between "no such
  token" and "expired" — both are `public`.
- **Rate-limit `/s/<token>`** so the space cannot be probed, even though 32 random bytes is not
  brute-forceable.
- Every `query()` passes `tables=[...]`. `delimp_share_link` is INTERNAL — it must never be readable
  through the public query layer.
- **Every test must be proven able to fail.** This plan family has produced thirteen defects of one
  shape — an assertion whose witness could not discriminate. On an auth boundary that is not a
  quality issue, it is the whole thing: a test that "passes" against a broken gate is worse than no
  test. Specifically, the suite must exercise the **anonymous** path with `reveal=False`; the
  heatmap work found that FRAN's existing matrix tests all ran with `DELIMP_INTERNAL_MODE=1` and had
  therefore never tested what a public visitor sees.

## Risks

| risk | mitigation |
|---|---|
| A link is forwarded beyond the intended recipient | Narrow scope, mandatory expiry, revocation, use-count visible so unexpected traffic shows |
| A link outlives the collaboration | `expires_at NOT NULL`, 90-day default, one-year hard cap |
| Scope creep from "one search" to "everything that lab has" | `scope_kind`/`scope_id` are explicit per link; no wildcard grants |
| The `lab` tier changes later and silently widens what links expose | Share links reuse the tier deliberately; a test must pin what a share scope can reach, so widening `lab` fails loudly |
| Token in logs | Path segment, `no-referrer`, and never logged by the access-log middleware — verify that, do not assume it |

## Deliberately out of scope

- Per-recipient identity. A link is a bearer grant; if we need to know *who* viewed, that is
  accounts, which is the thing this exists to avoid.
- Editing or upload by link holders. Read-only.
- Email delivery. Brett sends the link himself, as he does today.

## Open questions for Brett

1. **Does CoreOmics support OAuth/OIDC?** If yes, that is a materially better door than links for
   collaborators who already have CoreOmics accounts, and worth speccing alongside.
2. **Should a share link carry the three data exports** (`report.parquet`, the two briefs), or view
   only?
3. **Scope granularity** — one search per link, or the whole submission? A submission is closer to
   how a collaborator thinks about "my project"; a search is closer to what Brett is looking at when
   he decides to share.
