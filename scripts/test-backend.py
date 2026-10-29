"""Run offline tests without reading the user's model configuration or usage files."""
import os
import argparse
from pathlib import Path
import sys
import tempfile
import unittest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-office", action="store_true", help="Include real LibreOffice conversion; requires a working renderer")
    parser.add_argument("--pattern", default="test_*.py", help="Select test modules while keeping database/config isolation")
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend))
    with tempfile.TemporaryDirectory(prefix="office-offline-tests-") as directory:
        os.environ["OFFICE_DB_PATH"] = str(Path(directory) / "office.db")
        os.environ["OFFICE_AGENT_CONFIG_PATH"] = str(Path(directory) / "agents.json")
        os.environ["OFFICE_USAGE_PATH"] = str(Path(directory) / "usage.json")
        # Import only after selecting the isolated database. Some legacy tests
        # also replace DB_PATH; none may fall back to the real office.db.
        import agent_config
        import forge
        import llm
        import llm_usage
        import skills
        import office_runtime
        from contextlib import ExitStack
        from unittest.mock import patch

        with (ExitStack() as optional,
              patch.object(agent_config, "CONFIG_PATH", str(Path(directory) / "agents.json")),
              patch.object(skills, "STORE_PATH", Path(directory) / "skills.json"),
              patch.object(llm_usage, "USAGE_FILE", str(Path(directory) / "usage.json")),
              patch.object(llm, "USAGE_FILE", str(Path(directory) / "usage.json"))):
            llm._usage.clear()
            if args.with_office and not office_runtime.find_office():
                parser.error("--with-office 需要 LibreOffice，请先运行 npm run doctor")
            if not args.with_office:
                optional.enter_context(patch.object(office_runtime, "find_office", return_value=None))
            # File-generating tests must not leave drafts among user artifacts.
            forge.DELIVERABLES_DIR = str(Path(directory) / "deliverables")
            forge.WORKSPACE_DIR = str(Path(directory) / "workspace")
            forge.CODE_ROOT = str(Path(directory) / "workspace/code")
            forge.CODE_DIR = forge.CODE_ROOT
            Path(forge.DELIVERABLES_DIR).mkdir()
            Path(forge.CODE_DIR).mkdir(parents=True)
            suite = unittest.defaultTestLoader.discover(str(backend), pattern=args.pattern)
            if not suite.countTestCases():
                parser.error("没有匹配的测试模块，未执行任何检查")
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
