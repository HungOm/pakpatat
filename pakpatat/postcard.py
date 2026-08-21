# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Hung Om and Päkpätät contributors
"""
Render an answer as a shareable PNG.

A case worker asked something, got a cited answer, and wants to put it in a
community WhatsApp group. That is a good thing to support -- and the most
dangerous moment in this whole application, because an image leaves the app and
keeps travelling. It cannot be corrected, it does not expire, and whoever sees
it fourth-hand has no way back to the source.

So the picture is not "the answer". It is the answer PLUS everything that makes
the answer safe to act on:

  - the citations, kept inline exactly as written
  - every warning, including the retired-site and unverified-fact ones
  - the source list, with the document or page each claim came from
  - the date it was generated, because guidance here goes stale
  - a footer saying this is not UNHCR

Two rules that must not be softened later:

  1. NOTHING IS SILENTLY DROPPED. If the content does not fit, the font shrinks
     to a floor; below that the render is REFUSED and the caller is told to copy
     text instead. A truncated answer could cut a phone number in half, or lose
     the "this service is currently unavailable" line, and the reader would have
     no way to know something was missing.
  2. NO UNHCR BRANDING. The mark and colours are this project's own. See
     the README's notices -- an image is exactly where an implied affiliation does damage.

Rendered server-side with Pillow rather than in the browser: the desktop window
has no JS bridge to save a file, canvas text shaping for Burmese is unreliable
in WKWebView, and drawing this in plain Python keeps it testable.
"""
import pathlib
import re
import textwrap

from . import brand, config

# Fixed 1200 wide -- the width X, WhatsApp and Facebook all render text-images
# at without resampling, so glyph edges stay crisp.
#
# HEIGHT IS NOT FIXED. It used to be a flat 1500, and a three-paragraph answer
# produced a card that was half empty white space with the footer marooned at
# the bottom -- it read as a broken page rather than a post. The card now grows
# to its content between these bounds:
#
#   MIN_H  16:9. X's in-timeline crop. Going shorter gets letterboxed anyway,
#          so there is nothing to gain below it.
#   MAX_H  4:5. The tallest ratio X, Instagram and WhatsApp all show uncropped.
#          Past this the content becomes card 2.
#
# Every card in one thread is rendered at the SAME height (the tallest page's).
# Mixed heights in a 4-image X post tile ragged, and a thread that looks sloppy
# is a thread people trust less.
# Everything below is authored in 1200-wide logical units and multiplied by
# SCALE at render time, so the PNG ships at twice the nominal resolution.
#
# Text is the entire payload of this image and it is read on phone screens at
# 2x and 3x device-pixel ratios. At 1200 the glyphs were being upscaled by the
# viewer -- soft edges on exactly the digits of a hotline number that have to be
# unambiguous. Rendering the type at 2x costs file size and nothing else.
SCALE = 2

W = 1200
MIN_H, MAX_H = 675, 1500
PAD = 68

FONT_MIN, FONT_MAX = 19, 27          # body type; below FONT_MIN we paginate
LINE = 1.5                           # a touch airier: this is read on a phone

# A long answer becomes a numbered thread rather than an error.
#
# Refusing was the safe first move -- better than cropping a phone number in
# half -- but it is not a good answer to "this reply is four paragraphs". The
# rule that matters is that nothing is DROPPED, not that everything fits one
# square. So overflow now continues onto card 2, 3, 4 with "2/4" on each.
#
# The cap is a sanity bound, not a target: past this the reply is a document,
# not a post, and copying the text is the honest option.
MAX_CARDS = 8

AVATAR = 84             # X-style round avatar
HEADER_H = 176          # avatar + display name + @handle, then a hairline
FOOTER_H = 112          # date + not-affiliated + verify-by-phone

THEMES = {
    # Pure white, not the app's #f4f5f7 canvas. A shared image is not a screen
    # of the app; it lands in a feed next to other white cards, and an
    # off-white panel there reads as a screenshot of something slightly broken.
    "light": dict(bg="#ffffff", panel="#ffffff", ink="#0f1419", muted="#536471",
                  line="#e8ebed", accent="#4F46E5", accent_wash="#F3F2FE",
                  warn_bg="#FEF6E7", warn_ink="#8A5A00", warn_line="#F2C14E"),
    "dark":  dict(bg="#000000", panel="#000000", ink="#e7e9ea", muted="#71767b",
                  line="#2f3336", accent="#818CF8", accent_wash="#17151F",
                  warn_bg="#2A1F08", warn_ink="#F3C77B", warn_line="#6B5316"),
}

