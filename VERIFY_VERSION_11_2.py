from pathlib import Path

for filename in (
    "compiler.py",
    "compiler_core.py",
    "interaction_engine_v9.py",
    "numbering_engine.py",
    "validation_engine.py",
    "main.py",
):
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

source = Path("compiler_core.py").read_text(encoding="utf-8")
assert "Every exported region is paintable" in source
assert "const paintableRegions=allRegions.filter" in source
assert "r=>document.getElementById(r.regionId)" in source
assert "number reference${resolved===1?' was':'s were'} restored" in source
assert "completed automatically so the painting can continue" not in source
print("OK: Version 11.2 complete-paintability enforcement is installed.")
