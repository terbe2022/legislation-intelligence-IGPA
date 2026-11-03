"""Offline release validation. Never imports production modules or contacts services."""
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
failures = []
count = 0
secret = re.compile(r"\bsk-lf-[A-Za-z0-9_-]+|\bsk-[A-Za-z0-9_-]{16,}|[?&]sig=[^\s\"'<>]+|\bsig=[A-Za-z0-9%+/=_-]{15,}|AccountKey=[^;\s]+", re.I)
manifest = json.loads((ROOT / "docs/SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
for record in manifest["source_files"]:
    path = ROOT / record["path"]
    if not path.is_file():
        failures.append("Missing manifest file: " + record["path"])
    elif hashlib.sha256(path.read_bytes()).hexdigest() != record["release_sha256"]:
        failures.append("Content differs from release manifest: " + record["path"])
for path in ROOT.rglob("*"):
    if not path.is_file() or any(p in (".git", "__pycache__", ".venv", "venv") for p in path.parts):
        continue
    relative = path.relative_to(ROOT).as_posix()
    if path.name == "local.settings.json" or path.suffix in (".zip", ".tar", ".parquet", ".pkl", ".joblib"):
        failures.append("Excluded artifact present: " + relative)
    if path.suffix not in (".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".txt", ".example", ".js"):
        continue
    text = path.read_text(encoding="utf-8-sig")
    if path.name != "verify_release.py" and secret.search(text):
        failures.append("Embedded credential pattern: " + relative)
    if path.suffix == ".py":
        try:
            ast.parse(text, filename=relative)
            count += 1
        except SyntaxError as exc:
            failures.append("Syntax error: " + relative + ":" + str(exc.lineno))
    if path.suffix == ".md":
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in link and not link.startswith("#"):
                target = path.parent / link.split("#")[0]
                if not target.exists():
                    failures.append("Broken document link: " + relative + " -> " + link)
# Selected configuration's versioned prompt templates are runtime assets.
for config in (ROOT / "components/pipeline/configs").glob("*.example.yaml"):
    text = config.read_text(encoding="utf-8")
    for value in re.findall(r"(?m)^\s*(?:prompt_path|synthesis_prompt_path):\s*([^\s#]+)", text):
        value = value.strip("\"'")
        if not (ROOT / "components/pipeline" / value).is_file():
            failures.append("Missing configured prompt asset: " + value)
for module in ("webapp", "functions", "pipeline"):
    if not (ROOT / "components" / module / "requirements.txt").exists():
        failures.append("Missing captured component requirements: " + module)
print(json.dumps({"python_files_parsed": count,
                  "manifest_files_checked": len(manifest["source_files"]),
                  "production_modules_executed": 0, "failures": failures}, indent=2))
raise SystemExit(bool(failures))
