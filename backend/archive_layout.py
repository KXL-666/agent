"""可重复运行的旧产物归档迁移；保留历史文件与旧下载标识。"""
import os
import re
import shutil
from pathlib import Path

import forge
import storage


def migrate() -> int:
    count = 0
    with storage._lock, storage._conn() as conn:
        rows = conn.execute("SELECT id, sprint_id, kind, file_name, created_at FROM artifacts WHERE file_name IS NOT NULL").fetchall()
    for row in rows:
        name = row["file_name"]
        if not name or name.startswith("_process/") or re.match(r"^\d{4}-\d{2}-\d{2}/", name):
            continue
        source = forge.safe_deliverable_path(name)
        if not source:
            continue
        if row["kind"] in ("final", "custom"):
            requirement = (storage.get_sprint(row["sprint_id"]) or {}).get("requirement", "") if row["sprint_id"] else ""
            new_name = forge.publish_file(name, row["sprint_id"], row["created_at"], requirement)
        else:
            new_name = "_process/" + (name if name.replace("\\", "/").startswith("code/") else "legacy/" + name)
            dest = forge.output_path(new_name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not os.path.isfile(dest):
                shutil.copy2(source, dest)
        storage.update_artifact(row["id"], file_name=new_name)
        count += 1
    # 已更新引用后，再把旧文件移出交付区；不删除任何历史内容。
    for path in Path(forge.DELIVERABLES_DIR).rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(forge.DELIVERABLES_DIR).as_posix()
        if re.match(r"^\d{4}-\d{2}-\d{2}/", rel):
            continue
        dest = forge.output_path("_process/" + (rel if rel.startswith("code/") else "legacy/" + rel))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest):
            shutil.move(str(path), dest)
        elif path.read_bytes() == Path(dest).read_bytes():
            # 仅在副本逐字节一致时移除旧位置，源数据仍在过程目录内。
            path.unlink()
        else:
            raise RuntimeError(f"历史文件存在同名不同内容，保留原文件：{rel}")
    root = Path(forge.DELIVERABLES_DIR).resolve()
    for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir() and folder.resolve().is_relative_to(root):
            try:
                folder.rmdir()  # 仅移除空目录，绝不递归删除。
            except OSError:
                pass
    return count


if __name__ == "__main__":
    print(f"已更新 {migrate()} 条历史产物归档记录")
