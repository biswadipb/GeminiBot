"""Pictures of finished games for the result post: the final position, the players and who won."""

import io
import logging
import math
import os
import unicodedata
import urllib.request

import arabic_reshaper
from bidi.algorithm import get_display
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont, features

from boardgames import (GAMES, GO_SIZE, GO_STAR_POINTS, LADDERS, LUDO_HOME_COLUMNS, LUDO_OFFSET, LUDO_SAFE,
                        LUDO_TRACK, SNAKES, checkers_coords)

SCALE = 2  # draw at twice the size and shrink at the end, for smooth edges
WIDTH = 720
PAD = 24
PIECE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "pieces")
INK, HINT, PAPER = (29, 29, 31), (120, 120, 126), (250, 248, 243)
LIGHT, DARK, LAST = (238, 218, 182), (180, 135, 100), (205, 210, 106)
BLACK_DISC, WHITE_DISC = ((30, 30, 30), (70, 70, 70)), ((246, 240, 228), (185, 170, 140))
GOLD = (232, 185, 35)
LUDO = {"red": (229, 57, 53), "green": (67, 160, 71), "yellow": (251, 192, 45), "blue": (30, 136, 229)}
PARTY = [(229, 57, 53), (67, 160, 71), (251, 192, 45), (30, 136, 229), (142, 36, 170), (251, 140, 0)]
_pieces = {}


# Text in the pictures can contain anything people put in their names, so each character is drawn with the
# first of these fonts that has it: Noto for most writing, symbols, maths, currencies and emoji, then GNU Unifont,
# which has a glyph for virtually every Unicode character. They're downloaded once at startup (fonts/ folder).
log = logging.getLogger("gemini-bot")
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
NOTO = "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/"
UNIFONT = "https://ftp.gnu.org/gnu/unifont/unifont-18.0.01/"
NOTO_EXTRA = "https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/"
PLANGOTHIC = "https://github.com/Fitzgerald-Porthmouth-Koenigsegg/Plangothic_Project/releases/download/V2.9.5795/"
FONT_SOURCES = [
    ("NotoSans.ttf", NOTO + "notosans/NotoSans%5Bwdth,wght%5D.ttf"),
    ("NotoSansSymbols.ttf", NOTO + "notosanssymbols/NotoSansSymbols%5Bwght%5D.ttf"),
    ("NotoSansSymbols2.ttf", NOTO + "notosanssymbols2/NotoSansSymbols2-Regular.ttf"),
    ("NotoSansMath.ttf", NOTO + "notosansmath/NotoSansMath-Regular.ttf"),
    ("NotoSansBengali.ttf", NOTO + "notosansbengali/NotoSansBengali%5Bwdth,wght%5D.ttf"),
    ("NotoSansDevanagari.ttf", NOTO + "notosansdevanagari/NotoSansDevanagari%5Bwdth,wght%5D.ttf"),
    ("NotoSansArabic.ttf", NOTO + "notosansarabic/NotoSansArabic%5Bwdth,wght%5D.ttf"),
    ("NotoEmoji.ttf", NOTO + "notoemoji/NotoEmoji%5Bwght%5D.ttf"),
    ("NotoSansEgyptianHieroglyphs.ttf", NOTO + "notosansegyptianhieroglyphs/NotoSansEgyptianHieroglyphs-Regular.ttf"),
    ("unifont.otf", UNIFONT + "unifont-18.0.01.otf"),  # nearly every character in the first 65,536
    ("unifont_upper.otf", UNIFONT + "unifont_upper-18.0.01.otf"),  # most of the rest
    # writing systems Unifont leaves out
    ("NotoSansCuneiform.ttf", NOTO + "notosanscuneiform/NotoSansCuneiform-Regular.ttf"),
    ("NotoSansAnatolianHieroglyphs.ttf", NOTO + "notosansanatolianhieroglyphs/NotoSansAnatolianHieroglyphs-Regular.ttf"),
    ("NotoSansBamum.ttf", NOTO + "notosansbamum/NotoSansBamum%5Bwght%5D.ttf"),
    ("NotoSerifTangut.ttf", NOTO_EXTRA + "NotoSerifTangut/hinted/ttf/NotoSerifTangut-Regular.ttf"),
    ("PlangothicP1.ttf", PLANGOTHIC + "PlangothicP1-Regular.ttf"),  # rare Chinese characters (CJK extensions B-I)
    ("PlangothicP2.ttf", PLANGOTHIC + "PlangothicP2-Regular.ttf"),
]
_stack = None  # [(font path, set of characters it has)], in FONT_SOURCES order
_fonts = {}


