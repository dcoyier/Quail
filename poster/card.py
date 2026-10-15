"""Writes the business card pages: python3 poster/card.py OUT_DIR, then print each HTML file to PDF.

card.html:       one 3.5 x 2 in card with 0.125 in bleed; page 1 is the front, page 2 the back.
card-sheet.html: ten cards on US Letter with crop marks; page 1 fronts, page 2 backs (duplex, long edge)."""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
fonts = os.path.join(here, "fonts")
logo = os.path.join(os.path.dirname(here), "quail.png")
qr = os.path.join(here, "figures", "qr.svg")

STYLE = f"""
@font-face {{ font-family: "Source Serif 4"; src: url("file://{fonts}/SourceSerif4SmText-Bold.ttf"); font-weight: 700; }}
@font-face {{ font-family: "Source Sans 3"; src: url("file://{fonts}/SourceSans3-Regular.ttf"); font-weight: 400; }}
@font-face {{ font-family: "Source Sans 3"; src: url("file://{fonts}/SourceSans3-Semibold.ttf"); font-weight: 600; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: "Source Sans 3", sans-serif; color: #141414; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.card {{ position: absolute; width: 3.5in; height: 2in; overflow: hidden; }}
.front {{ background: #fff; padding: 0.24in 0.26in; display: flex; flex-direction: column; }}
.front .name {{ font-family: "Source Serif 4", serif; font-weight: 700; font-size: 16pt; line-height: 1.05; color: #003069; }}
.front .rule {{ width: 0.42in; height: 2.2pt; background: #FFD24F; margin: 0.09in 0 0.08in; }}
.front .role {{ font-size: 8.6pt; line-height: 1.35; color: #3A3632; }}
.front .contact {{ margin-top: auto; font-size: 8.6pt; line-height: 1.4; color: #141414; }}
.front .contact b {{ font-weight: 600; }}
.back {{ background: #003069; color: #fff; padding: 0.22in 0.24in; display: flex; gap: 0.16in; align-items: center; }}
.back .left {{ flex: 1; min-width: 0; display: flex; flex-direction: column; height: 100%; }}
.back .brand {{ display: flex; align-items: center; gap: 0.08in; }}
.back .logo {{ width: 0.5in; height: 0.5in; filter: invert(1); mix-blend-mode: screen; }}
.back .qname {{ font-family: "Source Serif 4", serif; font-weight: 700; font-size: 21pt; line-height: 1; }}
.back .qsub {{ margin-top: 0.1in; font-size: 7.6pt; line-height: 1.3; color: #D6E2F0; }}
.back .rule {{ width: 0.42in; height: 2.2pt; background: #FFD24F; margin-top: 0.1in; }}
.back .url {{ margin-top: auto; font-size: 8pt; font-weight: 600; color: #fff; }}
.back .qr {{ width: 1.02in; height: 1.02in; background: #fff; border-radius: 0.06in; padding: 0.1in; }}
.back .qr img {{ width: 100%; height: 100%; display: block; }}
"""

FRONT = """<div class="card front" style="{pos}">
  <div class="name">Dashiell Coyier</div>
  <div class="rule"></div>
  <div class="role">Data Scientist, DataSquad<br>Carleton College</div>
  <div class="contact"><b>coyierd@carleton.edu</b><br>github.com/dcoyier/Quail</div>
</div>"""

BACK = f"""<div class="card back" style="{{pos}}">
  <div class="left">
    <div class="brand"><img class="logo" src="file://{logo}" alt=""><div class="qname">Quail</div></div>
    <div class="qsub">An environment for agentic<br>qualitative analysis</div>
    <div class="url">github.com/dcoyier/Quail</div>
  </div>
  <div class="qr"><img src="file://{qr}" alt="QR code for github.com/dcoyier/Quail"></div>
</div>"""


def page(body, size):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Quail card</title>
<style>@page {{ size: {size}; margin: 0; }}{STYLE}
.page {{ position: relative; break-after: page; overflow: hidden; }}</style></head><body>{body}</body></html>"""


# One card with bleed: the page is 3.75 x 2.25 in and the card's background runs to its edges.
B = 0.125
single = []
for card, bg in ((FRONT, "#fff"), (BACK, "#003069")):
    single.append(f'<div class="page" style="width:3.75in;height:2.25in;background:{bg}">'
                  + card.format(pos=f"left:{B}in;top:{B}in") + "</div>")
open(os.path.join(out, "card.html"), "w").write(page("".join(single), "3.75in 2.25in"))

# Ten up on Letter: 2 x 5 cards butted together, centered, with crop marks in the margins.
X0, Y0, CW, CH = 0.75, 0.5, 3.5, 2.0
xs = [X0 + i * CW for i in range(3)]
ys = [Y0 + j * CH for j in range(6)]
marks = []
for x in xs:  # vertical marks above and below the grid
    marks.append(f'<div style="position:absolute;left:{x}in;top:0.05in;width:0;height:0.25in;border-left:0.5pt solid #000"></div>')
    marks.append(f'<div style="position:absolute;left:{x}in;top:{11 - 0.3}in;width:0;height:0.25in;border-left:0.5pt solid #000"></div>')
for y in ys:  # horizontal marks left and right of the grid
    marks.append(f'<div style="position:absolute;top:{y}in;left:0.2in;height:0;width:0.35in;border-top:0.5pt solid #000"></div>')
    marks.append(f'<div style="position:absolute;top:{y}in;left:{8.5 - 0.55}in;height:0;width:0.35in;border-top:0.5pt solid #000"></div>')
sheets = []
for card, bleed in ((FRONT, ""), (BACK, f'<div style="position:absolute;left:{X0 - B}in;top:{Y0 - B}in;width:{2 * CW + 2 * B}in;height:{5 * CH + 2 * B}in;background:#003069"></div>')):
    cards = "".join(card.format(pos=f"left:{x}in;top:{y}in") for y in ys[:5] for x in xs[:2])
    sheets.append(f'<div class="page" style="width:8.5in;height:11in">{bleed}{cards}{"".join(marks)}</div>')
open(os.path.join(out, "card-sheet.html"), "w").write(page("".join(sheets), "letter"))
