"""
Checks that every hard fact in an answer appears VERBATIM in what the model
was shown.

pakpatat/graph.py's node_verify validates citation TAGS: it catches a model citing
[S7] when only six sources exist. It says nothing about whether [S1] actually
contains the phone number attached to it -- and that is the failure that costs
something here. A local 7B model reproducing a hotline with two digits swapped,
correctly cited and confidently worded, reads as authoritative to someone about
to dial it.

So this module ignores the prose entirely and checks only the values that hurt
when wrong: phone numbers, emails, fees and dates. Each is normalised -- spacing
and punctuation differ harmlessly between a source page and an answer -- and
then required to appear somewhere in the closed input set: the formatted source
blocks PLUS the question, because a number the person typed themselves is not
an invention.

Deliberately conservative. It only inspects values it can confidently identify
("RM50", "03-2118 6200"); an unrecognised form is left alone rather than
flagged. False alarms are expensive in their own way -- staff who see a red
banner on every answer stop reading it.

Deterministic, offline, and free: no second model call.
"""
import re

# Burmese numerals. A Burmese answer may render a hotline in Burmese digits,
# which is a faithful reproduction, not an invention -- so normalise before
# comparing rather than flagging it.
_BURMESE_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
MONEY_RE = re.compile(r"(?:RM|MYR)\s?\d[\d,]*(?:\.\d{1,2})?", re.I)
# Dates, INCLUDING the way a language model actually writes them.
#
# This used to be ISO and slash forms only. That left the widest hole in the
# whole fact-verification layer, because a model writes "August 28, 2026", not
# "2026-08-28". Measured: asked about the REMEDI child promotion -- whose
# closing date the archive explicitly does NOT state -- the model answered
# "currently open until August 28, 2026". That date appears zero times in the
# corpus. It was fabricated, it is the single most consequential kind of fact
# here (a family believing they have until the 28th, or that they have missed
# it), and it passed this layer without a flag because of the spelling.
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"                                    # 2026-08-28
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"                         # 28/8/2026
    rf"|\b\d{{1,2}}\s+(?:{_MONTH_ALT})\.?,?\s+\d{{4}}\b"    # 28 August 2026
    rf"|\b(?:{_MONTH_ALT})\.?\s+\d{{1,2}},?\s+\d{{4}}\b"    # August 28, 2026
    rf"|\b(?:{_MONTH_ALT})\.?\s+\d{{4}}\b",                 # August 2026
    re.I,
)

_D_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_D_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
_D_DMY = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\.?,?\s+(\d{{4}})\b", re.I)
_D_MDY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)
_D_MY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{4}})\b", re.I)


def _date_key(value: str) -> str | None:
    """Canonical 'YYYY-MM-DD' (or 'YYYY-MM') for any spelling, so that
    '7 August 2026', 'August 07, 2026' and '2026-08-07' all compare equal.

    Without this, extending the pattern would have produced the opposite bug:
    a date the sources DO contain, spelled differently in the answer, flagged
    as invented. Warnings people learn to ignore protect nobody.
    """
    v = value.strip()
    if (m := _D_ISO.fullmatch(v)):
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    if (m := _D_SLASH.fullmatch(v)):                  # day/month/year (MY use)
        y = int(m[3]); y += 2000 if y < 100 else 0
        return f"{y}-{int(m[2]):02d}-{int(m[1]):02d}"
    if (m := _D_DMY.fullmatch(v)):
        return f"{m[3]}-{_MONTHS[m[2].lower().rstrip('.')]:02d}-{int(m[1]):02d}"
    if (m := _D_MDY.fullmatch(v)):
        return f"{m[3]}-{_MONTHS[m[1].lower().rstrip('.')]:02d}-{int(m[2]):02d}"
    if (m := _D_MY.fullmatch(v)):
        return f"{m[2]}-{_MONTHS[m[1].lower().rstrip('.')]:02d}"
    return None


def _haystack_dates(haystack: str) -> set[str]:
    keys = set()
    for m in DATE_RE.finditer(haystack):
        if (k := _date_key(m.group(0))):
            keys.add(k)
            if len(k) == 10:
                keys.add(k[:7])        # a full date also evidences its month
    return keys