def download_fonts():
    """Fetch any missing fonts (about 60 MB in all). Runs in the background at startup; until it's done,
    pictures use whatever fonts are already there."""
    global _stack
    os.makedirs(FONT_DIR, exist_ok=True)
    for name, url in FONT_SOURCES:
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            continue
        try:
            urllib.request.urlretrieve(url, path + ".part")
            os.replace(path + ".part", path)
        except Exception as e:
            log.warning("Could not download font %s: %s", name, e)
    _stack = None  # re-read the fonts on next use


def font_stack():
    global _stack
    if _stack is None:
        stack = []
        for name, _ in FONT_SOURCES:
            path = os.path.join(FONT_DIR, name)
            if os.path.exists(path):
                try:
                    with TTFont(path, lazy=True) as f:
                        stack.append((path, set(f.getBestCmap())))
                except Exception:
                    log.exception("Unreadable font %s", name)
        _stack = stack
    return _stack


def font(size, path=None):
    key = (size, path)
    if key not in _fonts:
        if path:
            engine = ImageFont.Layout.RAQM if SHAPING else ImageFont.Layout.BASIC
            _fonts[key] = ImageFont.truetype(path, size * SCALE, layout_engine=engine)
        else:
            _fonts[key] = ImageFont.load_default(size=size * SCALE)
    return _fonts[key]


SHAPING = features.check("raqm")  # Pillow can lay out Arabic, Bengali etc. itself when it has libraqm


def visual_order(words):
    """Without libraqm, put right-to-left text (Arabic, Hebrew) in display order and join Arabic letters."""
    if SHAPING or not any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in words):
        return words
    return get_display(arabic_reshaper.reshape(words))


def runs(words):
    """Split text into pieces that can each be drawn with one font: [(font path or None, text)]."""
    stack, pieces = font_stack(), []
    words = visual_order(words)
    for ch in words:
        if pieces and unicodedata.category(ch).startswith("M"):  # accents stay with their letter
            pieces[-1][1] += ch
            continue
        path = next((p for p, has in stack if ord(ch) in has), None)
        if pieces and pieces[-1][0] == path:
            pieces[-1][1] += ch
        else:
            pieces.append([path, ch])
    return pieces


def text_width(words, size):
    return sum(font(size, path).getlength(piece) for path, piece in runs(words)) / SCALE


def clean(text):
    """Keep every visible character; drop control characters, zero-width joiners and emoji style selectors."""
    kept = "".join(ch for ch in str(text) if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn")
                   and not 0xFE00 <= ord(ch) <= 0xFE0F and not 0xE0100 <= ord(ch) <= 0xE01EF)  # style selectors
    return " ".join(kept.split()) or "Player"


def s(v):
    return v * SCALE


def disc(draw, cx, cy, r, colour, outline, width=2):
    draw.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=colour, outline=outline, width=int(s(width)))


# ---------------------------------------------------------------- shading helpers (the same looks as the Mini App)

def rgb(hex_colour):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def gradient(img, box, start, end, radius=0):
    """A diagonal gradient (top left to bottom right) in a rounded box."""
    x0, y0, x1, y1 = (int(s(v)) for v in box)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    layer = Image.new("RGBA", (w, h))
    paint = ImageDraw.Draw(layer)
    for k in range(0, w + h, 2):
        paint.line([(k, 0), (0, k)], fill=mix(start, end, k / (w + h)) + (255,), width=3)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=int(s(radius)), fill=255)
    img.paste(layer, (x0, y0), mask)


def sphere(draw, cx, cy, r, light, mid, dark, outline=None):
    """A shaded ball: dark at the edge, brightest towards the top left."""
    steps = 18
    for i in range(steps):
        t = i / (steps - 1)
        radius = r * (1 - .82 * t)
        ox, oy = -.34 * r * t, -.38 * r * t
        colour = mix(dark, mid, t * 2) if t < .5 else mix(mid, light, (t - .5) * 2)
        disc(draw, cx + ox, cy + oy, radius, colour, None)
    if outline:
        disc(draw, cx, cy, r, None, outline, 1)


def shadow(draw, cx, cy, rx, ry, alpha=70):
    draw.ellipse([s(cx - rx), s(cy - ry), s(cx + rx), s(cy + ry)], fill=(0, 0, 0, alpha))


def marble(draw, cx, cy, r, side, glossy=True):
    """Black or white piece ("b"/"w", upper case = king) as a shaded marble with a shadow and shine."""
    shadow(draw, cx + r * .12, cy + r * .85, r * .9, r * .3)
    if side.lower() == "b":
        sphere(draw, cx, cy, r, (138, 138, 138), (42, 42, 42), (5, 5, 5), (0, 0, 0))
    else:
        sphere(draw, cx, cy, r, (255, 255, 255), (241, 237, 228), (180, 172, 155), (150, 140, 120))
    if glossy:
        draw.ellipse([s(cx - r * .5), s(cy - r * .58), s(cx - r * .1), s(cy - r * .3)],
                     fill=(255, 255, 255, 90 if side.lower() == "b" else 200))
    if side.isupper():
        crown(draw, cx, cy, r * .55)


