from pathlib import Path
import py_compile
for name in ("main.py","compiler.py","artwork_profiles.py","interaction_engine_v9.py","testing_runtime_v10.py"):
    py_compile.compile(name, doraise=True)
    print("OK:", name)
assert '"graphic_monochrome"' in Path("artwork_profiles.py").read_text(encoding="utf-8")
assert "preprocess_graphic_monochrome" in Path("compiler.py").read_text(encoding="utf-8")
print("OK: Version 10.1 graphic fidelity fix installed.")
