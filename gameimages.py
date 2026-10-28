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


def stone(draw, cx, cy, r, side):
    """A black or white playing piece ("b"/"w", upper case = king)."""
    fill, edge = BLACK_DISC if side.lower() == "b" else WHITE_DISC
    disc(draw, cx, cy, r, fill, edge, max(1, int(r // 9)))
    if side.isupper():
        crown(draw, cx, cy, r * .55)


def crown(draw, cx, cy, size, colour=GOLD):
    pts = [(-1, .65), (-1.05, -.45), (-.5, .05), (0, -.75), (.5, .05), (1.05, -.45), (1, .65)]
    draw.polygon([(s(cx + x * size), s(cy + y * size)) for x, y in pts], fill=colour, outline=(122, 90, 0), width=s(1))


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
        c = 0
        for ch in row:
            if ch.isdigit():
                c += int(ch)
                continue
            c += 1
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
            img.alpha_composite(piece_image(code, int(sq)), (int(s(x0 + c * sq)), int(s(y0 + r * sq))))
            c += 1
    return size


def square_board(draw, x0, y0, size, snapshot, kind):
    """Othello, checkers and Turkish draughts: 8 x 8, one character per square, top row first."""
    sq = size / 8
    for r in range(8):
        for c in range(8):
            box = [s(x0 + c * sq), s(y0 + r * sq), s(x0 + (c + 1) * sq), s(y0 + (r + 1) * sq)]
            if kind == "othello":
                draw.rectangle(box, fill=(46, 139, 87), outline=(31, 107, 65), width=s(1))
            else:
                draw.rectangle(box, fill=LIGHT if (r + c) % 2 == 0 else DARK)
            piece = snapshot[r * 8 + c]
            if piece != ".":
                stone(draw, x0 + (c + .5) * sq, y0 + (r + .5) * sq, sq * .4, piece)
    return size


def go_board(draw, x0, y0, size, snapshot, last):
    cell = size / GO_SIZE
    draw.rectangle([s(x0), s(y0), s(x0 + size), s(y0 + size)], fill=(220, 179, 92))
    first, end = cell / 2, size - cell / 2
    for i in range(GO_SIZE):
        draw.line([s(x0 + first), s(y0 + first + i * cell), s(x0 + end), s(y0 + first + i * cell)], fill=(107, 74, 27), width=s(1))
        draw.line([s(x0 + first + i * cell), s(y0 + first), s(x0 + first + i * cell), s(y0 + end)], fill=(107, 74, 27), width=s(1))
    for r, c in GO_STAR_POINTS:
        disc(draw, x0 + first + c * cell, y0 + first + r * cell, 4, (107, 74, 27), (107, 74, 27))
    for i, p in enumerate(snapshot):
        if p != ".":
            cx, cy = x0 + first + (i % GO_SIZE) * cell, y0 + first + (i // GO_SIZE) * cell
            stone(draw, cx, cy, cell * .46, p)
            if last and last != "pass" and i == "ABCDEFGHJ".index(last[0]) + (GO_SIZE - int(last[1:])) * GO_SIZE:
                disc(draw, cx, cy, cell * .15, None, (220, 40, 40), 2)
    return size


def line_board(draw, x0, y0, width, snapshot, rules):
    """Alquerque, 16 beads and fanorona: pieces on points joined by lines."""
    coords = rules.grid.coords
    rows = [r for r, _ in coords]
    cols = [c for _, c in coords]
    span_c, span_r = max(cols) - min(cols), max(rows) - min(rows)
    gap = min(width / (span_c + 1), 520 / (span_r + 1))
    w, h = gap * (span_c + 1), gap * (span_r + 1)
    left = x0 + (width - w) / 2
    at = lambda i: (left + (coords[i][1] - min(cols) + .5) * gap, y0 + (coords[i][0] - min(rows) + .5) * gap)
    draw.rounded_rectangle([s(left), s(y0), s(left + w), s(y0 + h)], radius=s(10), fill=(233, 211, 166))
    for a, b in rules.grid.segments():
        (xa, ya), (xb, yb) = at(a), at(b)
        draw.line([s(xa), s(ya), s(xb), s(yb)], fill=(107, 74, 27), width=s(2))
    for i, p in enumerate(snapshot):
        if p != ".":
            stone(draw, *at(i), gap * .34, p)
    return h


def ludo_board(draw, x0, y0, size, state):
    cell = size / 15
    box = lambda r, c, w=1, h=1: [s(x0 + c * cell), s(y0 + r * cell), s(x0 + (c + w) * cell), s(y0 + (r + h) * cell)]
    draw.rectangle(box(0, 0, 15, 15), fill=(255, 255, 255))
    corners = {"red": (0, 0), "green": (0, 9), "yellow": (9, 9), "blue": (9, 0)}
    spots = [(2, 2), (2, 4), (4, 2), (4, 4)]
    for colour, (r, c) in corners.items():
        draw.rectangle(box(r, c, 6, 6), fill=LUDO[colour])
        draw.rounded_rectangle(box(r + 1, c + 1, 4, 4), radius=s(cell * .4), fill=(255, 255, 255))
        for dr, dc in spots:
            disc(draw, x0 + (c + dc) * cell, y0 + (r + dr) * cell, cell * .42, None, LUDO[colour], 2)
    starts = {offset: colour for colour, offset in LUDO_OFFSET.items()}
    for i, (r, c) in enumerate(LUDO_TRACK):
        draw.rectangle(box(r, c), fill=LUDO[starts[i]] if i in starts else (255, 255, 255), outline=(190, 190, 190), width=s(1))
        if i in LUDO_SAFE and i not in starts:
            star_points = [(x0 + (c + .5 + .3 * math.sin(k * math.pi / 5)) * cell if k % 2 == 0 else
                            x0 + (c + .5 + .13 * math.sin(k * math.pi / 5)) * cell,
                            y0 + (r + .5 - .3 * math.cos(k * math.pi / 5)) * cell if k % 2 == 0 else
                            y0 + (r + .5 - .13 * math.cos(k * math.pi / 5)) * cell) for k in range(10)]
            draw.polygon([(s(x), s(y)) for x, y in star_points], fill=(170, 170, 170))
    for colour, cells in LUDO_HOME_COLUMNS.items():
        for r, c in cells:
            draw.rectangle(box(r, c), fill=LUDO[colour], outline=(190, 190, 190), width=s(1))
    centre = lambda pts: [(s(x0 + x * cell), s(y0 + y * cell)) for x, y in pts]
    draw.polygon(centre([(6, 6), (7.5, 7.5), (6, 9)]), fill=LUDO["red"])
    draw.polygon(centre([(6, 6), (9, 6), (7.5, 7.5)]), fill=LUDO["green"])
    draw.polygon(centre([(9, 6), (9, 9), (7.5, 7.5)]), fill=LUDO["yellow"])
    draw.polygon(centre([(6, 9), (9, 9), (7.5, 7.5)]), fill=LUDO["blue"])
    home = {"red": (6.6, 7.5), "green": (7.5, 6.6), "yellow": (8.4, 7.5), "blue": (7.5, 8.4)}
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
            disc(draw, x0 + (x + ox) * cell, y0 + (y + oy) * cell, cell * (.3 if len(colours) > 1 else .36), LUDO[colour], (30, 30, 30), 2)
    return size


def snakes_cell(n):
    i = n - 1
    row, col = divmod(i, 10)
    col = col if row % 2 == 0 else 9 - col
    return col + .5, 9 - row + .5


def snakes_board(draw, x0, y0, size, state):
    cell = size / 10
    pt = lambda x, y: (s(x0 + x * cell), s(y0 + y * cell))
    for n in range(1, 101):
        x, y = snakes_cell(n)
        shade = (255, 243, 214) if (((n - 1) // 10) + n) % 2 else (255, 224, 163)
        draw.rectangle([*pt(x - .5, y - .5), *pt(x + .5, y + .5)], fill=(255, 213, 79) if n == 100 else shade)
        text(draw, x0 + (x - .44) * cell, y0 + (y - .46) * cell, str(n), 11, (154, 123, 79))
    for start, end in LADDERS.items():
        (x1, y1), (x2, y2) = snakes_cell(start), snakes_cell(end)
        length = math.hypot(x2 - x1, y2 - y1)
        nx, ny = -(y2 - y1) / length * .16, (x2 - x1) / length * .16
        for side in (1, -1):
            draw.line([*pt(x1 + nx * side, y1 + ny * side), *pt(x2 + nx * side, y2 + ny * side)], fill=(141, 110, 99), width=s(4))
        t = .12
        while t < 1:
            x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            draw.line([*pt(x + nx, y + ny), *pt(x - nx, y - ny)], fill=(141, 110, 99), width=s(3))
            t += .3 / length
    for head, tail in SNAKES.items():
        (x1, y1), (x2, y2) = snakes_cell(head), snakes_cell(tail)
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        nx, ny, w = -dy / length, dx / length, min(.9, length / 4)
        c1 = (x1 + dx * .33 + nx * w, y1 + dy * .33 + ny * w)
        c2 = (x1 + dx * .66 - nx * w, y1 + dy * .66 - ny * w)
        body = []
        for k in range(41):
            t = k / 40
            bx = (1 - t) ** 3 * x1 + 3 * (1 - t) ** 2 * t * c1[0] + 3 * (1 - t) * t ** 2 * c2[0] + t ** 3 * x2
            by = (1 - t) ** 3 * y1 + 3 * (1 - t) ** 2 * t * c1[1] + 3 * (1 - t) * t ** 2 * c2[1] + t ** 3 * y2
            body.append(pt(bx, by))
        draw.line(body, fill=(46, 125, 50), width=s(int(cell * .22)), joint="curve")
        disc(draw, x0 + x1 * cell, y0 + y1 * cell, cell * .2, (46, 125, 50), (46, 125, 50))
    spots = {}
    for i, p in enumerate(state["pos"]):
        if p:
            spots.setdefault(p, []).append(i)
    for square, players in spots.items():
        x, y = snakes_cell(square)
        for k, i in enumerate(players):
            ox = (k % 2 - .5) * .36 if len(players) > 1 else 0
            oy = (k // 2 - .3) * .3 if len(players) > 1 else 0
            disc(draw, x0 + (x + ox) * cell, y0 + (y + oy) * cell, cell * .24, PARTY[i % len(PARTY)], (30, 30, 30), 2)
    return size


# ---------------------------------------------------------------- whole pictures

def compose(title, headline, players, draw_board, board_height):
    """players: [(swatch colour, label, is_winner)]. draw_board(img, draw, x, y, width) -> height used."""
    board_w = WIDTH - 2 * PAD
    height = int(PAD + 36 + 34 + 16 + board_height + 18 + 34 * len(players) + PAD)
    img = Image.new("RGBA", (s(WIDTH), s(height)), PAPER)
    draw = ImageDraw.Draw(img)
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
        gap = min(board_w / span_c, 520 / span_r)
        height = gap * span_r
        draw_board = lambda img, d, x, y, w: line_board(d, x, y, w, snapshot, rules)
    elif rules.kind == "chess":
        height, draw_board = board_w, lambda img, d, x, y, w: chess_board(img, d, x, y, w, snapshot, last)
    elif rules.kind == "go":
        height, draw_board = board_w, lambda img, d, x, y, w: go_board(d, x, y, w, snapshot, last)
    else:
        height, draw_board = board_w, lambda img, d, x, y, w: square_board(d, x, y, w, snapshot, rules.kind)
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
    draw_board = (lambda img, d, x, y, w: ludo_board(d, x, y, w, state)) if game["kind"] == "ludo" else \
        (lambda img, d, x, y, w: snakes_board(d, x, y, w, state))
    title = "Ludo" if game["kind"] == "ludo" else "Snakes and Ladders"
    return compose(f"{title} - final board", clean(headline), players, draw_board, board_w)