def ridged(draw, cx, cy, r, side):
    """A draughts man with ridged rings."""
    shadow(draw, cx + r * .1, cy + r * .9, r * .95, r * .3, 90)
    rings = [(30, 30, 30), (12, 12, 12), (44, 44, 44), (12, 12, 12), (52, 52, 52)] if side.lower() == "b" else \
        [(255, 248, 234), (205, 182, 140), (246, 236, 216), (212, 191, 152), (251, 243, 227)]
    edge = (5, 5, 5) if side.lower() == "b" else (168, 141, 98)
    for k, frac in enumerate((1, .56, .5, .34, .28)):
        disc(draw, cx, cy, r * frac, rings[k], edge if k == 0 else None, 1.5 if k == 0 else 0)
    if side.isupper():
        crown(draw, cx, cy, r * .5)


def stone(draw, cx, cy, r, side):
    marble(draw, cx, cy, r, side)


def crown(draw, cx, cy, size, colour=GOLD):
    pts = [(-1, .65), (-1.05, -.45), (-.5, .05), (0, -.75), (.5, .05), (1.05, -.45), (1, .65)]
    draw.polygon([(s(cx + x * size), s(cy + y * size)) for x, y in pts], fill=colour, outline=(122, 90, 0), width=s(1))


def star_points(cx, cy, r, inner=.45):
    return [(s(cx + math.cos(math.pi / 5 * k - math.pi / 2) * (r if k % 2 == 0 else r * inner)),
             s(cy + math.sin(math.pi / 5 * k - math.pi / 2) * (r if k % 2 == 0 else r * inner))) for k in range(10)]


def pawn(draw, x, y, r, colour, label=""):
    """A glossy playing piece: shadow, base, body, head, shine and the player's initial."""
    shadow(draw, x, y + r * .95, r * .95, r * .3, 80)
    edge = mix(colour, (0, 0, 0), .45)
    body = [(x - r * .78, y + r * .85)]
    body += [(x - r * .78 + r * 1.56 * t, y + r * .85 + math.sin(math.pi * t) * r * .14) for t in (.25, .5, .75, 1)]
    body += [(x + r * .42, y - r * .05), (x, y - r * .2), (x - r * .42, y - r * .05)]
    draw.polygon([(s(px), s(py)) for px, py in body], fill=colour, outline=edge, width=max(1, int(s(r * .07))))
    disc(draw, x, y - r * .48, r * .5, colour, edge, max(.5, r * .07))
    draw.ellipse([s(x - r * .33), s(y - r * .78), s(x - r * .02), s(y - r * .55)], fill=(255, 255, 255, 190))
    if label:
        f = font(max(6, int(r * .62)), font_stack()[0][0] if font_stack() else None)
        draw.text((s(x), s(y + r * .72)), label, font=f, fill=(255, 255, 255), anchor="ms",
                  stroke_width=max(1, int(s(r * .05))), stroke_fill=edge)


def piece_image(code, size):
    key = (code, size)
    if key not in _pieces:
        _pieces[key] = Image.open(os.path.join(PIECE_DIR, f"{code}.png")).convert("RGBA").resize((s(size), s(size)), Image.LANCZOS)
    return _pieces[key]


def text(draw, x, y, words, size, colour=INK):
    """Draw text whose top-left is at x, y, switching fonts as needed but sharing one baseline."""
    baseline = y + size * .95
    for path, piece in runs(words):
        f = font(size, path)
        draw.text((s(x), s(baseline)), piece, font=f, fill=colour, anchor="ls")
        x += f.getlength(piece) / SCALE


# ---------------------------------------------------------------- boards (each draws into a box: x, y, width)

def chess_board(img, draw, x0, y0, size, snapshot, last):
    sq = size / 8
    rows = snapshot.split("/")
    lit = {last[:2], last[2:4]} if last else set()
    for r, row in enumerate(rows):
        for c in range(8):
            name = "abcdefgh"[c] + str(8 - r)
            colour = LAST if name in lit else (LIGHT if (r + c) % 2 == 0 else DARK)
            draw.rectangle([s(x0 + c * sq), s(y0 + r * sq), s(x0 + (c + 1) * sq), s(y0 + (r + 1) * sq)], fill=colour)
        c = 0
        for ch in row:
            if ch.isdigit():
                c += int(ch)
                continue
            code = ("w" if ch.isupper() else "b") + ch.upper()
            piece = piece_image(code, int(sq))
            img.paste(piece, (int(s(x0 + c * sq)), int(s(y0 + r * sq))), piece)
            c += 1
    return size


