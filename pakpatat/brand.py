# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
The brand, in one place.

Every user-visible name, tagline, colour and disclaimer lives here so that the
window title, the splash, the UI, both launchers and the docs cannot drift
apart -- which they had: the app shipped a window titled "Shümthein", a rail
reading "Shümthein", and a favicon that was a blue square reading "RM".

"RM" was Refugee Malaysia, UNHCR's retired site brand, in UNHCR's own blue
(#0072BC). TERMS.md forbids that name and NOTICE.md forbids implying that
affiliation, so the app was violating both of its own rules in the two places
a user looks first. See BRAND.md.

DISPLAY NAME AND IDENTIFIERS NOW AGREE
--------------------------------------
The display name is Päkpätät and the ASCII form is `pakpatat` -- the Python
package, module paths, `PAKPATAT_*` environment variables, the launchd label
and the repository directory all use it. That agreement is the whole point of
the name: BRAND.md §2 rejected `Shümthein` partly because its ASCII form
`shumthein` was a four-way collision in the dictionary, so brand and identifier
could never be the same string without a footnote. `pakpatat` collides with
nothing, so there is no footnote to keep in sync.

The rename was done in one pass after pipeline/refresh.py landed (BRAND.md §5b);
it is deliberately a clean break, with no `SHUMTHEIN_*` fallback left in the
env-var lookups. An old .env therefore does not half-work -- it fails at the
one place that reports it, pakpatat/preflight.py.
"""

# --------------------------------------------------------------------- name
NAME = "Päkpätät"
NAME_ASCII = "Pakpatat"          # for consoles that cannot render a diaeresis
SLUG = "pakpatat"                # package, module, env-var and repo identifier
HANDLE = "@pakpatat"             # byline on shared images -- see postcard.py

# K'Cho for "owl". NOT in K'Cho-English Dictionary v1.0 -- the corpora there are
# Bible-derived and contain no bird vocabulary. The evidence is a T0 author
# verdict by a native speaker, which is the PRIMARY tier in this project's own
# methodology, not a weaker substitute for a corpus count. BRAND.md §2 records
# the full status and the open dialect question.
GLOSS = "owl"
GLOSS_LONG = "K'Cho for owl — the one who is asked, and who answers"

# The owl means wisdom: rich in knowledge, and calm about it. The second half of
# that is knowing where the knowledge stops, which is exactly this tool's
# headline behaviour -- so the tagline carries the refusal, not the cleverness.
TAGLINE = "It answers what it knows. It says so when it doesn't."
TAGLINE_SHORT = "Answers what it knows."

DESCRIPTION = (
    "Offline assistant that answers refugee-support questions from UNHCR's "
    "published guidance, with every answer cited and every phone number "
    "checked. Not affiliated with UNHCR."
)

# ------------------------------------------------------------- attribution
#
# "UNHCR-friendly" means crediting them clearly and never borrowing their
# identity. Attribution is required; affiliation is prohibited. These two
# strings must travel together -- the first without the second is the exact
# claim NOTICE.md retracts.
SOURCE_CREDIT = "Guidance published by UNHCR and its partners."
NOT_AFFILIATED = (
    "This is an independent tool. It is not affiliated with, endorsed by, or "
    "operated by UNHCR."
)
TAKEDOWN = (
    "Content concerns: contact the operating organisation, which will remove "
    "material on request."
)

# ------------------------------------------------------------------ palette
#
# Indigo primary, gold secondary. Two constraints had to be satisfied at once.
#
# 1. NOT UNHCR's blue. NOTICE.md forbids using their brand colour as if this
#    were official, and this UI used to set --accent to #0072BC with the
#    comment "UNHCR blue". Measured hue distance from the references:
#
#        candidate        hue    from UNHCR #0072BC   from UN #009EDB
#        indigo #4F46E5   243°           40°                47°
#        azure  #2563EB   221°           18°                24°
#        cyan   #06B6D4   189°           15°                 8°
#
#    Cyan and azure are close enough to read as the official palette at a
#    glance, which is the whole failure. Indigo is violet-leaning and reads as
#    its own thing while staying in the blue family the sector lives in.
#
# 2. Legible. #4F46E5 is 6.29:1 on white and #818CF8 is 6.34:1 on the dark
#    background -- both clear of the 4.5:1 floor for body text.
#
# The gold is the owl's eyes and appears NOWHERE in UI chrome. That separation
# is deliberate: --warn-* is also amber, and a brand colour that turned up in
# the same visual register as "this came from a retired page, confirm it" would
# blunt the warning. Brand marks and state colours are different vocabularies.
COLORS = {
    "light": {
        "accent":      "#4F46E5",   # indigo
        "accent_ink":  "#4338CA",
        "accent_wash": "#EEF0FE",
        "gold":        "#FBBF24",   # mark only -- never UI chrome
        "ink":         "#14161A",
        "paper":       "#F4F5F7",
    },
    "dark": {
        "accent":      "#818CF8",
        "accent_ink":  "#A5B4FC",
        "accent_wash": "#1E1B4B",
        "gold":        "#FCD34D",
        "ink":         "#ECEEF1",
        "paper":       "#0F1114",
    },
}

# ------------------------------------------------------------------- the mark
#
# One owl, frontal and symmetrical, drawn as a SINGLE closed path with
# fill-rule="evenodd". Nesting does all the work: the body fills, each eye ring
# is a hole, each pupil sits inside a hole and so fills again, and the beak is a
# hole. One path, one colour, no second element to fall out of registration --
# which is what lets it survive a photocopy and a 16px favicon alike.
#
# HOW THIS SHAPE WAS ARRIVED AT (three rejected drafts, so they are not redrawn)
#   1. Eyes drawn as a literal bracket pair `[ ]`. Conceptually the best idea
#      here -- every sentence this app writes is held inside brackets pointing
#      at a source -- but rendered, it read as punctuation floating on a blob.
#   2. Same, with the brackets enlarged. Read as two empty rectangles.
#   3. Same, with pupils added inside. Read as spectacles: frame and pupil at
#      equal weight, competing.
# An owl is legible from big round eyes and almost nothing else, so the icon now
# leads with those. The bracket survives in the LOCKUP, where `[ owl ]` has the
# room to be read as a frame rather than as noise.
#
# The brow dip is 2.5 units. At 10 the silhouette read as a heart; the depth
# that says "ear tufts" and the depth that says "heart" are closer together
# than they look.
MARK_PATH = (
    # body: shallow brow dip, two soft ear tufts, ovoid taper to a rounded base
    "M32 7C29 5 25 4 21 4.5C12 5.5 6 13 6 25C6 34 8 42 13 49"
    "C18 56 25 60 32 60C39 60 46 56 51 49C56 42 58 34 58 25"
    "C58 13 52 5.5 43 4.5C39 4 35 5 32 7Z"
    # eye rings (holes)
    "M21 18A9 9 0 1 0 21 36A9 9 0 1 0 21 18Z"
    "M43 18A9 9 0 1 0 43 36A9 9 0 1 0 43 18Z"
    # pupils (nested inside the holes, so they fill)
    "M21 23A4 4 0 1 0 21 31A4 4 0 1 0 21 23Z"
    "M43 23A4 4 0 1 0 43 31A4 4 0 1 0 43 23Z"
    # beak
    "M32 45L28.5 36H35.5Z"
)

# The lockup's frame: the citation bracket the icon could not carry. Drawn as
# two separate paths so they can be spaced around whatever they enclose.
BRACKET_LEFT = "M14 4H4V60H14V56H8V8H14Z"
BRACKET_RIGHT = "M0 4H10V60H0V56H6V8H0Z"


def mark_svg(size: int = 64, color: str = "currentColor") -> str:
    """One-colour mark. The canonical form: print, photocopy, stamp, fax."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        f'width="{size}" height="{size}" role="img" aria-label="{NAME}">'
        f'<path fill="{color}" fill-rule="evenodd" d="{MARK_PATH}"/></svg>'
    )


