# Euqilegna Paint Compiler 12 — Validation-First Recovery Compiler

Version 12 is designed as a drop-in replacement for the existing repository.
`main.py` does not need a new API path.

## What changes

1. Artwork is analyzed before compilation.
2. The normal Smart Auto build is attempted first.
3. If the result fails structural or visual QA, bounded recovery strategies run.
4. Every candidate package is validated for:
   - required output files
   - paintable regions
   - palette existence
   - region validation
   - embedded player data
   - Paint Again / Canvas Lock controls
   - visual similarity to the source artwork
5. Only a package that passes validation is copied to the final output directory.
6. Failed attempts are reported instead of silently publishing a broken painting.

## Drop-in installation

Extract the ZIP directly into:

C:\Users\dvpam\OneDrive\Documents\GitHub\euqilegna-paint-compiler

Replace matching files and folders.

Run:

C:\PythonEnvs\euqilegna\Scripts\python.exe -B VERIFY_VERSION_12.py

Then restart the server and rebuild previously generated Premium artwork.

## Publishing behavior

The public import remains:

from compiler import compile_artwork

So the existing FastAPI app, GitHub deployment, Render service, and Base44
integration can keep using the same compiler endpoint.