def frame(img, draw, x0, y0, w, h, wood=((107, 68, 40), (59, 36, 20)), border=.034, radius=12):
    """A wooden frame with a drop shadow; returns the inner box (x, y, w, h)."""
    draw.rounded_rectangle([s(x0 + 3), s(y0 + 6), s(x0 + w + 3), s(y0 + h + 6)], radius=s(radius), fill=(0, 0, 0, 60))
    gradient(img, (x0, y0, x0 + w, y0 + h), wood[0], wood[1], radius)
    pad = w * border
    return x0 + pad, y0 + pad, w - 2 * pad, h - 2 * pad


def square_board(img, draw, x0, y0, size, snapshot, kind):
    """Othello (felt, guide dots, discs) and checkers / Turkish draughts (wood squares, ridged men)."""
    if kind == "othello":
        bx, by, bw, _ = frame(img, draw, x0, y0, size, size)
        sq = bw / 8
        for r in range(8):
            for c in range(8):
                gradient(img, (bx + c * sq + 1, by + r * sq + 1, bx + (c + 1) * sq - 1, by + (r + 1) * sq - 1),
                         (51, 165, 101), (35, 127, 74), 2)
        for gx in (2, 6):
            for gy in (2, 6):
                disc(draw, bx + gx * sq, by + gy * sq, sq * .09, (15, 58, 32), None)
        for i, piece in enumerate(snapshot):
            if piece != ".":
                r, c = divmod(i, 8)
                marble(draw, bx + (c + .5) * sq, by + (r + .5) * sq, sq * .41, piece)
        return size
    bx, by, bw, _ = frame(img, draw, x0, y0, size, size, wood=((90, 52, 27), (45, 25, 11)))
    sq = bw / 8
    for r in range(8):
        for c in range(8):
            box = (bx + c * sq, by + r * sq, bx + (c + 1) * sq, by + (r + 1) * sq)
            if (r + c) % 2 == 0:
                gradient(img, box, (246, 228, 195), (231, 203, 158))
            else:
                gradient(img, box, (138, 86, 48), (107, 63, 32))
                for k in range(1, 5):  # grain
                    gx = box[0] + sq * k / 5
                    draw.line([s(gx), s(box[1]), s(gx + sq * .05), s(box[3])], fill=(0, 0, 0, 22), width=s(1))
    for i, piece in enumerate(snapshot):
        if piece != ".":
            r, c = divmod(i, 8)
            ridged(draw, bx + (c + .5) * sq, by + (r + .5) * sq, sq * .4, piece)
    return size


