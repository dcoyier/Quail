"""Writes language.svg: python3 poster/figures/language.py poster/figures/language.svg

Analysis-language figure, top to bottom in the order things are built:
Fields -> Recipes (expressions, predicates) -> Functions; the tag field is maize wherever it appears.
Mono text uses Source Code Pro's fixed advance (0.6 em) so code chips are sized exactly."""
import sys
from xml.sax.saxutils import escape

W = CONTENT = 1384
BLUE, MAIZE, BLUE_TINT = "#003069", "#FFD24F", "#D6E2F0"
BAND, FIELD, INK = "#F4F2EF", "#E4E2DE", "#141414"
MONO = 22
ADV = MONO * 0.6
HS = 64                           # height of a level's header strip
PAD = 28

out = []
def add(s): out.append("  " + s)
def t(x, y, s, cls="s", anchor=None):
    a = f' text-anchor="{anchor}"' if anchor else ""
    keep = ' xml:space="preserve"' if cls == "code" else ""
    add(f'<text class="{cls}" x="{x:g}" y="{y:g}"{a}{keep}>{escape(s)}</text>')
def code_w(s): return len(s) * ADV
def hexagon(x, y, w, h, cls):
    k = h / 2
    add(f'<path class="{cls}" d="M{x+k:g},{y:g} H{x+w-k:g} L{x+w:g},{y+k:g} L{x+w-k:g},{y+h:g} H{x+k:g} L{x:g},{y+k:g} Z"/>')
def rrect(x, y, w, h, cls, r):
    add(f'<rect class="{cls}" x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{r:g}"/>')

def level(y, h, title, gloss, title_chars):
    """A level: light body, solid blue header strip, blue outline."""
    r = 22
    add(f'<rect x="0" y="{y:g}" width="{CONTENT}" height="{h:g}" rx="{r}" fill="{BAND}"/>')
    add(f'<path d="M0,{y+HS:g} V{y+r:g} Q0,{y:g} {r},{y:g} H{CONTENT-r} Q{CONTENT},{y:g} {CONTENT},{y+r:g} V{y+HS:g} Z" fill="{BLUE}"/>')
    add(f'<rect x="2" y="{y+2:g}" width="{CONTENT-4}" height="{h-4:g}" rx="{r-2}" fill="none" stroke="{BLUE}" stroke-width="4"/>')
    t(PAD, y + 45, title, "lvtitle")
    t(PAD + title_chars * 19 + 26, y + 44, gloss, "lvgloss")

def block_arrow(cx, y0, y1):
    """A thick arrow pointing down from y0 to y1, centred on cx."""
    sw, hw, hl = 46, 112, 50
    add(f'<path d="M{cx-sw/2:g},{y0:g} H{cx+sw/2:g} V{y1-hl:g} H{cx+hw/2:g} L{cx:g},{y1:g} L{cx-hw/2:g},{y1-hl:g} H{cx-sw/2:g} Z" fill="{BLUE}"/>')

G = 7   # room on each side of an inline highlight
def measure(parts):
    return sum(code_w(text) + (2 * G if kind else 0) for text, kind in parts)
def code_line(x, y, parts):
    """Code with inline highlights: 'f' field, 'e' expression."""
    cx = x
    for text, kind in parts:
        w = code_w(text)
        if kind:
            cx += G
            if kind == "f":
                rrect(cx - 4, y - 22, w + 8, 30, "fieldmark", 4)
            else:
                rrect(cx - 5, y - 22, w + 10, 30, "exprmark", 15)
        t(cx, y, text, "code")
        cx += w + (G if kind else 0)
    return cx - x

# ---------- Level 1: Fields ----------
F_Y, F_H = 0, 184
level(F_Y, F_H, "Fields", "the columns of the dataset", 6)
chip_y = F_Y + HS + 48
fx = PAD
for parts in ([("body", None), (' = Field("body")', None)], [("year", None), (' = Field("year")', None)], [('Field("network")', None)]):
    text = "".join(p for p, _ in parts)
    w = code_w(text) + 36
    rrect(fx, chip_y, w, 48, "fieldchip", 6); t(fx + 18, chip_y + 32, text, "code"); fx += w + 24
topic = 'Field("topic")'
tw = code_w(topic) + 36
tx = CONTENT - PAD - tw
rrect(tx, chip_y, tw, 48, "tagchip", 6); t(tx + 18, chip_y + 32, topic, "code")
t(tx + tw / 2, chip_y - 12, "a tag becomes a field", anchor="middle")

# ---------- Level 2: Recipes ----------
GAP_LV = 130
R_Y = F_Y + F_H + GAP_LV
R_H = HS + 510
level(R_Y, R_H, "Recipes", "kept in variables and reused; nothing runs until a function uses them", 7)
mid = CONTENT / 2
col_l, col_r = PAD, mid + 24
for cx, title, gloss in [(col_l, "Expressions", "a value for each entry"), (col_r, "Predicates", "true or false for each entry, built from expressions")]:
    t(cx, R_Y + HS + 56, title, "coltitle")
    t(cx, R_Y + HS + 90, gloss, "s")

