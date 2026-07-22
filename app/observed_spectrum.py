"""Observed MS2 spectrum for a peptide — the REAL measured fragments, read on demand from the
Lance datasets in Azure Blob (franfragments/spectra). 'Layer-0' of the peptide spectrum view:
the actually-acquired spectrum, alongside theoretical (fragments.py) and predicted (koina.py).

Governance / safety:
  * The blob container is PRIVATE; the app reads it server-side via its App Service **managed
    identity** (prod) or a SAS in $FRAN_FRAGMENTS_SAS (dev). Clients never receive a blob URL/SAS.
  * The response is DECOUPLED from source identity: it returns the spectrum (m/z + intensities +
    ion annotations) for a representative best occurrence, NOT which search / sample / customer it
    came from (every search is sharing_status='private', and raw_path embeds customer names).
  * One peptide per request; no bulk path. The route rate-limits and picks a single best occurrence.
"""
from __future__ import annotations

import functools
import os
import threading
import time

from . import db

_ACCOUNT = os.environ.get("FRAN_FRAGMENTS_ACCOUNT", "franfragments")
_CONTAINER = os.environ.get("FRAN_FRAGMENTS_CONTAINER", "spectra")

# --- auth -----------------------------------------------------------------
_tok_lock = threading.Lock()
_tok: dict = {"bearer": None, "exp": 0.0, "epoch": 0}


def _bearer():
    """Cached AAD bearer token from the App Service managed identity (DefaultAzureCredential),
    refreshed ~5 min before expiry. `epoch` bumps on refresh so cached dataset handles rotate."""
    with _tok_lock:
        if _tok["bearer"] and time.time() < _tok["exp"] - 300:
            return _tok["bearer"], _tok["epoch"]
        from azure.identity import DefaultAzureCredential
        t = DefaultAzureCredential().get_token("https://storage.azure.com/.default")
        _tok.update(bearer=t.token, exp=float(t.expires_on), epoch=_tok["epoch"] + 1)
        return _tok["bearer"], _tok["epoch"]


def _storage_options():
    """(options, cache_epoch). Dev: a SAS in $FRAN_FRAGMENTS_SAS (stable, epoch 0). Prod: a managed-
    identity bearer token (epoch rotates on refresh)."""
    sas = os.environ.get("FRAN_FRAGMENTS_SAS")
    if sas:
        return {"account_name": _ACCOUNT, "azure_storage_sas_key": sas}, 0
    bearer, epoch = _bearer()
    return {"account_name": _ACCOUNT, "bearer_token": bearer}, epoch


@functools.lru_cache(maxsize=256)
def _dataset_cached(basename: str, epoch: int, opts_items: tuple):
    import lance
    return lance.dataset(f"az://{_CONTAINER}/{basename}", storage_options=dict(opts_items))


def _dataset(basename: str):
    opts, epoch = _storage_options()
    # include epoch in the key so a rotated token yields a fresh handle (stale ones fall out of LRU)
    return _dataset_cached(basename, epoch, tuple(sorted(opts.items())))


# --- lookup ---------------------------------------------------------------
def _clean_seq(s: str) -> str:
    """Amino-acid letters only — also makes the value safe to embed in the Lance filter string."""
    return "".join(c for c in (s or "").upper() if "A" <= c <= "Z")


def _best_occurrence(seq: str, charge: int | None):
    """One representative precursor of this peptide that HAS a spectrum-lane dataset, ordered by
    best q-value (a high-confidence example). Returns (basename, charge) or None. search_id/raw_path
    are used only to locate the blob — never returned."""
    where = "p.stripped_seq = %s"
    params = [seq]
    if charge:
        where += " AND p.charge = %s"
        params.append(int(charge))
    row = db.query(
        f"""SELECT l.lance_path AS lance_path, p.charge AS charge
            FROM delimp_precursors p
            JOIN delimp_spectrum_lane l ON l.search_id = p.search_id
            WHERE {where} AND l.lance_path IS NOT NULL
            ORDER BY p.best_q_value NULLS LAST
            LIMIT 1""",
        tuple(params),
        tables=["delimp_precursors", "delimp_spectrum_lane"],
        fetch="one",
        timeout_ms=8000,
    )
    if not row:
        return None
    return os.path.basename(str(row["lance_path"]).rstrip("/")), int(row["charge"])


def observed_spectrum(stripped_seq: str, charge: int | None = None) -> dict | None:
    """Measured MS2 spectrum for a representative occurrence of this peptide, or None if we have no
    stored spectrum for it. Identity-decoupled (no search/sample/customer)."""
    seq = _clean_seq(stripped_seq)
    if len(seq) < 2:
        return None
    occ = _best_occurrence(seq, charge)
    if not occ:
        return None
    basename, ch = occ
    cols = ["stripped_seq", "charge", "precursor_mz", "frg_mz", "frg_type", "frg_num",
            "frg_charge", "frg_loss", "frg_measured_relint", "frg_mass_acc_ppm"]
    flt = f"stripped_seq = '{seq}' AND charge = {int(ch)}"  # seq is [A-Z] only -> injection-safe
    tbl = _dataset(basename).scanner(columns=cols, filter=flt, limit=1).to_table().to_pylist()
    if not tbl:
        return None
    r = tbl[0]
    mz = r.get("frg_mz") or []

    def _at(key, i):
        v = r.get(key)
        return v[i] if v is not None and i < len(v) else None

    ions = []
    for i in range(len(mz)):
        ty, num = _at("frg_type", i), _at("frg_num", i)
        relint, ppm = _at("frg_measured_relint", i), _at("frg_mass_acc_ppm", i)
        ions.append({
            "ion": (f"{ty}{num}" if ty and num is not None else None),
            "type": ty, "num": num, "charge": _at("frg_charge", i), "loss": _at("frg_loss", i),
            "mz": round(float(mz[i]), 5),
            "rel_intensity": round(float(relint), 3) if relint is not None else None,
            "mass_acc_ppm": round(float(ppm), 3) if ppm is not None else None,
        })
    ions.sort(key=lambda x: x["mz"])
    return {
        "sequence": seq,
        "charge": r.get("charge"),
        "precursor_mz": round(float(r["precursor_mz"]), 5) if r.get("precursor_mz") is not None else None,
        "n_fragments": len(ions),
        "ions": ions,
        "source": "measured",  # neutral label; no search/customer identity exposed
    }
