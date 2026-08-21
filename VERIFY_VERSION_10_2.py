from pathlib import Path
for filename in ("main.py","compiler.py","artwork_profiles.py","interaction_engine_v9.py","testing_runtime_v10.py"):
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

source = Path("compiler.py").read_text(encoding="utf-8")
assert 'id="paintAgainBtn"' in source
assert "function resetArtworkForRepaint()" in source
assert "let numberFocus=false;" in source
assert "All unpainted number references are visible." in source
print("OK: Version 10.2 repaint and number-reference behavior is installed.")
