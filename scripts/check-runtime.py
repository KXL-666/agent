"""Usage: python scripts/check-runtime.py [--json] [--require office docker cjk_font]."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from runtime_diagnostics import diagnose


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require", nargs="*", choices=("office", "docker", "cjk_font", "key_storage"), default=[])
    args = parser.parse_args()
    report = diagnose()
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for check in report["checks"]:
            print(f"[{check['status']}] {check['label']}: {check['detail']}")
            if check["status"] == "missing":
                print(f"  {check['remedy']}")
        print(report["notice"])
    required_ready = all(check["status"] == "ready" for check in report["checks"] if check["key"] in args.require)
    return 0 if report["core_ready"] and required_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