# Body and beak only -- the eyes are drawn separately in the duotone version so
# they can take the gold.
BODY_PATH = (
    "M32 7C29 5 25 4 21 4.5C12 5.5 6 13 6 25C6 34 8 42 13 49"
    "C18 56 25 60 32 60C39 60 46 56 51 49C56 42 58 34 58 25"
    "C58 13 52 5.5 43 4.5C39 4 35 5 32 7Z"
    "M32 45L28.5 36H35.5Z"
)
EYES = ((21, 27), (43, 27))


def mark_svg_duotone(size: int = 64, body: str | None = None,
                     eye: str | None = None) -> str:
    """Screen mark: indigo body, gold eyes.

    Two colours, so it is NOT the fallback -- anything that might be photocopied
    or printed in one ink uses mark_svg() instead. Screens are where the gold
    earns its place.
    """
    body = body or COLORS["light"]["accent"]
    eye = eye or COLORS["light"]["gold"]
    rings = "".join(
        f'<circle cx="{x}" cy="{y}" r="9" fill="{eye}"/>' for x, y in EYES)
    pupils = "".join(
        f'<circle cx="{x}" cy="{y}" r="4" fill="{body}"/>' for x, y in EYES)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        f'width="{size}" height="{size}" role="img" aria-label="{NAME}">'
        f'<path fill="{body}" fill-rule="evenodd" d="{BODY_PATH}"/>'
        f'{rings}{pupils}</svg>'
    )


def favicon_data_uri(duotone: bool = True) -> str:
    """The mark as a data: URI, for <link rel="icon">.

    Inlined rather than served as a file: app.py serves a fixed set of routes,
    and a favicon that 404s is a broken-image icon in the window chrome.
    """
    import urllib.parse
    svg = mark_svg_duotone(32) if duotone else mark_svg(32, COLORS["light"]["accent"])
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


# ------------------------------------------------------------------ console
#
# The launchers print this before anything slow happens, so a double-click
# shows something branded within a second rather than a bare cursor while pip
# resolves. ASCII-only: Windows cmd.exe defaults to a code page that turns a
# diaeresis into a question mark.
CONSOLE_OWL = r"""
        ,___,   ,___,
        [O,O]   [O,O]
        /)__)   /)__)
    ----"--"-----"--"----
"""


def console_banner(ascii_only: bool = False) -> str:
    name = NAME_ASCII if ascii_only else NAME
    return f"{CONSOLE_OWL}\n    {name} - {TAGLINE}\n"
