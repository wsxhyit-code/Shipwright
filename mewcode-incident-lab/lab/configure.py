"""Generate a Shipwright project overlay using its checked configuration schema."""
import argparse
import json
from pathlib import Path


def config(mode="patch", evidence_url="http://127.0.0.1:9082"):
    return {
        "permission_mode": "default",
        "enable_verification_agent": True,
        "toolset": {
            "ops": [{"kind": kind, "capability": capability, "base_url": evidence_url, "timeout": 5}
                    for kind, capability in [("alertmanager", "alerts"), ("loki", "logs"), ("prometheus", "metrics")]],
            "create_pr": {"enabled": True, "verify_command": "python -m lab.verify", "base_ref": "main",
                          "artifacts_dir": "runtime/delivery", "require_independent": True,
                          "mode": mode, "remote": "origin", "timeout": 120},
        },
    }


def rules():
    root = Path.cwd().resolve()
    allow = ["Bash(python -m lab.verify)", "Bash(python -m unittest discover -s tests -v)",
             "Bash(python -m unittest discover -s acceptance -v)",
             "Bash(git checkout -b agent/empty-orders)", "CreatePR(*)"]
    deny = []
    # Leading '*lab/*' also matches the parent directory incident-lab/app/.
    # Use anchored absolute and relative scopes, never wildcard the root prefix.
    for tool in ["EditFile", "WriteFile"]:
        for relative in ["app/orders.py", "tests/test_*.py"]:
            for path in [relative, str(root / relative)]:
                allow.append(f"{tool}({path})")
        for relative in ["acceptance/*", "lab/*", "docs/API_CONTRACT.md", ".mewcode/*"]:
            for path in [relative, str(root / relative)]:
                deny.append(f"{tool}({path})")
    return [{"rule": item, "effect": "allow"} for item in allow] + [{"rule": item, "effect": "deny"} for item in deny]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["patch", "push", "pr"], default="patch")
    parser.add_argument("--evidence-url", default="http://127.0.0.1:9082")
    args = parser.parse_args()
    folder = Path(".mewcode")
    folder.mkdir(exist_ok=True)
    targets = [folder / "config.local.yaml", folder / "permissions.local.yaml"]
    if any(path.exists() for path in targets):
        parser.error("Existing local config/permissions detected; merge manually rather than overwrite")
    # JSON is a YAML subset, accepted by the actual yaml.safe_load configuration loader.
    for path, value in zip(targets, [config(args.mode, args.evidence_url), rules()]):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Created project toolset overlay and scoped CLI permissions; model provider comes from your existing config")


if __name__ == "__main__":
    main()