def go_board(img, draw, x0, y0, size, snapshot, last):
    draw.rounded_rectangle([s(x0 + 3), s(y0 + 6), s(x0 + size + 3), s(y0 + size + 6)], radius=s(10), fill=(0, 0, 0, 60))
    draw.rounded_rectangle([s(x0), s(y0), s(x0 + size), s(y0 + size)], radius=s(10), fill=(110, 69, 32))
    inner = 9
    gradient(img, (x0 + inner, y0 + inner, x0 + size - inner, y0 + size - inner), (236, 199, 124), (216, 166, 75), 4)
    for k in range(0, int(size), 7):  # grain
        draw.line([s(x0 + inner), s(y0 + inner + k), s(x0 + size - inner), s(y0 + inner + k + 3)], fill=(110, 60, 15, 18), width=s(1))
    area = size - 2 * inner
    cell = area / GO_SIZE
    bx, by = x0 + inner, y0 + inner
    first, end = cell / 2, area - cell / 2
    for i in range(GO_SIZE):
        edge = 2 if i in (0, GO_SIZE - 1) else 1
        draw.line([s(bx + first), s(by + first + i * cell), s(bx + end), s(by + first + i * cell)], fill=(59, 40, 16), width=s(edge))
        draw.line([s(bx + first + i * cell), s(by + first), s(bx + first + i * cell), s(by + end)], fill=(59, 40, 16), width=s(edge))
    for r, c in GO_STAR_POINTS:
        disc(draw, bx + first + c * cell, by + first + r * cell, 4, (59, 40, 16), None)
    for i, p in enumerate(snapshot):
        if p != ".":
            cx, cy = bx + first + (i % GO_SIZE) * cell, by + first + (i // GO_SIZE) * cell
            marble(draw, cx, cy, cell * .47, p, glossy=p == "b")
            if last and last != "pass" and i == "ABCDEFGHJ".index(last[0]) + (GO_SIZE - int(last[1:])) * GO_SIZE:
                disc(draw, cx, cy, cell * .17, None, (229, 57, 53), 2)
    return size


def line_board(img, draw, x0, y0, width, snapshot, rules):
    """Alquerque, 16 beads and fanorona: a carved board with engraved lines, holes and marbles."""
    coords = rules.grid.coords
    rows = [r for r, _ in coords]
    cols = [c for _, c in coords]
    span_c, span_r = max(cols) - min(cols), max(rows) - min(rows)
    gap = min(width / (span_c + 1.5), 520 / (span_r + 1.5))
    w, h = gap * (span_c + 1.5), gap * (span_r + 1.5)
    left = x0 + (width - w) / 2
    at = lambda i: (left + (coords[i][1] - min(cols) + .75) * gap, y0 + (coords[i][0] - min(rows) + .75) * gap)
    draw.rounded_rectangle([s(left + 3), s(y0 + 6), s(left + w + 3), s(y0 + h + 6)], radius=s(14), fill=(0, 0, 0, 60))
    gradient(img, (left, y0, left + w, y0 + h), (122, 74, 36), (78, 44, 18), 14)
    rim = gap * .16
    gradient(img, (left + rim, y0 + rim, left + w - rim, y0 + h - rim), (242, 220, 174), (221, 187, 130), 9)
    for a, b in rules.grid.segments():
        (xa, ya), (xb, yb) = at(a), at(b)
        draw.line([s(xa + .6), s(ya + 1), s(xb + .6), s(yb + 1)], fill=(255, 244, 220), width=s(2))
        draw.line([s(xa), s(ya), s(xb), s(yb)], fill=(92, 58, 23), width=s(2))
    for i, p in enumerate(snapshot):
        x, y = at(i)
        disc(draw, x, y, gap * .1, (74, 46, 18), None)
        if p != ".":
            marble(draw, x, y, gap * .34, p)
    return h


def ludo_board(img, draw, x0, y0, size, state):
    draw.rounded_rectangle([s(x0 + 3), s(y0 + 6), s(x0 + size + 3), s(y0 + size + 6)], radius=s(16), fill=(0, 0, 0, 60))
    draw.rounded_rectangle([s(x0), s(y0), s(x0 + size), s(y0 + size)], radius=s(16), fill=(62, 39, 35))
    inner = size * .015
    draw.rounded_rectangle([s(x0 + inner), s(y0 + inner), s(x0 + size - inner), s(y0 + size - inner)], radius=s(10), fill=(255, 250, 240))
    bx, by = x0 + inner, y0 + inner
    cell = (size - 2 * inner) / 15
    pt = lambda r, c: (bx + c * cell, by + r * cell)
    dark = {"red": (183, 28, 28), "green": (27, 94, 32), "yellow": (245, 127, 23), "blue": (13, 71, 161)}
    corners = {"red": (0, 0), "green": (0, 9), "yellow": (9, 9), "blue": (9, 0)}
    spots = [(2, 2), (2, 4), (4, 2), (4, 4)]
    for colour, (r, c) in corners.items():
        x, y = pt(r, c)
        gradient(img, (x + cell * .05, y + cell * .05, x + cell * 5.95, y + cell * 5.95), LUDO[colour], dark[colour], cell * .35)
        draw.rounded_rectangle([s(x + cell * 1.05), s(y + cell * 1.12), s(x + cell * 5), s(y + cell * 5.07)], radius=s(cell * .5), fill=(0, 0, 0, 45))
        draw.rounded_rectangle([s(x + cell), s(y + cell), s(x + cell * 5), s(y + cell * 5)], radius=s(cell * .5), fill=(255, 255, 255))
        for dr, dc in spots:
            sx, sy = pt(r + dr, c + dc)
            disc(draw, sx, sy, cell * .62, LUDO[colour] + (56,), LUDO[colour], cell * .07)
    starts = {offset: colour for colour, offset in LUDO_OFFSET.items()}
    for i, (r, c) in enumerate(LUDO_TRACK):
        x, y = pt(r, c)
        draw.rounded_rectangle([s(x + cell * .03), s(y + cell * .03), s(x + cell * .97), s(y + cell * .97)], radius=s(cell * .1),
                               fill=LUDO[starts[i]] if i in starts else (255, 255, 255), outline=(214, 207, 194), width=s(1))
        if i in starts:
            draw.polygon(star_points(x + cell / 2, y + cell / 2, cell * .3), fill=(255, 255, 255))
        elif i in LUDO_SAFE:
            draw.polygon(star_points(x + cell / 2, y + cell / 2, cell * .32), fill=(255, 193, 7), outline=(180, 130, 0))
    for colour, cells in LUDO_HOME_COLUMNS.items():
        for r, c in cells:
            x, y = pt(r, c)
            gradient(img, (x + cell * .03, y + cell * .03, x + cell * .97, y + cell * .97), LUDO[colour], dark[colour], cell * .1)
        er, ec = LUDO_TRACK[(LUDO_OFFSET[colour] + 50) % 52]
        hr, hc = cells[0]
        ang = math.atan2(hr - er, hc - ec)
        cx, cy = pt(er + .5, ec + .5)
        tip = [(.2, 0), (-.22, -.2), (-.22, .2)]
        draw.polygon([(s(cx + (px * math.cos(ang) - py * math.sin(ang)) * cell), s(cy + (px * math.sin(ang) + py * math.cos(ang)) * cell))
                      for px, py in tip], fill=LUDO[colour])
    tri = lambda pts: [(s(bx + px * cell), s(by + py * cell)) for px, py in pts]
    draw.polygon(tri([(6, 6), (7.5, 7.5), (6, 9)]), fill=LUDO["red"])
    draw.polygon(tri([(6, 6), (9, 6), (7.5, 7.5)]), fill=LUDO["green"])
    draw.polygon(tri([(9, 6), (9, 9), (7.5, 7.5)]), fill=LUDO["yellow"])
    draw.polygon(tri([(6, 9), (9, 9), (7.5, 7.5)]), fill=LUDO["blue"])
    disc(draw, bx + 7.5 * cell, by + 7.5 * cell, cell * .62, (255, 255, 255), (200, 200, 200), 1)
    draw.polygon(star_points(bx + 7.5 * cell, by + 7.5 * cell, cell * .45), fill=(255, 193, 7), outline=(180, 130, 0))
    home = {"red": (6.55, 7.5), "green": (7.5, 6.55), "yellow": (8.45, 7.5), "blue": (7.5, 8.45)}
    places = {}
    for player, tokens in enumerate(state["tokens"]):
        colour = state["colours"][player]
        for t, p in enumerate(tokens):
            if p == -1:
                r, c = corners[colour]
                x, y = c + spots[t][1], r + spots[t][0]
            elif p <= 50:
                r, c = LUDO_TRACK[(LUDO_OFFSET[colour] + p) % 52]
                x, y = c + .5, r + .5
            elif p < 56:
                r, c = LUDO_HOME_COLUMNS[colour][p - 51]
                x, y = c + .5, r + .5
            else:
                x, y = home[colour]
            places.setdefault((x, y), []).append(colour)
    for (x, y), colours in places.items():
        for k, colour in enumerate(colours):
            spread = .2 if len(colours) > 1 else 0
            ox, oy = spread * (1 if k % 2 else -1), spread * (1 if k > 1 else -1) * (len(colours) > 2)
            radius = .27 if len(colours) > 1 else .36
            pawn(draw, bx + (x + ox) * cell, by + (y + oy - radius * .1) * cell, cell * radius, LUDO[colour])
    return size


def snakes_cell(n):
    i = n - 1
    row, col = divmod(i, 10)
    col = col if row % 2 == 0 else 9 - col
    return col + .5, 9 - row + .5


SNAKE_COLOURS = [("#2e7d32", "#a5d6a7"), ("#6a1b9a", "#e1bee7"), ("#e65100", "#ffcc80"), ("#c62828", "#ffcdd2"),
                 ("#00695c", "#b2dfdb"), ("#4e342e", "#d7ccc8"), ("#283593", "#c5cae9"), ("#9e9d24", "#f0f4c3"),
                 ("#ad1457", "#f8bbd0"), ("#1565c0", "#bbdefb")]
TILE_COLOURS = ["#ff8a80", "#ffd180", "#ffff8d", "#b9f6ca", "#80d8ff", "#cf9fff"]


def snakes_board(img, draw, x0, y0, size, state, players):
    border = size * .032
    draw.rounded_rectangle([s(x0 + 3), s(y0 + 6), s(x0 + size + 3), s(y0 + size + 6)], radius=s(16), fill=(0, 0, 0, 60))
    gradient(img, (x0, y0, x0 + size, y0 + size), (46, 125, 50), (27, 94, 32), 16)
    draw.rounded_rectangle([s(x0 + border * .65), s(y0 + border * .65), s(x0 + size - border * .65), s(y0 + size - border * .65)],
                           radius=s(6), fill=(255, 253, 231))
    bx, by = x0 + border, y0 + border
    cell = (size - 2 * border) / 10
    pt = lambda x, y: (s(bx + x * cell), s(by + y * cell))
    for n in range(1, 101):
        x, y = snakes_cell(n)
        row, col = (n - 1) // 10, int(x - .5)
        tile = rgb(TILE_COLOURS[(row + col) % len(TILE_COLOURS)])
        gradient(img, (bx + (x - .47) * cell, by + (y - .47) * cell, bx + (x + .47) * cell, by + (y + .47) * cell),
                 mix(tile, (255, 255, 255), .45), tile, cell * .12)
        text(draw, bx + (x - .42) * cell, by + (y - .45) * cell, str(n), max(8, int(cell * .2)), (90, 70, 50))
        if n == 1:
            f = font(max(7, int(cell * .22)), font_stack()[0][0] if font_stack() else None)
            draw.text(pt(x, y + .16), "START", font=f, fill=(27, 94, 32), anchor="ms")
        elif n == 100:  # chequered flag
            px, py = bx + (x - .12) * cell, by + (y - .25) * cell
            draw.line([s(px), s(py), s(px), s(py + cell * .55)], fill=(60, 60, 60), width=s(2))
            sq = cell * .09
            for a in range(4):
                for b in range(3):
                    draw.rectangle([s(px + a * sq), s(py + b * sq), s(px + (a + 1) * sq), s(py + (b + 1) * sq)],
                                   fill=(20, 20, 20) if (a + b) % 2 else (255, 255, 255))
    for start, end in LADDERS.items():  # wooden ladders with a shadow
        (x1, y1), (x2, y2) = snakes_cell(start), snakes_cell(end)
        length = math.hypot(x2 - x1, y2 - y1)
        nx, ny = -(y2 - y1) / length * .17, (x2 - x1) / length * .17
        rungs, t = [], .1
        while t < .95:
            rungs.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
            t += .32 / length
        for dx, dy, colour, rail, rung in ((.06, .08, (0, 0, 0, 55), .1, .07), (0, 0, (109, 63, 23), .1, .07), (0, 0, (217, 160, 102), .04, .035)):
            for x, y in rungs:
                draw.line([*pt(x + nx + dx, y + ny + dy), *pt(x - nx + dx, y - ny + dy)], fill=colour, width=max(1, int(s(cell * rung))))
            for side in (1, -1):
                draw.line([*pt(x1 + nx * side + dx, y1 + ny * side + dy), *pt(x2 + nx * side + dx, y2 + ny * side + dy)],
                          fill=colour, width=max(1, int(s(cell * rail))))
    for k, (head, tail) in enumerate(SNAKES.items()):  # tapered, spotted snakes with eyes and a forked tongue
        dark_c, light_c = (rgb(c) for c in SNAKE_COLOURS[k % len(SNAKE_COLOURS)])
        (x1, y1), (x2, y2) = snakes_cell(head), snakes_cell(tail)
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        nx, ny, w = -dy / length, dx / length, min(1, length / 3.5)
        c1 = (x1 + dx * .3 + nx * w, y1 + dy * .3 + ny * w)
        c2 = (x1 + dx * .68 - nx * w, y1 + dy * .68 - ny * w)
        body = []
        for i in range(31):
            t = i / 30
            body.append(((1 - t) ** 3 * x1 + 3 * (1 - t) ** 2 * t * c1[0] + 3 * (1 - t) * t ** 2 * c2[0] + t ** 3 * x2,
                         (1 - t) ** 3 * y1 + 3 * (1 - t) ** 2 * t * c1[1] + 3 * (1 - t) * t ** 2 * c2[1] + t ** 3 * y2))
        for offset, colour in (((.07, .09), (0, 0, 0, 50)), ((0, 0), dark_c)):
            for i in range(30):
                width = (.34 - .26 * i / 30) * cell
                (ax, ay), (bx2, by2) = body[i], body[i + 1]
                draw.line([*pt(ax + offset[0], ay + offset[1]), *pt(bx2 + offset[0], by2 + offset[1])], fill=colour, width=max(1, int(s(width))))
                disc(draw, bx + (bx2 + offset[0]) * cell, by + (by2 + offset[1]) * cell, width / 2, colour, None)
        for i in range(1, 30, 3):
            (ax, ay), (bx2, by2) = body[i], body[i + 1]
            disc(draw, bx + (ax + bx2) / 2 * cell, by + (ay + by2) / 2 * cell, (.34 - .26 * i / 30) * cell * .22, light_c, None)
        fx, fy = x1 - body[1][0], y1 - body[1][1]
        fl = math.hypot(fx, fy)
        fx, fy = fx / fl, fy / fl
        px, py = -fy, fx
        hx, hy = bx + x1 * cell, by + y1 * cell
        draw.line([s(hx + fx * cell * .28), s(hy + fy * cell * .28), s(hx + fx * cell * .48), s(hy + fy * cell * .48)], fill=(229, 57, 53), width=3)
        for side in (1, -1):
            draw.line([s(hx + fx * cell * .48), s(hy + fy * cell * .48), s(hx + (fx * .56 + px * .06 * side) * cell), s(hy + (fy * .56 + py * .06 * side) * cell)],
                      fill=(229, 57, 53), width=3)
        disc(draw, hx + fx * cell * .04, hy + fy * cell * .04, cell * .25, dark_c, None)
        for side in (1, -1):
            ex, ey = hx + (fx * .12 + px * .1 * side) * cell, hy + (fy * .12 + py * .1 * side) * cell
            disc(draw, ex, ey, cell * .07, (255, 255, 255), None)
            disc(draw, ex + fx * cell * .02, ey + fy * cell * .02, cell * .035, (17, 17, 17), None)
    spots = {}
    for i, p in enumerate(state["pos"]):
        if p:
            spots.setdefault(p, []).append(i)
    for square, who in spots.items():
        x, y = snakes_cell(square)
        for k, i in enumerate(who):
            ox = (k % 2 - .5) * .4 if len(who) > 1 else 0
            oy = (k // 2 - .3) * .34 if len(who) > 1 else 0
            name = clean(players[i]["name"]) if players else ""
            pawn(draw, bx + (x + ox) * cell, by + (y + oy) * cell, cell * (.22 if len(who) > 1 else .3),
                 PARTY[i % len(PARTY)], name[0] if name else "")
    return size


# ---------------------------------------------------------------- whole pictures

def compose(title, headline, players, draw_board, board_height):
    """players: [(swatch colour, label, is_winner)]. draw_board(img, draw, x, y, width) -> height used."""
    board_w = WIDTH - 2 * PAD
    height = int(PAD + 36 + 34 + 16 + board_height + 18 + 34 * len(players) + PAD)
    img = Image.new("RGB", (s(WIDTH), s(height)), PAPER)
    draw = ImageDraw.Draw(img, "RGBA")  # on an RGB canvas, see-through shadows and highlights blend in
    text(draw, PAD, PAD, title, 28)
    text(draw, PAD, PAD + 40, headline, 20, HINT)
    used = draw_board(img, draw, PAD, PAD + 86, board_w)
    y = PAD + 86 + used + 18
    for colour, label, winner in players:
        disc(draw, PAD + 10, y + 12, 10, colour, (60, 60, 60), 2)
        text(draw, PAD + 30, y + 1, label, 19)
        if winner:
            crown(draw, PAD + 30 + text_width(label, 19) + 18, y + 12, 9)
        y += 34
    img = img.resize((WIDTH, height), Image.LANCZOS).convert("RGB")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def board_game_picture(game, snapshot, last, headline):
    """Two-player games: the final position plus both players, the winner marked with a crown."""
    rules = GAMES[game.get("kind", "chess")]
    board_w = WIDTH - 2 * PAD
    first = game.get("first", "white")
    players = []
    for colour in (first, "white" if first == "black" else "black"):
        swatch = WHITE_DISC[0] if colour == "white" else BLACK_DISC[0]
        players.append((swatch, f"{clean(game[colour]['name'])} ({colour.capitalize()})", game.get("result") == colour))
    if hasattr(rules, "grid"):
        coords = rules.grid.coords
        span_c = max(c for _, c in coords) - min(c for _, c in coords) + 1
        span_r = max(r for r, _ in coords) - min(r for r, _ in coords) + 1
        gap = min(board_w / (span_c + .5), 520 / (span_r + .5))
        height = gap * (span_r + .5)
        draw_board = lambda img, d, x, y, w: line_board(img, d, x, y, w, snapshot, rules)
    elif rules.kind == "chess":
        height, draw_board = board_w, lambda img, d, x, y, w: chess_board(img, d, x, y, w, snapshot, last)
    elif rules.kind == "go":
        height, draw_board = board_w, lambda img, d, x, y, w: go_board(img, d, x, y, w, snapshot, last)
    else:
        height, draw_board = board_w, lambda img, d, x, y, w: square_board(img, d, x, y, w, snapshot, rules.kind)
    return compose(clean(rules.title) + " - final position", clean(headline), players, draw_board, height)


def party_picture(game, headline):
    """Ludo and Snakes and Ladders: the final board plus each player's colour and progress."""
    state = game["state"]
    board_w = WIDTH - 2 * PAD
    players = []
    for i, p in enumerate(game["players"]):
        if game["kind"] == "ludo":
            colour = LUDO[p["colour"]]
            info = f"{sum(t == 56 for t in state['tokens'][i])}/4 home"
        else:
            colour = PARTY[i % len(PARTY)]
            info = f"square {state['pos'][i]}"
        if (state.get("left") or [False] * len(game["players"]))[i]:
            info += " (left)"
        players.append((colour, f"{clean(p['name'])} - {info}", state["winner"] == i))
    draw_board = (lambda img, d, x, y, w: ludo_board(img, d, x, y, w, state)) if game["kind"] == "ludo" else \
        (lambda img, d, x, y, w: snakes_board(img, d, x, y, w, state, game["players"]))
    title = "Ludo" if game["kind"] == "ludo" else "Snakes and Ladders"
    return compose(f"{title} - final board", clean(headline), players, draw_board, board_w)