# Bold is a real face, not a synthesised one. _font() accepted a `bold` argument
# and ignored it, so every "bold" string on the card rendered regular -- which is
# why the display name never looked like a display name.
_LATIN_BOLD = [
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ("/System/Library/Fonts/Helvetica.ttc", 1),
    ("C:/Windows/Fonts/segoeuib.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
]

# Latin first, then a Myanmar-capable face. Burmese needs real shaping, so
# Pillow must be built with libraqm -- checked in render() rather than assumed,
# because without it Burmese renders as disconnected glyphs that look like text
# and are not.
_LATIN = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_MYANMAR = [
    "/System/Library/Fonts/Supplemental/NotoSansMyanmar.ttc",
    "/System/Library/Fonts/NotoSansMyanmar.ttc",
    "/System/Library/Fonts/Supplemental/Myanmar Sangam MN.ttc",
    "C:/Windows/Fonts/mmrtext.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
]

_BURMESE = re.compile(r"[\u1000-\u109F\uAA60-\uAA7F]")


class TooLong(RuntimeError):
    """The answer will not fit without dropping something. Refuse, don't crop."""


def _first_existing(paths):
    for p in paths:
        if pathlib.Path(p).exists():
            return p
    return None


def _font(size: int, burmese: bool, bold: bool = False):
    from PIL import ImageFont
    if bold and not burmese:
        # Bold is only used for Latin chrome (display name, headings). Burmese
        # falls through to regular rather than risk a face without Myanmar
        # coverage silently substituting boxes for text.
        for path, index in _LATIN_BOLD:
            if pathlib.Path(path).exists():
                try:
                    return ImageFont.truetype(path, size, index=index)
                except OSError:
                    continue
    path = _first_existing(_MYANMAR if burmese else _LATIN) or \
        _first_existing(_LATIN) or _first_existing(_MYANMAR)
    if not path:
        raise RuntimeError("No usable font found for rendering an image.")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    """Wrap to pixel width, measuring rather than guessing character counts."""
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        words, line = para.split(), ""
        for w in words:
            # A token containing no spaces -- a source URL -- cannot wrap by
            # word, so the first version drew it straight off the right edge of
            # the card and the tail was invisibly lost. Break by character
            # instead: an ugly wrap is recoverable, a URL missing its last
            # thirty characters is not, and this card exists so a reader can
            # get back to the source.
            while draw.textlength(w, font=font) > width:
                cut = len(w)
                while cut > 1 and draw.textlength(w[:cut], font=font) > width:
                    cut -= 1
                if line:
                    out.append(line)
                    line = ""
                out.append(w[:cut])
                w = w[cut:]
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=font) <= width or not line:
                line = trial
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def _plain(md: str) -> str:
    """Markdown -> readable plain text for drawing.

    The image draws glyphs, not HTML, so anything left as markup is shown
    literally: the first render of this card had "### Coverage:" and
    "**RM 150.00**" printed with the hashes and asterisks visible. Structure is
    preserved (headings become their own line, list items keep a bullet) --
    only the syntax characters go.
    """
    out = []
    for line in md.split("\n"):
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)          # heading marks
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)         # bold
        line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", line)   # italic
        line = re.sub(r"`([^`]+)`", r"\1", line)               # inline code
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", line)
        line = re.sub(r"^\s*[-*+]\s+", "- ", line)             # normalise bullets
        line = re.sub(r"^\s*\|", "", line)                     # table pipes
        line = re.sub(r"\s*\|\s*", "  ", line)
        if re.fullmatch(r"[\s\-:]{3,}", line):                 # table rule row
            continue
        out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _blocks(msg: dict, question: str, lang: str,
            warnings: bool = False) -> list[tuple[str, str]]:
    """(kind, text) in the order they must appear.

    WARNINGS ARE OFF BY DEFAULT -- AN EXPLICIT OPERATOR DECISION, NOT A BUG.
    -----------------------------------------------------------------------
    This module's own docstring says nothing is silently dropped, and the README
    treats the retired-source and unverified-fact notices as part of what makes
    a shared answer safe. The operator has asked for a clean card without them,
    which is theirs to decide; it is recorded here so nobody "fixes" it back by
    accident, and so the trade is visible to whoever reads this next.

    What that trade is: a card with an unverified fact on it now looks exactly
    like a card where every fact was checked. The footer still carries the date,
    the not-affiliated line and "check the source before acting", and the source
    list still lets a reader get back to the document -- so the route to
    verification survives; the prompt to take it does not.

    Pass warnings=True to restore them.
    """
    b = [("q", question.strip())]
    if warnings:
        for u in (msg.get("unverified") or []):
            b.append(("warn", f"! NOT IN THE ARCHIVE: {u['value']} - check this "
                              f"before sharing it."))
        for w in (msg.get("warnings") or []):
            b.append(("warn", f"! {w}"))
    b.append(("body", _plain(msg.get("answer") or "")))

    # Group by the document, not by tag. Eight retrieved chunks from one page
    # produced eight identical lines -- the same title and the same 200-character
    # minute citation repeated five times -- which crowded out the answer and
    # made the card look like filler. One line per distinct source, with the
    # tags that point at it, keeps every citation checkable and takes a fifth of
    # the room.
    seen: dict[tuple, list[str]] = {}
    for s in (msg.get("sources") or []):
        key = (s.get("title", ""), s.get("citation") or s.get("url", ""))
        seen.setdefault(key, []).append(s.get("tag", ""))
    if seen:
        # Label the card. Without a heading the final card opened straight onto
        # "[S1] [S2] ..." and read as a stray fragment rather than the place a
        # reader goes to check the claim.
        b.append(("srchead", "WHERE THIS CAME FROM"))
        lines = []
        for (title, origin), tags in seen.items():
            lines.append(f"{' '.join(f'[{t}]' for t in tags)} {title}")
            lines.append(f"     {origin}")
            lines.append("")
        b.append(("src", "\n".join(lines).rstrip()))
    return b


