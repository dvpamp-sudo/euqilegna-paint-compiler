# Euqilegna Interactive Digital Art Studio 9.0
## Guaranteed Paintability Engine

Version 9 moves paintability validation out of the browser and into the compiler.

## New compiler pipeline

1. Generate candidate regions
2. Fill orphan pixels
3. Split every disconnected island into its own region
4. Validate each mask before palette assignment
5. Merge invalid or unpaintable fragments into the neighbor sharing the largest border
6. Repeat repair until every region passes
7. Assign final colors only after repair
8. Build SVG paths, numbers, and enlarged invisible hit targets
9. Run a complete automated customer simulation
10. Export only after a 100% pass

## Every exported region must have

- one connected pixel mask
- meaningful visible area
- a true interior number position
- enough interior radius for a label
- a valid SVG path
- a matching palette color
- one matching SVG number
- an interactive hit target
- successful simulated painting

## Quality reports

Every successful package contains:

- `paintability_engine_report.json`
- `paintability_simulation.json`

If any final region fails, compilation stops and the package is not released.

## Important

Previously compiled projects must be rebuilt. Existing generated SVG files were
created before compiler-side validation and cannot gain the guarantee through a
browser refresh.
