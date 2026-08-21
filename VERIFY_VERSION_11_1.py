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
assert 'id="canvasLockBtn"' in source
assert "let canvasLocked=true;" in source
assert "Canvas locked — tapping paints without moving the artwork." in source
assert "event.button===1 || spacePanActive" in source
print("OK: Version 11.1 canvas lock is installed.")
