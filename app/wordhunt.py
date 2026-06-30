"""Fun: English words / names / spicy words hidden in peptide sequences.

Peptides are written in the 20 amino-acid single-letter codes (ACDEFGHIKLMNPQRSTVWY) — so any word
using only those letters can appear as a substring. (B, J, O, U, X, Z are NOT amino acids, so e.g.
the F-word can't occur.)

Word list = the FULL English dictionary (aa_words.txt, ~50k words len 5-12, filtered to AA-spellable
from /usr/share/dict/words) PLUS curated names + spicy words. With ~50k words we CAN'T loop word-by-word
over millions of peptides, so scan() generates each peptide's substrings and looks them up in the word
dict (substring-set matching) — O(peptides x len x wordlen-range), not O(peptides x words).
"""
import os

AA = set("ACDEFGHIKLMNPQRSTVWY")

# Categories to HIDE from the site (the red/"spicy" chips). Set to an empty set/tuple to restore them.
# This is intentionally a soft switch so the spicy list below stays intact and can be re-enabled later
# with a one-line change (no data loss). Hidden words are dropped from the live scan here AND filtered
# out of the precomputed delimp_word_leaderboard at read time in queries.word_leaderboard().
HIDDEN_CATEGORIES = frozenset({"spicy"})

# curated overlays on top of the dictionary — these CATEGORIES take precedence so e.g. GRACE shows as
# a name and the spicy ones are flagged, instead of being lumped in as a generic "word".
_RAW = {
    "name": (
        "STAN DAN DANA DEAN DENISE GREG CRAIG MARK MARC NEAL NEIL PETE RICK NICK "
        "MIKE DIANE IRENE RENEE GRACE FAITH HEIDI KATE CASEY SARAH HANK FRANK "
        "STEVE STEVEN PETER WALTER CARTER PRESTON TRISTAN VINCENT"
    ),
    "spicy": "CRAP DAMN ARSE FART PISS SHIT SHITE FECK PRICK DICK TWIT TWAT WANK GIT",
}


def _load_dictionary() -> dict[str, str]:
    """The bundled AA-spellable English dictionary (all category 'word')."""
    out: dict[str, str] = {}
    path = os.path.join(os.path.dirname(__file__), "aa_words.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                w = line.strip().upper()
                if w:
                    out[w] = "word"
    except FileNotFoundError:  # degrade to curated-only if the wordlist isn't bundled
        pass
    return out


def _build() -> dict[str, str]:
    out = _load_dictionary()
    for cat in ("name", "spicy"):                 # overlay curated categories (take precedence)
        for w in _RAW[cat].split():
            w = w.strip().upper()
            if len(w) >= 3 and set(w) <= AA:
                out[w] = cat
    if HIDDEN_CATEGORIES:  # hide flagged categories (e.g. spicy) — drop both the curated words AND any
        out = {w: c for w, c in out.items()        # same-spelling dictionary word so they vanish entirely
               if c not in HIDDEN_CATEGORIES and w not in _HIDDEN_WORDS}
    return out


# words to scrub regardless of their dictionary category (the curated members of HIDDEN_CATEGORIES)
_HIDDEN_WORDS = frozenset(
    w.strip().upper() for cat in HIDDEN_CATEGORIES for w in _RAW.get(cat, "").split() if w.strip()
)


WORDS = _build()
_LENS = sorted({len(w) for w in WORDS}) or [5]
MINL, MAXL = _LENS[0], _LENS[-1]


def _load_common_ranked() -> dict:
    """The ~4k most common English words (AA-spellable) in FREQUENCY order → {word: rank} (rank 0 =
    most common). From the google-10000-english frequency list, filtered to amino-acid letters."""
    out = {}
    path = os.path.join(os.path.dirname(__file__), "aa_common.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                w = line.strip().upper()
                if w and w not in out:
                    out[w] = i
    except FileNotFoundError:
        pass
    return out


COMMON_RANK = _load_common_ranked()
COMMON = set(COMMON_RANK)
# pure function words — recognizable but make terrible "phrases" (THAT THIS WITH...), skip in poetry/code
_STOP = {"THAT", "THIS", "WITH", "HAVE", "WILL", "FROM", "THEY", "WERE", "BEEN", "THEIR", "WHAT",
         "WHEN", "THERE", "WHICH", "THESE", "WOULD", "THAN", "THEN", "THEM", "INTO", "ALSO", "SUCH",
         "PAGE", "SEARCH", "SITE", "FREE", "HTTP", "HTML", "LINK", "LINKS", "MAIL", "INFO", "DATA"}


def is_hidden(word: str | None, category: str | None = None) -> bool:
    """True if this word/category is currently hidden from the site (see HIDDEN_CATEGORIES).
    Used to scrub any spicy rows already sitting in the precomputed leaderboard table."""
    if category and category in HIDDEN_CATEGORIES:
        return True
    return (word or "").strip().upper() in _HIDDEN_WORDS


def is_common(word: str, category: str = "word") -> bool:
    """A word is 'common' if it's a curated name/spicy word (intentional) or in the frequency list."""
    return category in ("name", "spicy") or (word or "").upper() in COMMON


def sense_score(words) -> float:
    """Lower = reads more sensibly. Mean frequency-rank of the words (common words score low);
    unknown/acronym words get a big penalty so word-salad of obscure terms sinks."""
    if not words:
        return 1e9
    BIG = 12000
    return sum(COMMON_RANK.get(str(w).upper(), BIG) for w in words) / len(words)


def best_content_word(found_words):
    """From candidate words found in a peptide, pick the most-common CONTENT word (skip filler/stop +
    very short), so assembled 'prophecies' read as recognizable nouns rather than THAT/THIS/WITH soup."""
    cand = [w for w in found_words if len(w) >= 4 and w.upper() not in _STOP]
    if not cand:
        return None
    return min(cand, key=lambda w: COMMON_RANK.get(w.upper(), 99999))


def scan(peptides) -> list[dict]:
    """peptides: iterable of {stripped_seq, n_obs}. Returns a leaderboard of words found, with how
    many DISTINCT peptides contain each + total observations. Substring-set matching (fast for ~50k
    words). Counts each word at most once per peptide."""
    agg: dict[str, dict] = {}
    words = WORDS
    minl, maxl = MINL, MAXL
    for p in peptides:
        seq = (p.get("stripped_seq") or "").upper()
        L = len(seq)
        if L < minl:
            continue
        obs = int(p.get("n_obs") or 0)
        seen: set[str] = set()
        for i in range(L - minl + 1):
            kmax = min(maxl, L - i)
            for k in range(minl, kmax + 1):
                sub = seq[i:i + k]
                cat = words.get(sub)
                if cat is not None and sub not in seen:
                    seen.add(sub)
                    a = agg.get(sub)
                    if a is None:
                        a = agg[sub] = {"word": sub, "category": cat, "n_peptides": 0,
                                        "n_obs": 0, "example": seq}
                    a["n_peptides"] += 1
                    a["n_obs"] += obs
                    if len(seq) < len(a["example"]):
                        a["example"] = seq
    out = list(agg.values())
    out.sort(key=lambda x: (-x["n_obs"], -x["n_peptides"], x["word"]))
    return out
