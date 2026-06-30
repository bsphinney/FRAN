"""Koina (Wilhelm lab) fragment-intensity predictions for a peptide precursor.

Calls the public Koina inference API (https://koina.wilhelmlab.org) for several ML
predictors (Prosit, AlphaPeptDeep, ms2pip) and returns each model's predicted b/y
fragment intensities + m/z, plus an across-model AVERAGE. This lets FRAN show, next to
the search's own spectral-library intensities, what independent models predict — and how
much the models agree. Verified API: outputs are intensities/mz/annotation arrays.

Honesty notes surfaced to the UI: predicted intensities depend on collision energy (we
assume a default) and instrument type, so they are 'predicted at CE≈x', not measured.
External service -> cached, and each model degrades independently on failure.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request

_BASE = "https://koina.wilhelmlab.org/v2/models"
_ANN = re.compile(r"^([aby])(\d+)\+(\d+)$", re.I)

# input requirements per model (verified against the Koina model metadata)
MODELS = {
    "Prosit_2020_intensity_HCD": {"ce": True, "instrument": False},
    "AlphaPeptDeep_ms2_generic": {"ce": True, "instrument": True},
    "ms2pip_HCD2021": {"ce": False, "instrument": False},
}
DEFAULT_MODELS = list(MODELS)

_cache: dict[str, list | None] = {}
_lock = threading.Lock()


def _infer(model: str, seq: str, charge: int, ce: float, instrument: str) -> list | None:
    spec = MODELS.get(model, {"ce": True, "instrument": False})
    inputs = [
        {"name": "peptide_sequences", "shape": [1, 1], "datatype": "BYTES", "data": [seq]},
        {"name": "precursor_charges", "shape": [1, 1], "datatype": "INT32", "data": [int(charge)]},
    ]
    if spec.get("ce"):
        inputs.append({"name": "collision_energies", "shape": [1, 1], "datatype": "FP32", "data": [float(ce)]})
    if spec.get("instrument"):
        inputs.append({"name": "instrument_types", "shape": [1, 1], "datatype": "BYTES", "data": [instrument]})
    body = json.dumps({"id": "0", "inputs": inputs}).encode()
    try:
        req = urllib.request.Request(f"{_BASE}/{model}/infer", data=body,
                                     headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    except Exception:  # noqa: BLE001 - model/service unavailable -> skip this model
        return None
    out = {o["name"]: o["data"] for o in d.get("outputs", [])}
    ann, mz, inten = out.get("annotation"), out.get("mz"), out.get("intensities")
    if not (ann and mz and inten):
        return None
    peaks = []
    mx = max((float(v) for v in inten), default=0.0) or 1.0
    for i in range(len(ann)):
        iv = float(inten[i])
        if iv <= 0:
            continue
        label = ann[i].decode() if isinstance(ann[i], (bytes, bytearray)) else str(ann[i])
        m = _ANN.match(label)
        if not m:
            continue
        peaks.append({"ion": f"{m.group(1).lower()}{m.group(2)}", "fragment_charge": int(m.group(3)),
                      "label": f"{m.group(1).lower()}{m.group(2)}^{m.group(3)}",
                      "mz": round(float(mz[i]), 4), "rel_intensity": round(iv / mx, 4)})
    peaks.sort(key=lambda p: -p["rel_intensity"])
    return peaks


_PFLY_MODEL = "pfly_2024_fine_tuned"


def _pfly_score(probs: list[float]) -> float:
    """PFly returns a softmax over 4 flyability classes (class 1 = poor flyer, class 4 =
    strong flyer). Collapse to a single 0-1 flyability = (expected_class - 1) / 3."""
    if not probs or len(probs) < 4:
        return 0.0
    exp = sum((i + 1) * float(probs[i]) for i in range(4))
    return round(max(0.0, min(1.0, (exp - 1.0) / 3.0)), 4)


def predict_flyability(sequences: list[str], batch: int = 100) -> dict[str, dict]:
    # NOTE: the Koina PFly model rejects batches >=500 with HTTP 400 — keep batch <=100.
    """Map stripped peptide sequence -> {'score':0-1, 'classes':[p1,p2,p3,p4]} via the
    Koina PFly model. Flyability is sequence-intrinsic (no charge/CE), so we cache by
    sequence and can score the whole corpus in a handful of batched calls. Missing/failed
    sequences are simply absent from the returned dict."""
    out: dict[str, dict] = {}
    todo = []
    for s in sequences:
        s = (s or "").strip().upper()
        if not s or s in out or s in todo:
            continue
        with _lock:
            cached = _cache.get(f"pfly|{s}", "MISS")
        if cached != "MISS":
            if cached is not None:
                out[s] = cached
        else:
            todo.append(s)
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        body = json.dumps({"id": "0", "inputs": [
            {"name": "peptide_sequences", "shape": [len(chunk), 1], "datatype": "BYTES", "data": chunk}]}).encode()
        try:
            req = urllib.request.Request(f"{_BASE}/{_PFLY_MODEL}/infer", data=body,
                                         headers={"Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
            data = d["outputs"][0]["data"]
        except Exception:  # noqa: BLE001 - service unavailable -> skip this chunk
            continue
        for j, s in enumerate(chunk):
            probs = [float(x) for x in data[j * 4:(j + 1) * 4]]
            rec = {"score": _pfly_score(probs), "classes": [round(p, 4) for p in probs]}
            out[s] = rec
            with _lock:
                _cache[f"pfly|{s}"] = rec
    return out


def predict(seq: str, charge: int, models: list[str] | None = None,
            ce: float = 28.0, instrument: str = "Lumos") -> dict:
    seq = (seq or "").strip().upper()
    models = models or DEFAULT_MODELS
    if len(seq) < 2:
        return {"sequence": seq, "models": {}, "average": [], "ce": ce}
    per_model = {}
    for model in models:
        ck = f"{model}|{seq}|{charge}|{ce}|{instrument}"
        with _lock:
            cached = _cache.get(ck, "MISS")
        if cached == "MISS":
            cached = _infer(model, seq, charge, ce, instrument)
            with _lock:
                _cache[ck] = cached
        if cached is not None:
            per_model[model] = cached
    # across-model average: align by label (already max-normalized per model), mean over
    # the models that returned it; also count agreement (how many models predict each ion).
    agg: dict[str, dict] = {}
    for model, peaks in per_model.items():
        for p in peaks:
            a = agg.setdefault(p["label"], {"label": p["label"], "ion": p["ion"],
                                            "fragment_charge": p["fragment_charge"], "mz": p["mz"],
                                            "sum": 0.0, "n": 0})
            a["sum"] += p["rel_intensity"]; a["n"] += 1
    n_models = max(len(per_model), 1)
    average = sorted(
        ({"label": a["label"], "ion": a["ion"], "fragment_charge": a["fragment_charge"], "mz": a["mz"],
          "rel_intensity": round(a["sum"] / n_models, 4), "n_models_agree": a["n"]} for a in agg.values()),
        key=lambda x: -x["rel_intensity"],
    )
    return {"sequence": seq, "charge": charge, "ce": ce, "instrument": instrument,
            "models": per_model, "model_names": list(per_model), "average": average}
