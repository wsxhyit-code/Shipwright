"""Serial task consumer. Durable task IDs; explicit execution and bounded runtime."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="runtime")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    runtime = Path(args.runtime).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "dispatcher.lock"
    try:
        lock = lock_path.open("x")
    except FileExistsError:
        parser.error("Dispatcher already running or stale lock exists; check process before removing lock")
    state_path = runtime / "dispatch-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    def save():
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(state_path)

    try:
        print("Waiting for code-fix candidates; non-code alerts are not dispatched", flush=True)
        while True:
            for task in sorted((runtime / "tasks").glob("*.md")):
                if task.stem in state:
                    continue
                incident = json.loads((runtime / "alerts" / (task.stem + ".json")).read_text(encoding="utf-8"))
                if incident["status"] != "firing" or not incident["code_fix_candidate"]:
                    continue
                if not args.execute:
                    print(json.dumps({"task": str(task), "status": "ready", "agent_executed": False}), flush=True)
                    continue
                state[task.stem] = {"status": "running", "started_at": time.time()}
                save()  # Crash after this point requires manual inspection, never silent rerun.
                output = runtime / "reports" / (task.stem + "-agent.txt")
                output.parent.mkdir(exist_ok=True)
                try:
                    with output.open("w", encoding="utf-8") as stream:
                        result = subprocess.run([args.python, "-m", "mewcode", "-p", task.read_text(encoding="utf-8")],
                                                stdout=stream, stderr=subprocess.STDOUT, timeout=args.timeout)
                    state[task.stem].update(status="agent_returned" if result.returncode == 0 else "execution_failed",
                                            exit_code=result.returncode)
                except subprocess.TimeoutExpired:
                    state[task.stem].update(status="timed_out", exit_code=124)
                except OSError as exc:
                    state[task.stem].update(status="execution_failed", error=str(exc))
                state[task.stem].update(finished_at=time.time(), report=str(output))
                save()
                # Exit 0 is not proof of a PR: user must inspect delivery/pr.json and Agent output.
                print(json.dumps({"task_id": task.stem, **state[task.stem]}, ensure_ascii=False), flush=True)
            if args.once or not args.execute:
                break
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        lock.close()
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
