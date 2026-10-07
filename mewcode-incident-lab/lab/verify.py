"""Mechanical gates only. Independent model verification belongs to mewcode."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    return {"command": command, "exit_code": result.returncode, "output": result.stdout + result.stderr}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="runtime")
    args = parser.parse_args()
    runtime = Path(args.runtime)
    (runtime / "reports").mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path("acceptance/integrity.json").read_text(encoding="utf-8"))
    integrity_ok = all(Path(name).is_file() and hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
                       for name, digest in manifest.items())
    results = [{"command": ["fixed-evaluator-integrity"], "exit_code": 0 if integrity_ok else 1,
                "output": "Fixed evaluator intact" if integrity_ok else "Fixed evaluator or contract was changed"}]
    if integrity_ok:
        results.append(run([sys.executable, "-m", "compileall", "-q", "app"]))
        if results[-1]["exit_code"] == 0:
            results.append(run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]))
        if results[-1]["exit_code"] == 0:
            results.append(run([sys.executable, "-m", "unittest", "discover", "-s", "acceptance", "-v"]))
    passed = all(item["exit_code"] == 0 for item in results)
    report = {"mechanical_verification": "PASS" if passed else "FAIL", "independent_agent_verification": "NOT_RUN", "gates": results}
    (runtime / "reports" / "mechanical-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = "# Mechanical verification\n\n" + f"Result: {'PASS' if passed else 'FAIL'}\n\nIndependent Agent verification: NOT_RUN\n\n"
    for item in results:
        text += f"## {' '.join(item['command'])}\n\nExit code: {item['exit_code']}\n\n```text\n{item['output']}\n```\n\n"
    (runtime / "reports" / "verification.md").write_text(text, encoding="utf-8")
    print(json.dumps({"mechanical_verification": report["mechanical_verification"], "independent_agent_verification": "NOT_RUN"}))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
