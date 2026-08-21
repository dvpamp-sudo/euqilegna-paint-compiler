from pathlib import Path

FILES = (
    "main.py",
    "compiler.py",
    "compiler_core.py",
    "numbering_engine.py",
    "validation_engine.py",
    "compiler_v12/__init__.py",
    "compiler_v12/analyzer.py",
    "compiler_v12/models.py",
    "compiler_v12/recovery.py",
    "compiler_v12/validation.py",
    "compiler_v12/orchestrator.py",
)

for filename in FILES:
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

from compiler import compile_artwork
assert callable(compile_artwork)
print("OK: Version 12 public compiler facade is ready.")
