"""Writes poster-44x48.pdf: python3 poster/sheet.py OUT_DIR  (needs pypdf and reportlab)

For printers whose nearest size is 44 x 48 in. The 44 x 44 in poster.pdf sits unscaled at the top
of the sheet, so the only cut is across blank paper 44 in down; marks just below that line show
where to trim. The poster page is copied as-is, so its text and fonts stay vector."""
import io
import os
import sys

from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

IN = 72
W, H, POSTER = 44 * IN, 48 * IN, 44 * IN
WASTE = H - POSTER

here = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1]
poster = PdfReader(os.path.join(out, "poster.pdf")).pages[0]
assert (round(float(poster.mediabox.width)), round(float(poster.mediabox.height))) == (POSTER, POSTER)

pdfmetrics.registerFont(TTFont("SourceSans3", os.path.join(here, "fonts", "SourceSans3-Regular.ttf")))
marks = io.BytesIO()
c = canvas.Canvas(marks, pagesize=(W, H), initialFontName="SourceSans3")
c.setLineWidth(1)
y = WASTE - 0.5  # the stroke ends at the trim line, so nothing is left on the poster
for x in (0.5 * IN, W - 1.5 * IN):
    c.line(x, y, x + IN, y)
c.setFont("SourceSans3", 24)
c.setFillColorRGB(0.38, 0.36, 0.35)
c.drawCentredString(W / 2, WASTE - 0.6 * IN,
                    "Trim along the marks, or up to ¼ in above them, for a 44 × 44 in poster. "
                    "Everything below the marks is waste.")
c.save()

sheet = PageObject.create_blank_page(width=W, height=H)
sheet.merge_transformed_page(poster, Transformation().translate(0, WASTE))
sheet.merge_page(PdfReader(marks).pages[0])
writer = PdfWriter()
writer.add_page(sheet)
with open(os.path.join(out, "poster-44x48.pdf"), "wb") as f:
    writer.write(f)
