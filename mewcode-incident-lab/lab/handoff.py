"""Invoke the existing mewcode CLI. Does not fake Agent work or bypass CreatePR."""
import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--task")
    parser.add_argument("--python", default=sys.executable, help="Python environment where mewcode is installed")
    parser.add_argument("--module", default="mewcode")
    parser.add_argument("--execute", action="store_true", help="Actually invoke mewcode; otherwise show the task")
    args = parser.parse_args()
    runtime = Path(args.runtime).resolve()
    task = Path(args.task) if args.task else Path((runtime / "latest-task.txt").read_text(encoding="utf-8").strip())
    prompt = task.read_text(encoding="utf-8")
    if not args.execute:
        print(prompt)
        return
    # Same public CLI the user described; internal ops/config schemas are not guessed.
    raise SystemExit(subprocess.run([args.python, "-m", args.module, "-p", prompt]).returncode)


if __name__ == "__main__":
    main()
