"""instrument_labels.py — canonical instrument model/serial, shared by ingest and the backfill.

The corpus accumulated four ways of splitting one physical instrument across several labels. None
are typos in our code: they are what different vendor/export paths wrote, and they silently break
every per-instrument aggregate (including anything trained per-instrument off the Lance lanes).

WHAT WAS WRONG, measured 2026-08-25 over 20,988 runs:

  1. Whitespace.   ' timsTOF Pro' (2,198 runs) and 'timsTOF Pro' (10) are the same instrument,
                   same serial 1854399.00153, split by a leading space.
  2. Serial zero-padding. '1854399.153' (240) and '1854399.00153' (2,208) are the same timsTOF Pro.
  3. Serial case.  'FSN20215' (481) and 'fsn20215' (2,403) are the same Fusion Lumos.
  4. Model wrong.  Serial MA10354C carried THREE model labels: 'Orbitrap Exploris 480' (3,200),
                   'Orbitrap Exploris 120' (852) and 'Orbitrap Exploris Slot #10354' (41), with
                   overlapping date ranges. Two different models cannot share a serial. Brett
                   confirmed the core runs exactly one Exploris 480, so all 893 non-480 rows on
                   that serial are mislabels of it.

DELIBERATELY NOT "FIXED": serial MA10140C ('Orbitrap Exploris Slot #0140', 22 runs). It is a
DIFFERENT serial, and every one of those runs is under
service/off_campus/DUKE/MAtt_foster_hupo_2023 -- an external collaborator's instrument, not ours.
"We only have one 480" is authority over OUR instruments and says nothing about Duke's, so the slot
suffix is dropped to a neutral 'Orbitrap Exploris' rather than upgraded to a model tier nobody has
confirmed. Guessing here would invent a fact about someone else's hardware.
"""
from __future__ import annotations

# Serial spellings that denote the same physical instrument -> the canonical one (the majority
# spelling, to minimise churn).
SERIAL_CANON = {
    "1854399.153": "1854399.00153",   # timsTOF Pro, zero-padding variant
    "FSN20215": "fsn20215",           # Fusion Lumos, case variant
}

# Serial -> the instrument it actually is. Only for serials whose identity is established.
MODEL_BY_SERIAL = {
    "MA10354C": "Orbitrap Exploris 480",
}

# "Orbitrap Exploris Slot #10354" is a configured MACHINE NAME (it embeds the serial digits), not a
# model. Where the serial does not identify the model, keep the family and drop the slot id.
_SLOT_PREFIX = "Orbitrap Exploris Slot #"


def normalize(model: str | None, serial: str | None) -> tuple[str | None, str | None]:
    """(model, serial) -> canonical (model, serial). Pure, idempotent, and safe on None."""
    model = (model or "").strip() or None
    serial = (serial or "").strip() or None
    if serial:
        serial = SERIAL_CANON.get(serial, serial)
    # A known serial is stronger evidence than the model string that came with the file: the model
    # string is what disagreed with itself in the first place.
    if serial and serial in MODEL_BY_SERIAL:
        return MODEL_BY_SERIAL[serial], serial
    if model and model.startswith(_SLOT_PREFIX):
        model = "Orbitrap Exploris"
    return model, serial


# The platform a model implies. Instrument metadata beats the file-extension inference that
# `corpus_ingest._platform_from_disk` falls back to: 29 runs carried an Orbitrap model with
# platform='timstof' (22 of them an external Orbitrap whose files are named ".d"), and every one had
# NO ion-mobility range at all -- which a genuine timsTOF run always has.
_PLATFORM_PREFIX = (
    ("timsTOF", "timstof"),
    ("Orbitrap", "orbitrap"),
    ("Q Exactive", "orbitrap"),
    ("Exploris", "orbitrap"),
    ("Fusion", "orbitrap"),
)


def platform_for_model(model: str | None) -> str | None:
    """The platform implied by a model name, or None when the model does not say.

    Returning None rather than a guess matters: the caller uses this only to RESOLVE A
    CONTRADICTION, never to fill a blank, so an unrecognised model must leave the existing value
    alone rather than overwrite it.
    """
    m = (model or "").strip()
    for prefix, plat in _PLATFORM_PREFIX:
        if m.startswith(prefix):
            return plat
    return None
