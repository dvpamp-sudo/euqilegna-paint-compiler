from pathlib import Path

FILES = (
    "compiler.py",
    "compiler_core.py",
    "numbering_engine.py",
    "validation_engine.py",
    "pipeline_registry.py",
    "qa_engine.py",
    "main.py",
)

for filename in FILES:
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

from compiler import compile_artwork
from validation_engine import validate_compiled_records

assert callable(compile_artwork)
sample = [{
    "regionId": "region_1",
    "colorId": "1",
    "path": "M 0 0 L 1 0 L 1 1 Z",
    "label": {"fontSize": 8, "radius": 7},
}]
assert validate_compiled_records(sample, 1)["passed"]
print("OK: Version 11 modular facade and validation engine are operational.")
