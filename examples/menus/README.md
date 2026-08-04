# NYPL "What's on the Menu?" corpus

Two-grain Quail staging of the New York Public Library's crowdsourced
historical restaurant menu transcriptions (~17.5k menus, ~1.33M dish lines,
1850s–2000s, NYC-centric). Built for the autonomous analysis trial.

## Build

```sh
python prepare.py          # downloads the pinned export, writes data/*.csv
quail process --config /absolute/path/to/examples/menus/quail.toml
quail run     --config /absolute/path/to/examples/menus/quail.toml
```

`prepare.py` downloads the final NYPL S3 export (`2023_03_16`, pinned by
SHA-256), joins the four source tables, and emits two CSVs under `data/`
(gitignored; ~230 MB raw input, keep out of the tree):

## Datasets

**`nypl_items.csv` — one row per transcribed dish line (~1.33M rows).**
The analysis workhorse: counting, price work, per-decade normalization.

| field | notes |
| --- | --- |
| `item_id` | MenuItem id |
| `dish_name` | verbatim transcription (exact-string identity; variants abound) |
| `price`, `high_price` | floats as transcribed; cents/dollars and currency errors exist |
| `menu_id`, `date`, `year` | menu date (97% filled); `year` empty when undated/junk |
| `location` | producing restaurant/organization (100% filled) |
| `sponsor`, `event`, `venue`, `place`, `occasion`, `currency` | NYPL metadata, 22–91% filled |
| `page_number`, `page_uuid` | page provenance; uuid resolves to the scan in NYPL Digital Collections |
| `xpos`, `ypos` | transcription position on the page (course-position proxy) |

**`nypl_menu_docs.csv` — one row per menu (~17.5k rows).**
The document grain: `menu_text` is the full bill of fare, dish lines joined in
(page, ypos) reading order (median ~670 chars, p90 ~4.4k). Carries the same
menu metadata plus `page_count` / `dish_count`. This is the grain for
menu-level semantic similarity; keeping it separate avoids duplicating each
document ~76x across item rows (which would corrupt corpus-relative FTS
statistics and bloat embedding warm).

## Deliberately withheld

`Dish.csv`'s precomputed aggregates (`menus_appeared`, `times_appeared`,
`first_appeared`, `last_appeared`, `lowest_price`, `highest_price`) are not
imported. They are ready-made answers to first-appearance/price questions the
analysis agent must derive itself.

## Corpus shape (from the pinned export)

Dated menus by decade: 1850s 22 · 1860s 21 · 1870s **1** · 1880s 283 ·
1890s 1,482 · 1900s 7,082 · 1910s 3,495 · 1920s 446 · 1930s 1,180 ·
1940s 725 · 1950s 777 · 1960s 734 · 1970s 313 · 1980s 291 · 1990s 80 ·
2000s 31 · 2010s 8 (+591 undated). Coverage is dominated by the Buttolph
collecting era (1900–1924); normalize by menus-per-decade before claiming
trends, and treat pre-1880 / 1920s / post-1990 boundaries with care.

## Provenance and terms

- Data: NYPL [What's on the Menu?](https://www.nypl.org/research/support/whats-on-the-menu),
  published for open reuse; site/API retired January 2025, bulk exports remain
  on S3. Cite NYPL and the export date.
- Field docs: [Curating Menus data dictionary](http://www.curatingmenus.org/data_dictionary/).
- Underlying menus are overwhelmingly pre-1931 (public domain); dish names and
  prices are uncopyrightable facts. No personal data.
