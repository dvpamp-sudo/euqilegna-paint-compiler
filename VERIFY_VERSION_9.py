from __future__ import annotations

from pathlib import Path
import py_compile

FILES = (
    "main.py",
    "compiler.py",
    "interaction_engine_v9.py",
)

for filename in FILES:
    py_compile.compile(filename, doraise=True)
    print(f"OK: {filename}")

source = Path("compiler.py").read_text(encoding="utf-8")
assert "build_pointer_engine_script" in source
assert 'html = r"""<!doctype html>' in source
print("OK: Version 9 interaction engine is wired into generated players.")