# A run of digits with the separators phone numbers actually use. Note this
# matches plenty of non-phones ("level 2", "3 documents"); the digit-count
# filter below -- not the pattern -- is what identifies a phone number.
PHONE_RE = re.compile(r"\+?\d[\d (). -]{5,}\d")

# Malaysian numbers run 9-11 digits (03-2118 6200, 019-123 4567, 1-800-88-5776).
# Below 7 it is a year, a floor or a count; above 15 it is not a phone number.
_MIN_PHONE_DIGITS = 7
_MAX_PHONE_DIGITS = 15


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text.translate(_BURMESE_DIGITS))


def _loose(text: str) -> str:
    """Lowercase, with spaces and thousands separators removed, so that
    'RM 1,000' and 'RM1000' compare equal."""
    return re.sub(r"[\s, ]", "", text.translate(_BURMESE_DIGITS)).lower()


def _phone_variants(value: str) -> list[str]:
    """Malaysian numbers appear both internationally and locally: +60 3-2118
    6200 and 03-2118 6200 are the same number. Accept either form so a source
    written one way verifies an answer written the other."""
    d = _digits(value)
    out = [d]
    if d.startswith("60"):
        out.append("0" + d[2:])
    elif d.startswith("0"):
        out.append("60" + d[1:])
    return out


def _money_variants(value: str) -> list[str]:
    """'RM50', 'RM 50.00' and 'MYR 50' are one fee written three ways."""
    amount = _loose(re.sub(r"(?i)^(?:rm|myr)", "", value))
    amount = re.sub(r"\.0+$", "", amount)          # RM50.00 -> RM50
    return [f"rm{amount}", f"myr{amount}"]


def check(answer: str, haystack: str) -> list[dict]:
    """Return the hard facts in `answer` that do not appear in `haystack`.

    `haystack` must be everything the model was allowed to see -- the rendered
    source blocks and the question. Each result is
    {"kind": "phone"|"email"|"amount"|"date", "value": "<as written>"}.
    """
    hay_loose = _loose(haystack)
    hay_digits = _digits(haystack)
    hay_dates = _haystack_dates(haystack)

    found: list[tuple[str, str]] = []
    remaining = answer

    # Extract in order of specificity, blanking each match as it is taken, so
    # the loose phone pattern cannot re-match the digits inside an email
    # address, a price or an ISO date and report the same value twice.
    for kind, pattern in (("email", EMAIL_RE), ("amount", MONEY_RE),
                          ("date", DATE_RE), ("phone", PHONE_RE)):
        for m in pattern.finditer(remaining):
            found.append((kind, m.group(0).strip()))
        remaining = pattern.sub(" ", remaining)

    unverified: list[dict] = []
    seen: set[str] = set()

    for kind, value in found:
        if kind == "phone":
            n = len(_digits(value))
            if not _MIN_PHONE_DIGITS <= n <= _MAX_PHONE_DIGITS:
                continue                                   # not a phone number
            key = _digits(value)
            ok = any(v in hay_digits for v in _phone_variants(value))
        elif kind == "amount":
            key = _loose(value)
            ok = any(v in hay_loose for v in _money_variants(value))
        elif kind == "date":
            # Compare canonical forms, not spellings -- see _date_key.
            canon = _date_key(value)
            if canon is None:
                key, ok = _loose(value), _loose(value) in hay_loose
            else:
                key, ok = canon, canon in hay_dates
        else:                                              # email
            key = _loose(value)
            ok = key in hay_loose

        if ok or key in seen:
            continue
        seen.add(key)
        unverified.append({"kind": kind, "value": value})

    return unverified


LABELS = {
    "phone": "phone number",
    "email": "email address",
    "amount": "amount",
    "date": "date",
}


def describe(unverified: list[dict]) -> str:
    """One plain-language sentence naming the exact values in doubt, for
    clients that can only render text (the MCP tool)."""
    items = ", ".join(f"{LABELS.get(u['kind'], 'value')} {u['value']}"
                      for u in unverified)
    return (
        f"DO NOT RELY ON THIS WITHOUT CHECKING: the following does not appear "
        f"anywhere in the archive text this answer was built from, so the AI "
        f"may have altered or invented it -- {items}. Open the linked sources "
        f"and read the value there before using or sharing it."
    )
