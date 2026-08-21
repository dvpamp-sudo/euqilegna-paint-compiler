from pathlib import Path
for filename in (
    "compiler.py","compiler_core.py","compiler_v12/orchestrator.py",
    "compiler_v12/validation.py","compiler_v12/recovery.py",
    "premium_catalog.py","main.py",
):
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

validation = Path("compiler_v12/validation.py").read_text(encoding="utf-8")
recovery = Path("compiler_v12/recovery.py").read_text(encoding="utf-8")
assert "Region count exploded beyond the usability budget" in validation
assert "Too many tiny paint regions for customer use" in validation
assert '"target_regions": min(int(requested.get("target_regions") or 480), 480)' in recovery
print("OK: Version 12.4 region-budget protection is installed.")
