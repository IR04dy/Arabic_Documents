# Arabic Dictionary OCR Extractor

This project extracts Arabic name-and-meaning entries from the two scanned PDF dictionaries in this directory. It runs locally and retains source-page and OCR-confidence information for every meaning.

## Requirements

- Python 3.9+
- Poppler commands: `pdfinfo`, `pdftoppm`
- Tesseract 5 with Arabic language data (`ara`)
- macOS `sips` (used to crop the two-column dictionary)

On macOS, the OCR dependencies can be installed with:

```sh
brew install poppler tesseract tesseract-lang
```

## Run

```sh
python3 src/run_pipeline.py
```

The detailed database is written to `output/arabic_names.json`. A simplified export pairing every name with its gender and meanings is written to `output/arabic_names_with_meanings.json`. Convenience exports are also created as `male_names.json`, `female_names.json`, `unisex_names.json`, and `unknown_names.json`. Intermediate rendered pages and OCR responses are cached in `work/`, so reruns are substantially faster.

Useful options:

```sh
python3 src/run_pipeline.py --dpi 240 --workers 4 --output output/arabic_names.json
```

An alternate Tesseract model can be benchmarked with:

```sh
python3 src/run_pipeline.py --tessdata-dir models/tessdata
```

## Output model

Each unique name contains its normalized search form, observed spelling variants, a `gender` value (`male`, `female`, `unisex`, or `unknown`), and one or more meanings. Every meaning includes the source PDF, PDF page, column, OCR confidence, gender evidence, and a `needs_review` flag. Gender comes primarily from the dictionaries' own male, female, and shared-name sections; explicit wording such as "اسم مذكر" or "اسم مؤنث" takes precedence.

Names are deduplicated in two stages. The first merges exact normalized spellings across OCR passes and source books. The second conservatively merges differences in diacritics, initial alef/hamza spelling, and compound-name spacing. If two spellings occur as separate entries on the same source page, they remain separate to avoid collapsing genuinely different names such as `أزهار` and `إزهار`. All merged spellings remain available in the detailed record's `variants` array.

OCR is inherently imperfect, especially for uncommon proper names. Records marked for review should be compared with the cited source page before use in a production database.
