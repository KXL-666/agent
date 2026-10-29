"""BE/FE code is actually merged, and conflicting files block integration."""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import project_agent


class DockerDiscoveryTest(unittest.TestCase):
    def test_falls_back_to_env_override_and_windows_install_dir(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            fake_cli = Path(directory) / "docker.exe"
            fake_cli.write_text("", encoding="utf-8")
            with patch.object(project_agent.shutil, "which", return_value=None), \
                 patch.dict("os.environ", {"OFFICE_DOCKER_PATH": str(fake_cli)}):
                self.assertEqual(project_agent.docker_executable(), str(fake_cli))
            with patch.object(project_agent.shutil, "which", return_value=None), \
                 patch.dict("os.environ", {"OFFICE_DOCKER_PATH": str(fake_cli.parent / "missing.exe")}):
                with patch.object(project_agent, "os", SimpleNamespace(name="nt", getenv=os.getenv)), \
                     patch.dict("os.environ", {"ProgramFiles": directory, "ProgramW6432": directory}):
                    install = Path(directory) / "Docker" / "Docker" / "resources" / "bin"
                    install.mkdir(parents=True)
                    (install / "docker.exe").write_text("", encoding="utf-8")
                    self.assertEqual(project_agent.docker_executable(), str(install / "docker.exe"))
                    (install / "docker.exe").unlink()
                    self.assertIsNone(project_agent.docker_executable())
        with patch.object(project_agent.shutil, "which", return_value="/usr/bin/docker"):
            self.assertEqual(project_agent.docker_executable(), "/usr/bin/docker")

    def test_docker_available_uses_discovered_executable(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command[0])
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(project_agent, "docker_executable", return_value=r"C:\Docker\docker.exe"), \
             patch.object(project_agent.subprocess, "run", side_effect=fake_run):
            self.assertTrue(project_agent.docker_available())
        self.assertEqual(calls, [r"C:\Docker\docker.exe"])


class ProjectMergeTest(unittest.TestCase):
    def test_pytest_imports_feed_sandbox_requirements_and_declaration_gate(self):
        # C1 Sprint S1792907192082：测试 import pytest 但无任何依赖清单，
        # 沙箱退回 unittest 后 import 失败被误判为测试错误；README 也无法复现。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "conftest.py").write_text("import pytest\n", encoding="utf-8")
            (root / "tests" / "test_cli.py").write_text("def test_cli(): assert True\n", encoding="utf-8")
            (root / "cli.py").write_text("import sys\n", encoding="utf-8")
            (root / "README.md").write_text("# CLI\n\n运行：`python cli.py 25 C`。\n测试：`python -m pytest -q`。\n",
                                            encoding="utf-8")
            self.assertIn("pytest", project_agent.declared_python_test_requirements(root))
            report = {c["label"]: c for c in project_agent.static_check(root)}
            self.assertFalse(report["pytest 依赖声明"]["ok"])
            (root / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
            report = {c["label"]: c for c in project_agent.static_check(root)}
            self.assertTrue(report["pytest 依赖声明"]["ok"], report["pytest 依赖声明"]["detail"])
            self.assertIn("pytest", project_agent.declared_python_test_requirements(root))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "test_core.py").write_text("import unittest\n", encoding="utf-8")
            self.assertNotIn("pytest", project_agent.declared_python_test_requirements(root))
            labels = [c["label"] for c in project_agent.static_check(root)]
            self.assertNotIn("pytest 依赖声明", labels)

    def test_static_check_blocks_module_package_name_shadowing(self):
        # C1 Sprint S1792902305434：顶层模块与同名包并存、根目录 __init__.py 相对导入。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "temperature_converter.py").write_text("def convert(v, u): return v\n", encoding="utf-8")
            package = root / "temperature_converter"
            package.mkdir()
            (package / "__init__.py").write_text("from .core import convert\n", encoding="utf-8")
            (package / "core.py").write_text("def convert(v, u): return v\n", encoding="utf-8")
            (root / "__init__.py").write_text("from .temperature_converter import convert\n", encoding="utf-8")
            (root / "README.md").write_text("# 温度换算\n\n运行：`python -m temperature_converter 25 C`。\n",
                                            encoding="utf-8")
            (root / "test_core.py").write_text("def test_convert(): assert True\n", encoding="utf-8")
            report = {c["label"]: c for c in project_agent.static_check(root)}
            naming = report["模块与包命名"]
            self.assertFalse(naming["ok"])
            self.assertIn("temperature_converter.py", naming["detail"])
            self.assertIn("__init__.py", naming["detail"])
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            package = root / "temperature_converter"
            package.mkdir()
            (package / "__init__.py").write_text("from .core import convert\n", encoding="utf-8")
            (package / "core.py").write_text("def convert(v, u): return v\n", encoding="utf-8")
            (root / "README.md").write_text("# 温度换算\n\n运行：`python -m temperature_converter 25 C`。\n",
                                            encoding="utf-8")
            (root / "test_core.py").write_text("def test_convert(): assert True\n", encoding="utf-8")
            report = {c["label"]: c for c in project_agent.static_check(root)}
            self.assertTrue(report["模块与包命名"]["ok"], report["模块与包命名"]["detail"])

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

    def test_test_docstring_todo_does_not_claim_runtime_is_incomplete(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_cli.py").write_text(
                '"""TODO: update entrypoint if it ever changes."""\n'
                'def test_result(): assert 25 * 9 / 5 + 32 == 77\n', encoding="utf-8")
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "未完成的实现")
            self.assertTrue(check["ok"], check)

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

    def test_literal_setup_test_extra_is_available_to_container_installer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "setup.py").write_text(
                "from setuptools import setup\nsetup(name='demo', extras_require={'test': ['pytest>=7']})\n",
                encoding="utf-8",
            )
            self.assertEqual(project_agent.declared_python_test_requirements(root), ["pytest>=7"])

    def test_dev_test_dependency_and_readme_are_reconciled(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            (root / "tests" / "test_cli.py").write_text("import pytest\ndef test_ok(): assert 2 == 2\n", encoding="utf-8")
            (root / "requirements-dev.txt").write_text("# tests\npytest>=7.0\n", encoding="utf-8")
            (root / "README.md").write_text(
                "The test suite uses only the standard library's `unittest`, so no extra packages are required.\n"
                "```bash\npython -m unittest discover -s tests -v\n```\n", encoding="utf-8")
            self.assertEqual(project_agent.declared_python_test_requirements(root), ["pytest>=7.0"])
            self.assertTrue(project_agent.reconcile_python_test_docs(root))
            readme = (root / "README.md").read_text(encoding="utf-8")
            self.assertIn("pip install -r requirements-dev.txt", readme)
            self.assertIn("python -m pytest -q", readme)
            self.assertNotIn("python -m unittest", readme)

    def test_static_check_rejects_undocumented_bare_command_entrypoint(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "README.md").write_text("```bash\ntemp-convert \"25 C\"\n```\n", encoding="utf-8")
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "README命令与依赖说明")
            self.assertFalse(check["ok"], check)
            self.assertIn("temp-convert", check["detail"])

    def test_readme_output_text_is_not_treated_as_bare_command(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "```bash\n$ python -m temperature.cli 25 K\n"
                "Error: unsupported unit K\nSupported units: C or F\n```\n",
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

    def test_static_check_rejects_cli_test_module_that_differs_from_readme(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            tests = root / "tests"
            tests.mkdir()
            (root / "README.md").write_text(
                "运行 `python -m temperature.cli \"25 C\"`。\n测试 `python -m pytest -q`。\n",
                encoding="utf-8",
            )
            (tests / "test_cli.py").write_text(
                'CLI_MODULE = "temperature_cli"\ndef test_entrypoint(): assert True\n', encoding="utf-8")
            check = next(item for item in project_agent.static_check(root, [])
                         if item["label"] == "README命令与依赖说明")
        self.assertFalse(check["ok"], check)
        self.assertIn("temperature_cli", check["detail"])
        self.assertIn("temperature.cli", check["detail"])

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
