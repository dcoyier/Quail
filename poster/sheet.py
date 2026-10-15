"""Writes poster-44x48.pdf: python3 poster/sheet.py OUT_DIR  (needs pypdf)

For printers whose nearest size is 44 x 48 in. The 44 x 44 in poster.pdf sits unscaled at the top
of the sheet, so the only cut is across blank paper 44 in down, along a line drawn just below it.
The poster page is copied as-is, so its text and fonts stay vector."""
import os
import sys

from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import DecodedStreamObject, NameObject

IN = 72
W, H, POSTER = 44 * IN, 48 * IN, 44 * IN
WASTE = H - POSTER

out = sys.argv[1]
poster = PdfReader(os.path.join(out, "poster.pdf")).pages[0]
assert (round(float(poster.mediabox.width)), round(float(poster.mediabox.height))) == (POSTER, POSTER)

# A 1 pt black line across the sheet; the stroke ends at the trim line, so nothing is left on the poster.
line = DecodedStreamObject()
line.set_data(f"q 1 w 0 G 0 {WASTE - 0.5:g} m {W} {WASTE - 0.5:g} l S Q".encode())
marks = PageObject.create_blank_page(width=W, height=H)
marks[NameObject("/Contents")] = line

sheet = PageObject.create_blank_page(width=W, height=H)
sheet.merge_transformed_page(poster, Transformation().translate(0, WASTE))
sheet.merge_page(marks)
writer = PdfWriter()
writer.add_page(sheet)
with open(os.path.join(out, "poster-44x48.pdf"), "wb") as f:
    writer.write(f)
