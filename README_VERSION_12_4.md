# Version 12.4 — Region Budget Protection

Fixes the Noir Vinyl recovery build that produced 4,140 regions, including
3,422 tiny regions, despite an intended target of about 480 regions.

Version 12.4 rejects over-segmented candidates, rejects packages dominated by
tiny regions, caps monochrome recovery at 480 target regions, and raises the
minimum region area so the selected package remains usable for painting.
