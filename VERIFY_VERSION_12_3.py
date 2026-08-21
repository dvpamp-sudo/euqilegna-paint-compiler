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

core = Path("compiler_core.py").read_text(encoding="utf-8")
orch = Path("compiler_v12/orchestrator.py").read_text(encoding="utf-8")
assert 'validationPassed": validation_report["passed"]' not in core
assert "fallbackStrategy" in core
assert "if index >= 2 and best is None" in orch
print("OK: Version 12.3 validation recovery and runtime guard are installed.")
