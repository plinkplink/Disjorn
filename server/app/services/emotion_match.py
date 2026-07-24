"""Deterministic emotion-tag → pack-emotion matcher (chibi resolution ladder).

Bots tag whatever emotion word fits the moment ("[emotion: Wry]"); packs hold
a finite set of PNGs. Boxing the bot into the pack's exact vocabulary costs
per-turn context and steering effort, so the server absorbs the mismatch here
instead: a pure-function ladder maps free-form tags onto pack filenames with
no model, no network, and no randomness — same tag + same pack always yields
the same chibi.

The ladder (first hit wins; chibi.resolve() tries its exact-match index
before calling in, so nothing here can shadow a pack's real filename):

  1. PACK ALIASES — `Aliases.txt` in the pack dir, `alias -> Target` lines.
     The pack owner's taste knob: which face "Wry" gets is an aesthetic
     judgment, so packs override the built-ins without touching code.
  2. LEXICON — the built-in synonym table below (generic emotion word →
     ordered candidate list). Generic on both sides so any pack benefits.
  3. STEMMING — inflection folding, both directions: Shrug ↔ Shrugging,
     Laughs → Laughing, Amusement → Amused.
  4. FUZZY — difflib at a high cutoff, typo rescue only (Schemeing →
     Scheming). Skipped for short tags where near-misses are ambiguous.
  5. TOKENS — multi-word tags retry each word through steps 1-3 left to
     right ("evil grin" → evil → Scheming). Compounds like "heart eyes"
     never get here: normalization already folds them to `hearteyes`.

Misses still resolve to None (messages.py drops the emote silently, as
ever), but chibi.resolve() logs them — tune the lexicon from observed
misses, not imagined ones.

Alias targets and lexicon candidates are resolved exact-then-stemmed against
the pack index; a candidate naming an emote the pack lacks simply falls
through to the next, so one lexicon serves packs of different depths.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Iterable, Optional

_NORM_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[\s\-_]+")

# Minimum stem length left after suffix-stripping; shorter stems ("s" from
# "es") match everything and nothing.
_MIN_STEM = 3

# Fuzzy matching guards: tags shorter than this skip step 4 entirely, and
# candidates must clear the cutoff. 0.86 lets one-typo words through while
# keeping distinct emotions (sad/mad = 0.67) apart.
_FUZZY_MIN_LEN = 5
_FUZZY_CUTOFF = 0.86


def normalize(name: str) -> str:
    """Case-insensitive key: strip everything but letters and digits, so
    quotes and stray punctuation ('"Fired Up"') can't sink a tag. Lowercase
    FIRST — the character class only knows lowercase.

    Byte-compatible with chibi._normalize — the two must agree or ladder
    hits would name keys the index doesn't have.
    """
    return _NORM_RE.sub("", name.lower())


# ---------------------------------------------------------------------------
# Built-in lexicon: normalized alias -> ordered candidate emotions.
#
# Candidates are generic emotion words (normalized), tried in order until one
# resolves against the pack at hand. Order encodes judgment: the first
# candidate is the best face for the word, later ones are fallbacks for
# shallower packs. Keep entries alphabetical within their block; keep the
# table boring — cleverness belongs in pack Aliases.txt files.
# ---------------------------------------------------------------------------

LEXICON: dict[str, tuple[str, ...]] = {
    # -- happy / confident -------------------------------------------------
    "amped": ("pumped", "chargedup"),
    "beaming": ("cheerful", "happy"),
    "cackling": ("laughing", "scheming"),
    "cheeky": ("mischievous", "sly", "smug"),
    "chuckling": ("amused", "laughing"),
    "chuffed": ("proud", "happy"),
    "cocky": ("smug", "confident"),
    "delighted": ("cheerful", "happy"),
    "eager": ("excited", "hopeful"),
    "elated": ("ecstatic", "happy"),
    "energized": ("pumped", "chargedup", "excited"),
    "enthusiastic": ("excited", "pumped"),
    "firedup": ("pumped", "chargedup", "excited"),
    "giddy": ("excited", "goofy"),
    "giggling": ("snickering", "laughing"),
    "glad": ("happy", "cheerful"),
    "gleeful": ("ecstatic", "cheerful"),
    "grin": ("smug", "happy"),
    "grinning": ("smug", "happy"),
    "hyped": ("pumped", "excited"),
    "hysterical": ("cryinglaughing", "laughing"),
    "jazzed": ("excited", "pumped"),
    "jolly": ("cheerful", "happy"),
    "joyful": ("happy", "ecstatic"),
    "lol": ("laughing", "amused"),
    "motivated": ("determined", "pumped"),
    "overjoyed": ("ecstatic", "happy"),
    "playful": ("mischievous", "goofy"),
    "pleased": ("content", "happy"),
    "psyched": ("pumped", "excited"),
    "raring": ("pumped", "excited"),
    "rofl": ("cryinglaughing", "laughing"),
    "sassy": ("smug", "sly"),
    "satisfied": ("content", "proud"),
    "silly": ("goofy", "mischievous"),
    "smartass": ("smug", "sly"),
    "smirk": ("sly", "smug"),
    "smirking": ("sly", "smug"),
    "stoked": ("excited", "pumped"),
    "teasing": ("mischievous", "sly", "flirty"),
    "thrilled": ("excited", "ecstatic"),
    "victorious": ("triumphant", "proud"),
    "vindicated": ("smug", "triumphant"),
    "winning": ("triumphant", "confident"),
    "wry": ("sly", "amused", "smug"),
    # -- calm / content ----------------------------------------------------
    "calm": ("content", "relieved"),
    "chill": ("content", "relaxed"),
    "contemplative": ("focused", "dreamy"),
    "cozy": ("wholesome", "content"),
    "daydreaming": ("dreamy", "content"),
    "mellow": ("content", "dreamy"),
    "musing": ("dreamy", "curious"),
    "peaceful": ("content", "dreamy"),
    "pensive": ("focused", "melancholy"),
    "phew": ("relieved",),
    "relaxed": ("content", "relieved"),
    "serene": ("content", "dreamy"),
    "thoughtful": ("focused", "curious"),
    "whew": ("relieved",),
    "wistful": ("nostalgic", "dreamy", "melancholy"),
    # -- affection / warm --------------------------------------------------
    "affectionate": ("adoring", "smitten"),
    "appreciative": ("grateful",),
    "blushing": ("bashful", "embarrassed"),
    "caring": ("sympathetic", "wholesome"),
    "charmed": ("smitten", "amused"),
    "compassionate": ("sympathetic", "wholesome"),
    "coy": ("bashful", "flirty"),
    "crush": ("smitten", "adoring"),
    "enamored": ("smitten", "adoring"),
    "encouraging": ("hopeful", "sympathetic"),
    "fond": ("adoring", "smitten"),
    "loving": ("adoring", "love", "smitten"),
    "moved": ("grateful", "wholesome"),
    "optimistic": ("hopeful",),
    "please": ("pleading", "begging"),
    "puppyeyes": ("pleading", "adoring"),
    "seductive": ("flirty", "smoldering"),
    "smoldering": ("flirty",),
    "supportive": ("sympathetic", "hopeful"),
    "thankful": ("grateful",),
    "touched": ("grateful", "wholesome"),
    "warm": ("wholesome", "adoring"),
    # -- curiosity / cunning -----------------------------------------------
    "analyzing": ("focused", "curious"),
    "brainstorming": ("inspired", "scheming"),
    "concentrating": ("focused", "determined"),
    "conspiratorial": ("scheming", "sneaky"),
    "cunning": ("scheming", "sly"),
    "devious": ("scheming", "mischievous"),
    "doubtful": ("skeptical", "suspicious"),
    "driven": ("determined", "focused"),
    "dubious": ("skeptical", "suspicious"),
    "eureka": ("inspired", "mindblown"),
    "evil": ("scheming", "mischievous"),
    "hmm": ("curious", "skeptical"),
    "inquisitive": ("curious", "fascinated"),
    "interested": ("curious", "fascinated"),
    "intrigued": ("curious", "fascinated"),
    "investigating": ("focused", "curious"),
    "leery": ("suspicious", "skeptical"),
    "lockedin": ("focused", "determined"),
    "nosy": ("curious", "sneaky"),
    "plotting": ("scheming", "sneaky"),
    "resolute": ("determined", "confident"),
    "sideeye": ("suspicious", "skeptical"),
    "stealthy": ("sneaky",),
    "sus": ("suspicious",),
    "troubled": ("concerned", "anxious"),
    "unconvinced": ("skeptical", "unimpressed"),
    "uneasy": ("concerned", "nervous"),
    "wary": ("suspicious", "concerned"),
    "wicked": ("mischievous", "scheming"),
    "wondering": ("curious", "confused"),
    "worried": ("concerned", "anxious"),
    # -- surprise ----------------------------------------------------------
    "alarmed": ("shocked", "scared"),
    "amazed": ("awestruck", "mindblown"),
    "astonished": ("shocked", "awestruck"),
    "astounded": ("shocked", "awestruck"),
    "blownaway": ("mindblown", "awestruck"),
    "flabbergasted": ("shocked", "disbelief"),
    "impressed": ("awestruck", "proud"),
    "incredulous": ("disbelief", "skeptical"),
    "jawdrop": ("shocked", "gasping"),
    "omg": ("shocked", "gasping"),
    "speechless": ("shocked", "disbelief"),
    "startled": ("surprised", "shocked"),
    "stunned": ("shocked", "mindblown"),
    "what": ("confused", "shocked"),
    "whoa": ("surprised", "awestruck"),
    "wow": ("awestruck", "surprised"),
    # -- anger -------------------------------------------------------------
    "aggravated": ("frustrated", "annoyed"),
    "bitter": ("spiteful", "jealous"),
    "cranky": ("annoyed", "impatient"),
    "enraged": ("furious", "angry"),
    "envious": ("jealous",),
    "exasperated": ("frustrated", "eyeroll"),
    "fuming": ("furious", "angry"),
    "grr": ("angry", "annoyed"),
    "grumpy": ("annoyed", "pouting"),
    "huffy": ("pouting", "annoyed"),
    "indignant": ("offended", "annoyed"),
    "insulted": ("offended", "hurt"),
    "irate": ("furious", "angry"),
    "irked": ("annoyed",),
    "irritated": ("annoyed", "frustrated"),
    "livid": ("furious",),
    "mad": ("angry", "annoyed"),
    "miffed": ("annoyed",),
    "outraged": ("offended", "furious"),
    "peeved": ("annoyed",),
    "resentful": ("spiteful", "jealous"),
    "salty": ("annoyed", "spiteful"),
    "seething": ("furious", "spiteful"),
    "vengeful": ("spiteful", "scheming"),
    "wounded": ("hurt", "sad"),
    # -- disgust / disapproval ---------------------------------------------
    "appalled": ("disgusted", "horrified"),
    "contempt": ("unimpressed", "disapproving"),
    "deadpan": ("unimpressed",),
    "disdain": ("unimpressed", "disapproving"),
    "dismissive": ("unimpressed", "eyeroll"),
    "droll": ("amused", "sly"),
    "ew": ("disgusted",),
    "eww": ("disgusted",),
    "facepalm": ("cringing", "eyeroll"),
    "gross": ("disgusted",),
    "grossedout": ("disgusted",),
    "ick": ("disgusted",),
    "ironic": ("sly", "amused"),
    "judging": ("disapproving", "unimpressed"),
    "judgy": ("disapproving", "unimpressed"),
    "meh": ("unimpressed", "bored"),
    "oof": ("cringing", "sympathetic"),
    "repulsed": ("disgusted",),
    "revolted": ("disgusted",),
    "sardonic": ("sly", "unimpressed"),
    "scandalized": ("shocked", "disapproving"),
    "scornful": ("disapproving", "unimpressed"),
    "smh": ("disapproving", "eyeroll"),
    "sneer": ("disapproving", "smug"),
    "ugh": ("eyeroll", "disgusted"),
    "whatever": ("eyeroll", "unimpressed"),
    "yikes": ("cringing", "nervous"),
    "yuck": ("disgusted",),
    # -- confusion / neutral -----------------------------------------------
    "ambivalent": ("shrugging", "confused"),
    "baffled": ("confused", "disbelief"),
    "bemused": ("amused", "confused"),
    "bewildered": ("confused", "shocked"),
    "conflicted": ("confused", "concerned"),
    "disoriented": ("dizzy", "confused"),
    "dunno": ("shrugging", "confused"),
    "huh": ("confused",),
    "idk": ("shrugging", "confused"),
    "indifferent": ("shrugging", "unimpressed"),
    "lost": ("confused",),
    "neutral": ("shrugging", "content"),
    "perplexed": ("confused",),
    "puzzled": ("confused", "curious"),
    "torn": ("confused", "concerned"),
    "uncertain": ("confused", "concerned"),
    "unsure": ("confused", "nervous"),
    # -- sadness -----------------------------------------------------------
    "bawling": ("sobbing", "crying"),
    "blue": ("melancholy", "sad"),
    "brooding": ("sulking", "melancholy"),
    "bummed": ("disappointed", "sad"),
    "crushed": ("devastated", "heartbroken"),
    "depressed": ("miserable", "despair"),
    "dismayed": ("disappointed", "concerned"),
    "down": ("sad", "melancholy"),
    "gloomy": ("melancholy", "sulking"),
    "grieving": ("heartbroken", "sobbing"),
    "homesick": ("nostalgic", "lonely"),
    "letdown": ("disappointed", "sad"),
    "longing": ("yearning", "wistful"),
    "mopey": ("sulking", "pouting"),
    "mourning": ("heartbroken", "melancholy"),
    "pining": ("yearning", "smitten"),
    "regretful": ("guilty", "disappointed"),
    "rueful": ("guilty", "sad"),
    "sullen": ("sulking", "pouting"),
    "tearful": ("crying", "sad"),
    "teary": ("crying", "sad"),
    "unhappy": ("sad", "disappointed"),
    "weeping": ("sobbing", "crying"),
    # -- despair / defeat --------------------------------------------------
    "broken": ("devastated", "soulless"),
    "burnedout": ("exhausted", "soulless"),
    "burnout": ("exhausted", "soulless"),
    "deadinside": ("soulless",),
    "drained": ("exhausted", "soulless"),
    "empty": ("soulless", "lonely"),
    "giveup": ("defeated", "despair"),
    "hopeless": ("despair", "defeated"),
    "numb": ("soulless",),
    "resigned": ("defeated", "shrugging"),
    # -- shame / embarrassment ---------------------------------------------
    "ashamed": ("embarrassed", "guilty"),
    "contrite": ("apologetic", "guilty"),
    "flustered": ("embarrassed", "nervous"),
    "humiliated": ("mortified", "embarrassed"),
    "oops": ("embarrassed", "awkward"),
    "remorseful": ("guilty", "apologetic"),
    "sheepish": ("embarrassed", "bashful"),
    "sorry": ("apologetic", "guilty"),
    # -- fear --------------------------------------------------------------
    "afraid": ("scared", "nervous"),
    "apprehensive": ("anxious", "nervous"),
    "dread": ("anxious", "horrified"),
    "fearful": ("scared", "anxious"),
    "frantic": ("panicked",),
    "freakingout": ("panicked",),
    "frightened": ("scared",),
    "jumpy": ("nervous",),
    "overloaded": ("overwhelmed",),
    "panicking": ("panicked",),
    "petrified": ("terrified", "scared"),
    "spooked": ("scared", "surprised"),
    "stressed": ("anxious", "overwhelmed"),
    "swamped": ("overwhelmed",),
    "tense": ("nervous", "anxious"),
    "timid": ("nervous", "bashful"),
    # -- body states -------------------------------------------------------
    "boiling": ("hot", "furious"),
    "buzzed": ("drunktipsy",),
    "chilly": ("cold",),
    "drowsy": ("sleepy",),
    "drunk": ("drunktipsy",),
    "fatigued": ("exhausted", "sleepy"),
    "freezing": ("cold",),
    "lightheaded": ("dizzy",),
    "listless": ("bored", "melancholy"),
    "peckish": ("hungry",),
    "roasting": ("hot",),
    "shivering": ("cold", "scared"),
    "starving": ("hungry",),
    "sweating": ("hot", "nervous"),
    "sweaty": ("hot", "nervous"),
    "tipsy": ("drunktipsy",),
    "tired": ("sleepy", "exhausted"),
    "uninterested": ("bored", "unimpressed"),
    "wasted": ("drunktipsy",),
    "weary": ("exhausted", "defeated"),
    "wiped": ("exhausted",),
    "woozy": ("dizzy", "drunktipsy"),
    "yawning": ("sleepy", "bored"),
    "zzz": ("sleepy",),
    # -- tropes ------------------------------------------------------------
    "angelic": ("innocent", "wholesome"),
    "kawaii": ("sparkle", "adoring"),
    "magical": ("sparkle",),
    "sparkly": ("sparkle",),
    "tsundere": ("tsunderehuff", "pouting", "annoyed"),
    "whome": ("innocent",),
}


# ---------------------------------------------------------------------------
# Stemming
# ---------------------------------------------------------------------------

def _stem_variants(norm: str) -> set[str]:
    """The word plus deterministic de-inflections (shrugging -> shrug...).

    Handles -ing/-ed/-es/-s/-ness, consonant doubling (shrugg -> shrug), and
    dropped-e restoration (schem -> scheme). Over-generation is fine: variants
    only ever *meet* variants of real pack names, so a nonsense stem matches
    nothing.
    """
    variants = {norm}
    if norm.endswith("ies") and len(norm) - 3 >= _MIN_STEM:
        variants.add(norm[:-3] + "y")
    for suffix in ("ing", "ed", "es", "s", "ness", "ment", "ion"):
        if not norm.endswith(suffix):
            continue
        base = norm[: -len(suffix)]
        if len(base) < _MIN_STEM:
            continue
        variants.add(base)
        if suffix in ("ing", "ed", "ment", "ion"):
            if len(base) >= _MIN_STEM + 1 and base[-1] == base[-2]:
                variants.add(base[:-1])  # shrugg -> shrug
            variants.add(base + "e")  # schem -> scheme, confus -> confuse
    return variants


def _stem_hit(norm: str, keys: Iterable[str]) -> Optional[str]:
    """Match tag against keys by stem overlap; three passes, strongest first.

    Pass order (tag-variant hits raw key, raw tag hits key-variant, then
    variant-to-variant) plus sorted key iteration keeps the answer stable
    regardless of dict order.
    """
    ordered = sorted(keys)
    tag_variants = _stem_variants(norm)
    for key in ordered:
        if key in tag_variants:
            return key
    for key in ordered:
        if norm in _stem_variants(key):
            return key
    for key in ordered:
        if tag_variants & _stem_variants(key):
            return key
    return None


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

def _hit(candidate: str, keys: set[str]) -> Optional[str]:
    """Resolve one candidate emotion against the index: exact, then stemmed."""
    norm = normalize(candidate)
    if norm in keys:
        return norm
    return _stem_hit(norm, keys)


def _match_word(
    norm: str, keys: set[str], pack_aliases: dict[str, tuple[str, ...]]
) -> Optional[str]:
    """Ladder steps 1-3 for a single normalized word."""
    for candidate in pack_aliases.get(norm, ()):
        found = _hit(candidate, keys)
        if found is not None:
            return found
    for candidate in LEXICON.get(norm, ()):
        found = _hit(candidate, keys)
        if found is not None:
            return found
    return _stem_hit(norm, keys)


def match(
    emotion: str,
    keys: Iterable[str],
    pack_aliases: Optional[dict[str, tuple[str, ...]]] = None,
) -> Optional[str]:
    """Map a free-form emotion tag to a normalized pack-index key, or None.

    `keys` are the pack's normalized emotion names (chibi's index keys);
    `pack_aliases` the parsed Aliases.txt. Pure and deterministic.
    """
    key_set = set(keys)
    aliases = pack_aliases or {}
    norm = normalize(emotion)
    if not norm:
        return None
    if norm in key_set:  # caller usually pre-checks; free to re-check
        return norm

    found = _match_word(norm, key_set, aliases)
    if found is not None:
        return found

    if len(norm) >= _FUZZY_MIN_LEN:
        close = difflib.get_close_matches(
            norm, sorted(key_set), n=1, cutoff=_FUZZY_CUTOFF
        )
        if close:
            return close[0]

    tokens = [t for t in _TOKEN_RE.split(emotion.strip()) if t]
    if len(tokens) > 1:
        for token in tokens:
            found = _match_word(normalize(token), key_set, aliases)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# Pack alias files
# ---------------------------------------------------------------------------

def parse_aliases(path: Path) -> dict[str, tuple[str, ...]]:
    """Parse an Aliases.txt: `alias -> Target[, Target...]` lines.

    `#` starts a comment; blank/malformed lines are skipped (a typo in a
    taste file must never break emote resolution). Keys are normalized;
    targets keep their written form (they re-normalize inside _hit).
    """
    aliases: dict[str, tuple[str, ...]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return aliases
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "->" not in line:
            continue
        raw_alias, raw_targets = line.split("->", 1)
        alias = normalize(raw_alias)
        targets = tuple(t.strip() for t in raw_targets.split(",") if t.strip())
        if alias and targets:
            aliases[alias] = targets
    return aliases
