# Version 10.1 — Graphic Fidelity Fix

The previous Line Art profile incorrectly treated broad black artwork as outline
ink, leaving only a few white spaces. Coverage repair then stretched those few
areas into giant polygons.

This release adds a separate `graphic_monochrome` profile for Noir Vinyl
Reverie and similar artwork. It preserves black and gray fills, record grooves,
metallic detail, and source tones, while reducing destructive region merging.

Rebuild Noir Vinyl Reverie after deployment. Existing generated packages still
contain the old segmentation and must be replaced.
