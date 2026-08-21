# Euqilegna Version 9 — Intelligent Artwork Profiles

This update fixes the foreground-detection problem across the Premium Library.

## Profiles

- Portrait
- Line Art
- Landscape
- Decorative
- Full-Frame Illustration
- Photo

The compiler automatically analyzes the artwork and selects a profile. The
official Premium Library also has explicit profile overrides.

## Full-frame rules

Line art, landscapes, decorative artwork, scenic compositions, and full-frame
illustrations do not require an isolated foreground subject. Auto-crop is
automatically disabled for those profiles.

## Recovery order

No foreground or segmentation stage is allowed to stop the build by itself.

1. Try the selected profile pipeline.
2. If foreground detection is missing or implausibly small, use the full canvas.
3. If line-art closure is insufficient, retry as full-frame illustration.
4. If illustration markers are insufficient, retry with full-canvas photo regions.
5. Continue to the Guaranteed Paintability and customer simulation quality gates.

## Premium assignments

- Midnight Jazz Muse — Portrait
- Noir Vinyl Reverie — Line Art
- Afrofuturist Stargazer — Portrait
- Golden Koi & Lotus — Decorative
- Rainy Café Nocturne — Landscape
- Celestial Botanical Portrait — Decorative
- Art Deco Peacock — Decorative
- Brownstone Jazz Evening — Landscape

## Installation

Copy the package into your current repository, run:

`VERIFY_INTELLIGENT_PROFILES.bat`

Test locally, rebuild the Premium artwork, then commit and push to GitHub.
Render will redeploy automatically.