def rows(cx, items, kind):
    y = R_Y + HS + 162
    for parts, gloss in items:
        w = measure(parts)
        if kind == "p":
            hexagon(cx, y - 30, w + 48, 44, "predchip"); code_line(cx + 24, y, parts)
        else:
            rrect(cx, y - 30, w + 36, 44, "exprchip", 22); code_line(cx + 18, y, parts)
        t(cx + 4, y + 42, gloss)
        y += 96

rows(col_l, [
    ([("sem", None), (" = ", None), ("body", "f"), ('.semantic("waiting to vote")', None)], "closeness in meaning to a phrase"),
    ([("kw", None), (" = ", None), ("body", "f"), ('.lexical("polls")', None)], "keyword score; above 0 when the word appears"),
    ([("body", "f"), ('.lower().search("long lines?")', None)], "lowercase the text, then find the first match"),
    ([("sem + 0.2 * kw", None)], "scores added together"),
], "e")
rows(col_r, [
    ([("close = ", None), ("sem", "e"), (" > 0.5", None)], "close in meaning"),
    ([("in_2012 = ", None), ("year", "f"), (' == "2012"', None)], "aired in 2012"),
    ([("close & ~in_2012", None)], "close in meaning, and not from 2012"),
    ([("close | (", None), ("kw", "e"), (" > 0)", None)], "close in meaning, or says “polls”"),
], "p")

# ---------- Level 3: Functions ----------
FN_Y = R_Y + R_H + GAP_LV
card_w = (CONTENT - 2 * PAD - 24) / 2
card_h = 196
FN_H = HS + PAD + 2 * card_h + 24 + PAD
level(FN_Y, FN_H, "Functions", "the four core operations of the analysis language", 9)
funcs = [
    ("count", "Counts the matching entries", [("count(", None), ("close", "p"), (", by=", None), ("year", "f"), (")", None)]),
    ("retrieve", "Returns the matching entries to read", [("retrieve(", None), ("close", "p"), (", rank=", None), ("sem", "e"), (")", None)]),
    ("values", "Returns a Python list, one value per entry", [("values(", None), ("body.length()", "e"), (", where=", None), ("close", "p"), (")", None)]),
    ("tag", "Records a decision on the matching entries", [("tag(", None), ("close", "p"), (", ", None), ('"topic"', "t"), (', "voting lines")', None)]),
]
for i, (name, idea, parts) in enumerate(funcs):
    cx = PAD + (i % 2) * (card_w + 24)
    cy = FN_Y + HS + PAD + (i // 2) * (card_h + 24)
    rrect(cx, cy, card_w, card_h, "card", 18)
    t(cx + 26, cy + 54, name, "fname")
    t(cx + 26, cy + 98, idea, "idea")
    x, by = cx + 26, cy + 154
    for text, kind in parts:
        if kind is None:
            t(x, by, text, "code"); x += code_w(text); continue
        pad = {"p": 22, "e": 18, "f": 16, "t": 16}[kind]
        x += pad
        w = code_w(text)
        if kind == "p":
            hexagon(x - 16, by - 27, w + 32, 38, "predchip")
        elif kind == "e":
            rrect(x - 12, by - 27, w + 24, 38, "exprchip", 19)
        elif kind == "f":
            rrect(x - 10, by - 27, w + 20, 38, "fieldchip", 6)
        else:
            rrect(x - 10, by - 27, w + 20, 38, "tagchip", 6)
        t(x, by, text, "code"); x += w + pad

# ---------- Level-to-level arrows ----------
block_arrow(mid, F_Y + F_H + 14, R_Y - 12)
block_arrow(mid, R_Y + R_H + 14, FN_Y - 12)


H = FN_Y + FN_H
svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H:g}" width="{W}" height="{H:g}">
  <style>
    text {{ font-family: 'Source Sans 3', sans-serif; fill: {INK}; }}
    .s {{ font-size: 24px; fill: #625D58; }}
    .lvtitle {{ font-size: 34px; font-weight: 700; fill: #fff; }}
    .lvgloss {{ font-size: 26px; fill: {BLUE_TINT}; }}
    .coltitle {{ font-size: 32px; font-weight: 700; }}
    .idea {{ font-size: 27px; font-weight: 600; }}
    .fname {{ font-family: 'Source Code Pro', monospace; font-size: 36px; font-weight: 700; fill: {BLUE}; font-variant-ligatures: none; }}
    .code {{ font-family: 'Source Code Pro', monospace; font-size: {MONO}px; font-weight: 500; font-variant-ligatures: none; }}
    .card {{ fill: #fff; stroke: {INK}; stroke-width: 3; }}
    .predchip {{ fill: {BLUE_TINT}; stroke: {BLUE}; stroke-width: 2.5; }}
    .exprchip {{ fill: #fff; stroke: {INK}; stroke-width: 2.5; }}
    .fieldchip {{ fill: {FIELD}; stroke: {INK}; stroke-width: 2.5; }}
    .tagchip {{ fill: {MAIZE}; stroke: {INK}; stroke-width: 2.5; }}
    .fieldmark {{ fill: {FIELD}; stroke: {INK}; stroke-width: 1.5; }}
    .exprmark {{ fill: #fff; stroke: {INK}; stroke-width: 1.5; }}
  </style>
{chr(10).join(out)}
</svg>
'''
open(sys.argv[1], "w", encoding="utf-8").write(svg)
print("height", H)