def _layout(d, blocks, size, burmese, inner):
    """Wrap every block once, into a flat list of drawable lines.

    Returns [(kind, text, font, line_height)]. Pagination happens afterwards on
    this flat list, so a block can straddle a card boundary instead of forcing
    a page break that would leave half a card empty.
    """
    f_body = _font(size, burmese)
    f_q = _font(size + 7, burmese, bold=True)          # the question leads
    f_srchead = _font(max(size - 5, 13), burmese, bold=True)
    f_small = _font(max(size - 4, 13), burmese)
    flat = []
    for kind, text in blocks:
        font = {"q": f_q, "srchead": f_srchead, "src": f_small,
                "warn": f_small}.get(kind, f_body)
        # Warnings and the question are inset blocks, so they wrap narrower.
        width = inner - (56 if kind in ("warn", "q") else 0)
        for ln in _wrap(d, text, font, width):
            flat.append((kind, ln, font, int(font.size * LINE)))
        gap = {"q": 1.0, "warn": 0.85}.get(kind, 0.7)
        flat.append((kind + "-gap", "", font, int(font.size * gap)))
    return flat


def _paginate(flat, avail):
    """Split the flat line list into cards of `avail` pixels.

    A card never breaks between a warning's lines: a half-shown warning is
    worse than a slightly short card, because the reader sees a fragment of a
    caution and no indication the rest exists.
    """
    pages, cur, used = [], [], 0
    i = 0
    while i < len(flat):
        kind, txt, font, lh = flat[i]
        run = [flat[i]]
        if kind == "warn":                      # keep a warning block together
            j = i + 1
            while j < len(flat) and flat[j][0] in ("warn", "warn-gap"):
                run.append(flat[j]); j += 1
        run_h = sum(r[3] for r in run)
        if used + run_h > avail and cur:
            pages.append(cur); cur, used = [], 0
        cur.extend(run); used += run_h
        i += len(run)
    if cur:
        pages.append(cur)
    return pages


