# Euqilegna Interactive Digital Art Studio Version 9

## What was implemented

Version 9 introduces a modular pointer and touch interaction engine in:

`interaction_engine_v9.py`

The image-processing compiler remains intact, while the fragile click behavior is replaced at generated-player build time.

## Pointer engine behavior

- Mouse, touch, Apple Pencil, and Windows pen use one Pointer Events path.
- Pointer coordinates are transformed into SVG coordinates using `getScreenCTM()`.
- The engine tests the actual SVG fill under the pointer.
- When regions overlap, the smallest matching region is selected first.
- Thin sections receive a small nearest-label accessibility tolerance.
- Pan and zoom gestures are not treated as paint taps.
- Synthetic clicks after pointer events are suppressed to prevent double painting.
- Region paths receive keyboard focus and accessible labels.
- The existing save, progress, hint, completion, and reveal functions remain in place.

## Syntax warning fix

The embedded HTML/JavaScript template is now a raw Python string, eliminating:

`SyntaxWarning: invalid escape sequence '\s'`

## Install into your current repository

Copy all files from this package into:

`C:\Users\dvpam\OneDrive\Documents\GitHub\euqilegna-paint-compiler`

Run:

`VERIFY_VERSION_9.bat`

Then start locally:

`C:\PythonEnvs\euqilegna\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000`

## Required test sequence

1. Rebuild a premium artwork. Old generated players do not contain Version 9.
2. Open the newly generated player.
3. Select a palette number.
4. Test mouse, touch, zoomed view, and—on iPad—Apple Pencil or finger.
5. Commit and push the new files to GitHub.
6. Render will redeploy automatically.

## GitHub commit suggestion

`Implement Version 9 pointer and touch interaction engine`
