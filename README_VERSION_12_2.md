# Version 12.2 — Fast Paintability Validation

Version 12.2 fixes the long pause at:
`Validating and repairing customer paintability`.

The old validator repeatedly performed full-canvas work for every region on
every repair pass. On detailed artwork with hundreds of regions this could take
30 minutes or more.

The new validator:
- validates each region inside a tight bounding-box crop
- avoids full-canvas neighbor-size scans
- performs cheap area/label checks during repair passes
- postpones expensive polygon/vector checks until the final validation
- reduces the maximum repair passes from 12 to 8
- preserves the final strict paintability validation before export

This is a performance optimization, not a removal of paintability safeguards.