def render(msg: dict, question: str, theme: str = "light", lang: str = "en",
           out_path: pathlib.Path | None = None,
           warnings: bool = False) -> list[pathlib.Path]:
    """Render an answer as one card, or a numbered thread if it is long.

    The answer is never trimmed, shortened or simplified to make it fit: it
    wraps, it flows onto the next card, and if it genuinely will not fit in
    MAX_CARDS the render is refused so the caller can offer text instead.

    `warnings=False` is the default by operator decision -- see _blocks().

    Returns the list of PNG paths in reading order.
    """
    import datetime as dt
    from PIL import Image, ImageDraw, features

    S = SCALE       # every geometry literal below is logical units x S

    burmese = bool(_BURMESE.search((msg.get("answer") or "") + question))
    if burmese and not features.check("raqm"):
        raise RuntimeError(
            "Burmese text cannot be rendered as an image on this computer: "
            "Pillow is built without complex-text shaping (libraqm). Copy the "
            "answer as text instead.")

    t = THEMES.get(theme, THEMES["light"])
    blocks = _blocks(msg, question, lang, warnings=warnings)
    # The source list is a card of its own at the end of the thread. Repeating a
    # 200-character minute citation on every card crowds out the answer, but
    # dropping it altogether would send an unsourced claim into a group chat --
    # and cards get separated when they are forwarded. One sources card at the
    # end is how a reader gets back to the document.
    body_blocks = [b for b in blocks if b[0] not in ("src", "srchead")]
    src_blocks = [b for b in blocks if b[0] in ("srchead", "src")]

    inner = (W - 2 * PAD) * S
    avail = (MAX_H - HEADER_H - FOOTER_H - PAD) * S

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    for size in range(FONT_MAX * S, (FONT_MIN * S) - 1, -S):
        pages = _paginate(_layout(probe, body_blocks, size, burmese, inner), avail)
        if len(pages) <= MAX_CARDS - (1 if src_blocks else 0):
            break
    else:
        raise TooLong("even at the smallest readable size this needs more than "
                      f"{MAX_CARDS} cards")

    if src_blocks:
        pages += _paginate(_layout(probe, src_blocks, size, burmese, inner), avail)
    total = len(pages)

    # EACH card is sized to its own content, floored at MIN_H.
    #
    # A previous version gave every card in a thread the tallest page's height so
    # a multi-image X post would tile evenly. Rendered, that was the wrong trade:
    # a long answer followed by a three-line source list produced a second card
    # that was two-thirds empty white. This module's own docstring names the
    # primary destination as a community WhatsApp group, where images are opened
    # one at a time and a mostly-blank card just looks broken. Even tiling only
    # pays off in an X grid, which crops to a common height anyway.

    # Default to the Downloads folder -- see config.DOWNLOADS_DIR for why the
    # old location inside data/ was the wrong answer.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if out_path:
        base = pathlib.Path(out_path).expanduser()
        if base.is_dir():
            base = base / f"pakpatat-{stamp}.png"
    else:
        base = config.DOWNLOADS_DIR / f"pakpatat-{stamp}.png"
    base.parent.mkdir(parents=True, exist_ok=True)

    f_brand = _font(29 * S, False, bold=True)
    f_handle = _font(23 * S, False)
    f_num = _font(18 * S, False, bold=True)
    f_foot = _font(15 * S, False)
    today = dt.date.today().isoformat()
    out_paths = []

    for n, page in enumerate(pages, 1):
        content_h = sum(lh for *_, lh in page)
        H = max(MIN_H * S,
                min(MAX_H * S, (HEADER_H + FOOTER_H + PAD) * S + content_h))
        img = Image.new("RGB", (W * S, H), t["bg"])
        d = ImageDraw.Draw(img)

        # ---- header: X-style byline -- round avatar, name, @handle ----
        #
        # Was a thin accent rule with a small mark and the name beside it, which
        # read as a document letterhead. In a feed the thing that identifies a
        # post is an avatar with a name and a handle under it, so that is what
        # this draws.
        ax, ay = PAD * S, PAD * S
        d.ellipse([ax, ay, ax + AVATAR * S, ay + AVATAR * S], fill=t["accent"])
        # The owl, scaled into the avatar: two gold eyes and a beak. Drawn with
        # primitives rather than the SVG path -- Pillow has no path renderer,
        # and at 84px the silhouette is carried by the eyes anyway.
        er = AVATAR * S * 0.155
        for ex in (ax + AVATAR * S * 0.33, ax + AVATAR * S * 0.67):
            ey = ay + AVATAR * S * 0.44
            d.ellipse([ex - er, ey - er, ex + er, ey + er], fill="#FBBF24")
            pr = er * 0.44
            d.ellipse([ex - pr, ey - pr, ex + pr, ey + pr], fill=t["accent"])
        bx, by = ax + AVATAR * S / 2, ay + AVATAR * S * 0.63
        d.polygon([(bx, by + AVATAR * S * 0.13), (bx - AVATAR * S * 0.05, by),
                   (bx + AVATAR * S * 0.05, by)], fill="#FBBF24")

        tx = ax + AVATAR * S + 22 * S
        d.text((tx, ay + 12 * S), "Päkpätät", font=f_brand, fill=t["ink"])
        d.text((tx, ay + 49 * S), brand.HANDLE, font=f_handle, fill=t["muted"])

        if total > 1:
            # A reader who sees card 3 alone must be able to tell there are more.
            label = f"{n}/{total}"
            tw = d.textlength(label, font=f_num)
            x0 = (W - PAD) * S - tw - 30 * S
            d.rounded_rectangle([x0, ay + 20 * S, (W - PAD) * S, ay + 58 * S], radius=19 * S,
                                fill=t["accent"])
            d.text((x0 + 15 * S, ay + 27 * S), label, font=f_num, fill="#ffffff")
        d.line([PAD * S, (HEADER_H - 30) * S, (W - PAD) * S, (HEADER_H - 30) * S],
               fill=t["line"], width=1)

        # The question and the warnings are the two things a reader must not
        # skim past, so both are drawn as inset blocks with a coloured rule down
        # the left edge -- the question in brand indigo, warnings in amber.
        # Previously the question was plain black body text and the warning was
        # a full-bleed band that ran off both margins.
        y = HEADER_H * S
        runs = []                       # (kind, y0, y1) for the block backgrounds
        # Group the inset kinds into panels. Two rules, both learned from the
        # render:
        #   - a panel must NOT swallow its own trailing gap, or the question
        #     panel and the warning panel below it meet with no seam and read
        #     as one two-tone box;
        #   - but consecutive blocks of the SAME kind must merge ACROSS that
        #     gap, or two warnings draw as two panels with the left rule broken
        #     between them, which looks like a rendering fault.
        # Tracking the last *text* kind (gaps don't count) does both.
        prev_text = None
        for kind, txt, font, lh in page:
            if kind in ("q", "warn"):
                if runs and prev_text == kind:
                    runs[-1] = (kind, runs[-1][1], y + lh)
                else:
                    runs.append((kind, y, y + lh))
                prev_text = kind
            elif not kind.endswith("-gap"):
                prev_text = kind          # a different kind ends the panel
            y += lh

        # NB: not `base` -- that name holds the output path further down, and
        # shadowing it here made the filename build from the string "warn".
        for bkind, y0, y1 in runs:
            fill = t["accent_wash"] if bkind == "q" else t["warn_bg"]
            rule = t["accent"] if bkind == "q" else t["warn_line"]
            d.rounded_rectangle([PAD * S, y0 - 14 * S, (W - PAD) * S, y1 + 6 * S], radius=14 * S,
                                fill=fill)
            d.rounded_rectangle([PAD * S, y0 - 14 * S, (PAD + 6) * S, y1 + 6 * S], radius=3 * S,
                                fill=rule)

        y = HEADER_H * S
        for kind, txt, font, lh in page:
            if kind.endswith("-gap"):
                y += lh
                continue
            colour = {"q": t["accent"], "warn": t["warn_ink"],
                      "src": t["muted"], "srchead": t["accent"],
                      "body": t["ink"]}.get(kind, t["ink"])
            x = (PAD + (28 if kind in ("q", "warn") else 0)) * S
            d.text((x, y), txt, font=font, fill=colour)
            y += lh

        # ---- footer: on EVERY card, because cards travel separately ----
        d.line([PAD * S, H - FOOTER_H * S, (W - PAD) * S, H - FOOTER_H * S],
               fill=t["line"], width=S)
        d.text((PAD * S, H - (FOOTER_H - 20) * S),
               f"Generated {today} from an offline archive of UNHCR Malaysia "
               f"guidance.", font=f_foot, fill=t["muted"])
        d.text((PAD * S, H - (FOOTER_H - 44) * S),
               "Independent community tool - NOT affiliated with UNHCR. "
               "Guidance changes: check the source before acting.",
               font=f_foot, fill=t["muted"])
        d.text((PAD * S, H - (FOOTER_H - 68) * S),
               "Verify anything urgent by phone.", font=f_foot, fill=t["muted"])

        p = base if total == 1 else base.with_name(f"{base.stem}-{n}of{total}.png")
        img.save(p, "PNG", optimize=True, dpi=(72 * S, 72 * S))
        out_paths.append(p)

    return out_paths
