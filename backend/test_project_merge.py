"""BE/FE code is actually merged, and conflicting files block integration."""

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import project_agent


class ProjectMergeTest(unittest.TestCase):
    def test_missing_requirements_manifest_is_created_for_known_imports(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "cli.py").write_text("import argparse\nimport sys\n", encoding="utf-8")
            self.assertTrue(project_agent.ensure_python_dependency_manifest(root, ["requirements.txt"], "python"))
            self.assertIn("No third-party dependencies", (root / "requirements.txt").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_cli.py").write_text("import pytest\n", encoding="utf-8")
            self.assertTrue(project_agent.ensure_python_dependency_manifest(root, ["requirements.txt"], "python"))
            self.assertEqual((root / "requirements.txt").read_text(encoding="utf-8").strip(), "pytest")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "cli.py").write_text("import requests\n", encoding="utf-8")
            self.assertFalse(project_agent.ensure_python_dependency_manifest(root, ["requirements.txt"], "python"))
            self.assertFalse((root / "requirements.txt").exists())

    def test_plan_accepts_equivalent_dependency_and_test_filenames(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_cli.py").write_text("def test_cli(): assert True\n", encoding="utf-8")
            report = project_agent.static_check(root, ["pyproject.toml", "tests/test_cli_main.py"])
        check = next(item for item in report if item["label"] == "项目计划文件")
        self.assertTrue(check["ok"], check)

    def test_static_check_rejects_skipped_or_xfailed_tests(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_cli.py").write_text(
                "import pytest\ndef test_cli(): pytest.skip('not ready')\n", encoding="utf-8")
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "自动化测试无跳过")
        self.assertFalse(check["ok"], check)

    def test_static_check_rejects_incomplete_readme_setup_and_package_commands(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            package = root / "temperature_converter"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            (root / "README.md").write_text(
                "```sh\ncd temperature_converter\npython -m temperature_converter\n```\n",
                encoding="utf-8",
            )
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "README命令与依赖说明")
        self.assertFalse(check["ok"], check)
        self.assertIn("pip install -r requirements.txt", check["detail"])
        self.assertIn("项目根目录", check["detail"])

    def test_static_check_accepts_readme_install_and_root_module_command(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            package = root / "temperature_converter"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            (root / "README.md").write_text(
                "```sh\npython -m pip install -r requirements.txt\npython -m temperature_converter\n```\n",
                encoding="utf-8",
            )
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "README命令与依赖说明")
        self.assertTrue(check["ok"], check)

    def test_static_check_rejects_unittest_command_for_pytest_tests(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_cli.py").write_text("import pytest\ndef test_ok(): assert True\n", encoding="utf-8")
            (root / "README.md").write_text("Run `python -m unittest discover -s tests`.\n", encoding="utf-8")
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "README命令与依赖说明")
        self.assertFalse(check["ok"], check)
        self.assertIn("使用 pytest", check["detail"])

    def test_verbatim_merge_and_conflict(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            contributions = root / "contributions"
            (contributions / "T1").mkdir(parents=True)
            (contributions / "T2").mkdir()
            converter = contributions / "T1" / "converter.py"
            converter.write_text("def to_f(c):\n    return c * 9 / 5 + 32\n", encoding="utf-8")
            cli = contributions / "T2" / "cli.py"
            cli.write_text("from converter import to_f\nprint(to_f(25))\n", encoding="utf-8")
            merged, conflicts = project_agent.merge_contributions(contributions, root / "project")
            self.assertFalse(conflicts)
            self.assertEqual({item["path"] for item in merged}, {"converter.py", "cli.py"})
            self.assertEqual((root / "project" / "converter.py").read_bytes(), converter.read_bytes())
            self.assertEqual(merged[0]["sha256"], hashlib.sha256((root / "project" / merged[0]["path"]).read_bytes()).hexdigest())
            (contributions / "T2" / "converter.py").write_text("def to_f(c): return 0\n", encoding="utf-8")
            _, conflicts = project_agent.merge_contributions(contributions, root / "conflicted")
            self.assertEqual(conflicts[0]["path"], "converter.py")

    def test_temperature_project_merges_runs_and_reports_missing_sandbox(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            contributions = root / "contributions"
            backend = contributions / "T1"
            frontend = contributions / "T2"
            backend.mkdir(parents=True)
            frontend.mkdir()
            (backend / "temperature.py").write_text(
                "def convert(value, unit):\n"
                "    unit = unit.upper()\n"
                "    if unit == 'C': return value * 9 / 5 + 32, 'F'\n"
                "    if unit == 'F': return (value - 32) * 5 / 9, 'C'\n"
                "    raise ValueError('仅支持 C 或 F')\n",
                encoding="utf-8",
            )
            (frontend / "cli.py").write_text(
                "import sys\nfrom temperature import convert\n"
                "def main(args):\n"
                "    if len(args) != 2:\n"
                "        print('用法：python cli.py 数值 C|F', file=sys.stderr); return 2\n"
                "    try:\n"
                "        result, unit = convert(float(args[0]), args[1])\n"
                "    except ValueError as error:\n"
                "        print(f'输入错误：{error}', file=sys.stderr); return 2\n"
                "    print(f'{result:.1f} {unit}'); return 0\n"
                "if __name__ == '__main__': sys.exit(main(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            (frontend / "test_temperature.py").write_text(
                "import unittest\nfrom temperature import convert\n"
                "class TemperatureTest(unittest.TestCase):\n"
                "    def test_celsius_to_fahrenheit(self): self.assertEqual(convert(25, 'C'), (77.0, 'F'))\n"
                "    def test_fahrenheit_to_celsius(self): self.assertEqual(convert(77, 'F'), (25.0, 'C'))\n"
                "    def test_unsupported_unit(self):\n"
                "        with self.assertRaises(ValueError): convert(25, 'X')\n"
                "if __name__ == '__main__': unittest.main()\n",
                encoding="utf-8",
            )
            (frontend / "README.md").write_text(
                "# 温度换算\n\n运行：`python cli.py 25 C`。\n测试：`python -m unittest -v`。\n",
                encoding="utf-8",
            )
            project = root / "integrated"
            merged, conflicts = project_agent.merge_contributions(contributions, project)
            self.assertFalse(conflicts)
            self.assertEqual({item["path"] for item in merged},
                             {"temperature.py", "cli.py", "test_temperature.py", "README.md"})

            tests = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=project,
                                   capture_output=True, text=True, timeout=20)
            self.assertEqual(tests.returncode, 0, tests.stderr)
            expected = (([sys.executable, "cli.py", "25", "C"], "77.0 F"),
                        ([sys.executable, "cli.py", "77", "F"], "25.0 C"))
            for command, output in expected:
                result = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), output)
            for args in (("bad", "C"), ("25", "X")):
                result = subprocess.run([sys.executable, "cli.py", *args], cwd=project,
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("输入错误", result.stderr)

            report = project_agent.static_check(project, ["temperature.py", "cli.py", "README.md", "test_temperature.py"])
            self.assertTrue(all(item["ok"] for item in report), report)
            with patch.object(project_agent, "docker_available", return_value=False):
                evaluation = project_agent.evaluate(project, [], "python", execute=True)
            self.assertEqual(evaluation["status"], "unchecked")
            runner = next(item for item in evaluation["checks"] if item["label"] == "隔离安装与运行测试")
            self.assertIsNone(runner["ok"])


if __name__ == "__main__":
    unittest.main()
