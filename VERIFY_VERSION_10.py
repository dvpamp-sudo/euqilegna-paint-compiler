from __future__ import annotations
import py_compile
from pathlib import Path

for filename in (
    "main.py",
    "compiler.py",
    "interaction_engine_v9.py",
    "artwork_profiles.py",
    "testing_runtime_v10.py",
):
    py_compile.compile(filename, doraise=True)
    print(f"OK: {filename}")

source = Path("main.py").read_text(encoding="utf-8")
for required in (
    "/beta-test",
    "/beta/admin-v10",
    "/api/runtime-status",
    "ThreadPoolExecutor(max_workers=1)",
    "RUNTIME_DB.save_job",
):
    assert required in source, required

print("OK: Version 10 permanent testing release is installed.")
