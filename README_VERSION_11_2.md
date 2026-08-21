# Version 11.2 — Complete Paintability Enforcement

This release fixes visible artwork sections that appeared in the image but were
not offered as paintable regions.

## New rules

- Every exported SVG region is counted as paintable.
- Regions are never silently excluded because a number is difficult to place.
- Missing number references are repaired instead of automatically painted.
- Curved bands, crescents, rings, record highlights, and narrow regions receive
  a denser interior-point search.
- If browser hit testing cannot confirm a point, the compiler-provided interior
  label point remains available as a fallback.
- Completion cannot occur until every rendered region has been painted.
- Palette totals include every rendered region.

Existing generated artwork must be deleted and rebuilt because the interactive
behavior is stored inside each generated package.
