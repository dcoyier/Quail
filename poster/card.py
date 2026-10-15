"""Writes the business card pages: python3 poster/card.py OUT_DIR, then print each HTML file to PDF.

card.html:       one single-sided 3.5 x 2 in card with 0.125 in bleed.
card-sheet.html: ten cards on US Letter with crop marks."""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
fonts = os.path.join(here, "fonts")

STYLE = f"""
@font-face {{ font-family: "Source Serif 4"; src: url("file://{fonts}/SourceSerif4SmText-Bold.ttf"); font-weight: 700; }}
@font-face {{ font-family: "Source Sans 3"; src: url("file://{fonts}/SourceSans3-Regular.ttf"); font-weight: 400; }}
@font-face {{ font-family: "Source Sans 3"; src: url("file://{fonts}/SourceSans3-Semibold.ttf"); font-weight: 600; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: "Source Sans 3", sans-serif; color: #141414; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.card {{ position: absolute; width: 3.5in; height: 2in; overflow: hidden; }}
.front {{ background: #fff; padding: 0.2in 0.26in; display: flex; flex-direction: column; }}
.front .name {{ font-family: "Source Serif 4", serif; font-weight: 700; font-size: 16pt; line-height: 1.05; color: #003069; }}
.front .role {{ margin-top: 0.1in; font-size: 8.6pt; line-height: 1.35; color: #3A3632; }}
.front .contact {{ margin-top: auto; font-size: 8.6pt; line-height: 1.3; color: #141414; }}
.front .contact b {{ font-weight: 600; }}
"""

FRONT = """<div class="card front" style="{pos}">
  <div class="name">Dashiell Coyier</div>
  <div class="role">Computer Science and Math<br>Carleton College, Class of 2029<br>Data Scientist, DataSquad</div>
  <div class="contact"><b>coyierd@carleton.edu</b><br>515-732-8372<br>github.com/dcoyier<br>linkedin.com/in/dashiell-coyier-a9a518293</div>
</div>"""



def page(body, size):
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Quail card</title>
<style>@page {{ size: {size}; margin: 0; }}{STYLE}
.page {{ position: relative; break-after: page; overflow: hidden; }}</style></head><body>{body}</body></html>"""


# One card with bleed: the page is 3.75 x 2.25 in and the card's background runs to its edges.
B = 0.125
single = []
for card, bg in ((FRONT, "#fff"),):
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
for card, bleed in ((FRONT, ""),):
    cards = "".join(card.format(pos=f"left:{x}in;top:{y}in") for y in ys[:5] for x in xs[:2])
    sheets.append(f'<div class="page" style="width:8.5in;height:11in">{bleed}{cards}{"".join(marks)}</div>')
open(os.path.join(out, "card-sheet.html"), "w").write(page("".join(sheets), "letter"))
