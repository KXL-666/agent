"""Explicit runtime probes. No model calls, package installation or file writes."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


def probe(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace").strip()[:160]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def diagnose() -> dict:
    checks = []

    def add(key, label, status, detail, remedy=""):
        checks.append(dict(key=key, label=label, status=status, detail=detail, remedy=remedy))

    add("python", "Python", "ready" if sys.version_info >= (3, 10) else "missing", sys.version.split()[0],
        "安装 Python 3.10 或更高版本")
    modules = {"fastapi": "fastapi", "langgraph": "langgraph", "httpx": "httpx",
               "dotenv": "python-dotenv", "psutil": "psutil", "docx": "python-docx",
               "pptx": "python-pptx", "openpyxl": "openpyxl", "pypdf": "pypdf",
               "multipart": "python-multipart", "reportlab": "reportlab", "pymupdf": "PyMuPDF", "PIL": "Pillow"}
    if os.name != "nt":
        modules["keyring"] = "keyring"
    modules.update({"aiosqlite": "aiosqlite", "langgraph.checkpoint.sqlite": "langgraph-checkpoint-sqlite"})
    def available(module):
        try:
            return importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            return False
    missing = [package for module, package in modules.items() if not available(module)]
    add("python_packages", "Python 依赖", "missing" if missing else "ready",
        ", ".join(missing) if missing else "所需模块可发现（不代表原生库或外部服务可用）",
        "python -m pip install -r backend/requirements.txt")
    node = shutil.which("node")
    version = probe([node, "--version"]) if node else None
    try:
        major, minor = map(int, (version or "").lstrip("v").split(".")[:2])
        node_ok = (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major > 22
    except ValueError:
        node_ok = False
    add("node", "Node.js", "ready" if node_ok else "missing", version or "不可用", "建议安装 Node.js 24 LTS")
    import office_runtime
    office = office_runtime.find_office()
    office_version = probe([office, "--headless", "--version"]) if office else None
    add("office", "Office 渲染与公式重算", "ready" if office_version else "missing",
        office_version or "未发现可执行的 LibreOffice", "安装 LibreOffice，或设置 OFFICE_SOFFICE")
    fonts = [os.getenv("OFFICE_CJK_FONT", ""), "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simsun.ttc",
             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf", "/System/Library/Fonts/PingFang.ttc"]
    font = next((path for path in fonts if path and Path(path).is_file()), None)
    add("cjk_font", "中文字体文件", "ready" if font else "missing", font or "未发现已知中文字体",
        "安装中文字体；自定义 PDF 字体可设置 OFFICE_CJK_FONT。实际排版仍需渲染验收")
    docker = os.getenv("OFFICE_DOCKER_PATH") or shutil.which("docker")
    if not docker and os.name == "nt":
        bases = [os.getenv("ProgramFiles", "C:/Program Files"), os.getenv("LOCALAPPDATA", "")]
        docker = next((str(path) for base in bases if base
                       for path in (Path(base) / "Docker/Docker/resources/bin/docker.exe",
                                    Path(base) / "Docker/Docker/resources/docker.exe") if path.is_file()), None)
    docker_version = probe([docker, "info", "--format", "{{.ServerVersion}}"]) if docker else None
    add("docker", "Docker 服务", "ready" if docker_version else "missing",
        docker_version or "Docker 服务不可用，代码功能测试将保持未验证", "安装并启动 Docker Desktop / Docker Engine")
    if os.name == "nt":
        add("key_storage", "密钥安全存储", "ready", "Windows DPAPI；实际加密失败将拒绝保存，不回退明文")
    else:
        import secretbox
        try:
            backend = secretbox._keyring_backend()
            add("key_storage", "密钥安全存储", "ready", type(backend).__module__ + "（实际写入仍可能需要解锁）")
        except ValueError:
            add("key_storage", "密钥安全存储", "missing", "没有可用的原生系统密钥存储",
                "安装 keyring 并解锁 Keychain、Secret Service 或 KWallet；不可用时禁止保存新密钥")
    add("model", "模型连接", "unchecked", "本检查不发送模型请求，也不读取或输出密钥",
        "在成员配置中执行连接测试；会产生模型用量")
    core = all(item["status"] == "ready" for item in checks if item["key"] in ("python", "python_packages", "node"))
    return {"core_ready": core, "checks": checks,
            "notice": "ready 仅表示对应环境检查通过，不代表任何生成任务已验收；预算为事后估算，可能有在途调用超额。"}
