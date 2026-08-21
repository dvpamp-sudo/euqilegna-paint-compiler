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

source = Path("compiler_v12/orchestrator.py").read_text(encoding="utf-8")
assert "def forward_progress" in source
assert "progress_callback=forward_progress" in source
assert "innerPercent" in source
print("OK: Version 12.1 progress forwarding is installed.")
