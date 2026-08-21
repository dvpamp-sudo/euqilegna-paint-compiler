from pathlib import Path

for filename in (
    "main.py",
    "testing_runtime_v10.py",
    "compiler.py",
    "compiler_core.py",
    "compiler_v12/orchestrator.py",
):
    source = Path(filename).read_text(encoding="utf-8")
    compile(source, filename, "exec")
    print("OK:", filename)

main = Path("main.py").read_text(encoding="utf-8")
runtime = Path("testing_runtime_v10.py").read_text(encoding="utf-8")

assert "def get_job_record" in main
assert "RUNTIME_DB.load_job(job_id)" in main
assert "RUNTIME_DB.save_job(job)" in main
assert 'def runtime_job_diagnostic' in main
assert "def load_job" in runtime
assert "public = dict(job)" in runtime

print("OK: Version 12.5 persistent Render job storage is installed.")
