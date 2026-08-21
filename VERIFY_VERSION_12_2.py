from pathlib import Path

for filename in (
    "compiler.py",
    "compiler_core.py",
    "compiler_v12/orchestrator.py",
    "main.py",
):
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

source = Path("compiler_core.py").read_text(encoding="utf-8")
assert "def _cropped_region_mask" in source
assert "def _best_neighbor_for_merge_fast" in source
assert "Guaranteed Paintability Engine 12.2 Fast Crop Validator" in source
assert "max_passes: int = 8" in source
print("OK: Version 12.2 fast paintability validator is installed.")
