# Euqilegna Paint Compiler 11 — Modular Foundation

Version 11 preserves the existing application and Render API while separating
critical responsibilities into focused modules.

## Structure

- `compiler.py` — stable public facade
- `compiler_core.py` — existing image-processing orchestration
- `artwork_profiles.py` — artwork classification and settings
- `numbering_engine.py` — safe interior number placement
- `validation_engine.py` — region, palette, path, and label validation
- `interaction_engine_v9.py` — mouse, touch, and Apple Pencil interaction
- `pipeline_registry.py` — profile-to-pipeline routing
- `qa_engine.py` — release-readiness summary
- `testing_runtime_v10.py` — persistent jobs and beta feedback

## Permanent publishing rule

A package is not allowed to finish when it contains duplicate IDs, invalid
palette references, empty SVG paths, or regions without usable number labels.
The compiler writes `validation_report.json` into every generated package.

## Installation

Extract all files into:

`C:\Users\dvpam\OneDrive\Documents\GitHub\euqilegna-paint-compiler`

Replace matching files, run `VERIFY_VERSION_11.bat`, start the server, remove
the old generated package, and rebuild the artwork.

The public import remains unchanged:

`from compiler import compile_artwork`
