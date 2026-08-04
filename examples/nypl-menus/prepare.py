"""Build the two NYPL menus CSVs (item grain + menu-document grain) from the
pinned "What's on the Menu?" export. Run: python prepare.py [--force]"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import tarfile
import urllib.request
from collections import defaultdict
from pathlib import Path

# Final biweekly export published by NYPL (the site and API were retired in
# January 2025; the S3 bucket remains). Pinned by checksum so the trial corpus
# is reproducible byte-for-byte.
EXPORT_URL = (
    "https://s3.amazonaws.com/menusdata.nypl.org/gzips/"
    "2023_03_16_07_02_35_data.tgz"
)
EXPORT_SHA256 = "fff07d8853bfc4a256d9fd9ad4b22908808e61d4428ef2779cfbfa38fbc185c5"

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
RAW_DIR = DATA_DIR / "raw"
ITEMS_CSV = DATA_DIR / "nypl_items.csv"
MENU_DOCS_CSV = DATA_DIR / "nypl_menu_docs.csv"

# Deliberately NOT imported from Dish.csv: menus_appeared, times_appeared,
# first_appeared, last_appeared, lowest_price, highest_price. Those are the
# web application's precomputed global aggregates; the analysis agent must
# derive such figures itself, and importing them would leak ready-made
# answers for first-appearance findings.

csv.field_size_limit(10_000_000)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_export(force: bool) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    archive = RAW_DIR / EXPORT_URL.rsplit("/", 1)[-1]
    if force or not archive.exists():
        print(f"downloading {EXPORT_URL}")
        urllib.request.urlretrieve(EXPORT_URL, archive)
    actual = sha256(archive)
    if actual != EXPORT_SHA256:
        sys.exit(f"checksum mismatch for {archive}: {actual}")
    for name in ("Menu.csv", "MenuPage.csv", "MenuItem.csv", "Dish.csv"):
        if force or not (RAW_DIR / name).exists():
            with tarfile.open(archive) as tar:
                tar.extractall(RAW_DIR, filter="data")
            break
    return RAW_DIR


def read_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def clean(value: str | None) -> str:
    return (value or "").strip()


def menu_year(date: str) -> str:
    prefix = date[:4]
    return prefix if prefix.isdigit() and 1840 <= int(prefix) <= 2030 else ""


def build(raw: Path) -> None:
    menus: dict[str, dict[str, str]] = {}
    for row in read_rows(raw / "Menu.csv"):
        date = clean(row["date"])
        menus[row["id"]] = {
            "menu_id": row["id"],
            "date": date,
            "year": menu_year(date),
            "location": clean(row["location"]),
            "sponsor": clean(row["sponsor"]),
            "event": clean(row["event"]),
            "venue": clean(row["venue"]),
            "place": clean(row["place"]),
            "occasion": clean(row["occasion"]),
            "currency": clean(row["currency"]),
        }

    pages: dict[str, tuple[str, int, str]] = {}
    page_counts: dict[str, set[int]] = defaultdict(set)
    for row in read_rows(raw / "MenuPage.csv"):
        number = int(row["page_number"] or 0)
        pages[row["id"]] = (row["menu_id"], number, clean(row["uuid"]))
        page_counts[row["menu_id"]].add(number)

    dishes: dict[str, str] = {}
    for row in read_rows(raw / "Dish.csv"):
        name = clean(row["name"])
        if name:
            dishes[row["id"]] = name

    menu_fields = [
        "menu_id", "date", "year", "location", "sponsor", "event",
        "venue", "place", "occasion", "currency",
    ]
    item_fields = [
        "item_id", "dish_name", "price", "high_price", *menu_fields,
        "page_number", "page_uuid", "xpos", "ypos",
    ]

    doc_lines: dict[str, list[tuple[int, float, str]]] = defaultdict(list)
    items = orphans = 0
    with ITEMS_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=item_fields)
        writer.writeheader()
        for row in read_rows(raw / "MenuItem.csv"):
            page = pages.get(row["menu_page_id"])
            dish = dishes.get(row["dish_id"])
            menu = menus.get(page[0]) if page else None
            if page is None or dish is None or menu is None:
                orphans += 1
                continue
            _, page_number, page_uuid = page
            ypos = float(row["ypos"] or 0.0)
            writer.writerow({
                "item_id": row["id"],
                "dish_name": dish,
                "price": clean(row["price"]),
                "high_price": clean(row["high_price"]),
                **menu,
                "page_number": page_number,
                "page_uuid": page_uuid,
                "xpos": clean(row["xpos"]),
                "ypos": clean(row["ypos"]),
            })
            doc_lines[menu["menu_id"]].append((page_number, ypos, dish))
            items += 1

    doc_fields = [*menu_fields, "page_count", "dish_count", "menu_text"]
    with MENU_DOCS_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=doc_fields)
        writer.writeheader()
        for menu_id, lines in doc_lines.items():
            lines.sort()
            writer.writerow({
                **menus[menu_id],
                "page_count": len(page_counts[menu_id]),
                "dish_count": len(lines),
                "menu_text": "\n".join(name for _, _, name in lines),
            })

    print(f"items:     {items} rows -> {ITEMS_CSV} ({orphans} orphans dropped)")
    print(f"menu docs: {len(doc_lines)} rows -> {MENU_DOCS_CSV}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="re-download and re-extract the pinned export")
    build(fetch_export(parser.parse_args().force))


if __name__ == "__main__":
    main()
