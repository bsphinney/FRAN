"""Fun: English words / names / spicy words hidden in peptide sequences.

Peptides are written in the 20 amino-acid single-letter codes
(ACDEFGHIKLMNPQRSTVWY) — so any word using only those letters can appear as a
substring. (B, J, O, U, X, Z are NOT amino acids, so e.g. the F-word can't occur.)
Curated lists below are auto-filtered to AA-spellable + length>=3.
"""
AA = set("ACDEFGHIKLMNPQRSTVWY")

_RAW = {
    "word": (
        "LIVE LIVER DEAD HEART FACE CAFE MEAT MEAL SEAT SEED FEED FEET HEAD HAND "
        "LAND SAND SAFE LIFE WIFE WILD WIND WINE VINE FINE MINE NICE RICE RACE PACE "
        "PEACE SPACE GRACE TRACE CREAM DREAM STEAM GREAT TREAT WHEAT HEAT CHEAT "
        "CHEAP SHEEP SHEET GREEN SCREEN SPEED GREED FREE THREE TEETH FAITH EARTH "
        "SMART START CHART SHARP SHARE SPARE SCARE STARE SWEAT GENE CELL ACID LIPID "
        "HEME WATER LATER LASER PAPER PHASE CHASE THEME SCENE SENSE DANCE FANCY "
        "CANDY HANDY CRANE PLANE PLANT GIANT SAINT PAINT PRINT DRIFT SWIFT SWEEP "
        "STEEP STREET SWEET FLEET GREET STRAW DRAW DREW GREW CREW DRIP TRIP GRIP "
        "STRIP SCRAP SCRAPE STREAM DREAM SECRET PERFECT RESPECT PRACTICE"
    ),
    "name": (
        "STAN DAN DANA DEAN DENISE GREG CRAIG MARK MARC NEAL NEIL PETE RICK NICK "
        "MIKE DIANE IRENE RENEE GRACE FAITH HEIDI KATE CASEY SARAH HANK FRANK "
        "STEVE STEVEN PETER WALTER CARTER PRESTON TRISTAN VINCENT"
    ),
    "spicy": "CRAP DAMN ARSE FART PISS SHIT SHITE FECK PRICK DICK TWIT TWAT WANK GIT",
}


def _build():
    out = {}
    for cat, words in _RAW.items():
        for w in words.split():
            w = w.strip().upper()
            if len(w) >= 3 and set(w) <= AA and w not in out:
                out[w] = cat
    return out


WORDS = _build()


def scan(peptides: list[dict]) -> list[dict]:
    """peptides: [{stripped_seq, n_obs}]. Returns a leaderboard of words found,
    with how many distinct peptides contain each and total observations."""
    agg: dict[str, dict] = {}
    for p in peptides:
        seq = (p.get("stripped_seq") or "").upper()
        if not seq:
            continue
        obs = int(p.get("n_obs") or 0)
        for w, cat in WORDS.items():
            if w in seq:
                a = agg.get(w)
                if a is None:
                    a = agg[w] = {"word": w, "category": cat, "n_peptides": 0,
                                  "n_obs": 0, "example": seq}
                a["n_peptides"] += 1
                a["n_obs"] += obs
                if len(seq) < len(a["example"]):
                    a["example"] = seq
    out = list(agg.values())
    out.sort(key=lambda x: (-x["n_obs"], -x["n_peptides"], x["word"]))
    return out
