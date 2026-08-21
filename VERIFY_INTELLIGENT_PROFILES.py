from __future__ import annotations

import py_compile
from pathlib import Path

for filename in (
    "main.py",
    "compiler.py",
    "interaction_engine_v9.py",
    "artwork_profiles.py",
    "premium_catalog.py",
):
    py_compile.compile(filename, doraise=True)
    print(f"OK: {filename}")

source = Path("compiler.py").read_text(encoding="utf-8")
assert "safe_foreground_mask" in source
assert "infer_profile" in source
assert "automaticRecoveryEnabled" in source
assert "Foreground could not be detected." not in source
print("OK: Intelligent profiles and non-fatal foreground recovery are installed.")
