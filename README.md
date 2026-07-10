# Euqilegna Paint Compiler — Phase 1

Cleaner paint-by-number conversion using SLIC superpixels, region merging, smoothed SVG paths, readable labels, and Base44-ready outputs.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`.

## Outputs
- paintMap.svg
- paintMap_colored.svg
- regions.json
- palette.json
- preview.png
- metadata.json

Recommended settings for detailed AI artwork: 20–24 colors, balanced detail, min region area 220, merge strength 28, outline width 0.5.
