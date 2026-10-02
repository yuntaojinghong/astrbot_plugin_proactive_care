"""把插件打包成可直接安装的压缩包。

用法::

    python scripts/build_release.py

产出（写入 ``dist/``）：

- ``astrbot_plugin_proactive_care.zip``
- ``astrbot_plugin_proactive_care.tar.gz``

压缩包外层套一层同名目录，解压后直接丢进 ``AstrBot/data/plugins/`` 即可，
符合 AstrBot 的安装习惯。
"""

from __future__ import annotations

import os
import tarfile
import zipfile

PKG_NAME = "astrbot_plugin_proactive_care"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(REPO_ROOT, "dist")

# 开发期文件不进分发包
EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".github",
    ".idea",
    ".vscode",
    "dist",
    "scripts",
    "tests",
    "docs",  # GitHub Pages 站点，插件运行不需要
    ".ruff_cache",
    ".pytest_cache",
    ".test_data",
}
EXCLUDE_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    "requirements-dev.txt",
    "logo.svg",  # 矢量源文件，分发 PNG 即可
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".tmp", ".db", ".db-journal", ".db-wal")


def should_skip(name: str) -> bool:
    if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
        return True
    return name.endswith(EXCLUDE_SUFFIX)


def walk_files() -> list[str]:
    collected: list[str] = []
    for current, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if not should_skip(d)]
        for name in files:
            if should_skip(name):
                continue
            absolute = os.path.join(current, name)
            collected.append(os.path.relpath(absolute, REPO_ROOT))
    return sorted(collected)


def build_zip(files: list[str]) -> str:
    target = os.path.join(DIST, f"{PKG_NAME}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            archive.write(os.path.join(REPO_ROOT, relative), os.path.join(PKG_NAME, relative))
    return target


def build_tar(files: list[str]) -> str:
    target = os.path.join(DIST, f"{PKG_NAME}.tar.gz")
    with tarfile.open(target, "w:gz") as archive:
        for relative in files:
            archive.add(
                os.path.join(REPO_ROOT, relative),
                arcname=os.path.join(PKG_NAME, relative),
            )
    return target


def main() -> None:
    os.makedirs(DIST, exist_ok=True)
    files = walk_files()

    # 最低限度的自检：缺少这些文件说明打包一定会出问题
    required = {"metadata.yaml", "main.py", "_conf_schema.json", "logo.png"}
    missing = required - set(files)
    if missing:
        raise SystemExit(f"打包中止，缺少必需文件: {sorted(missing)}")

    zip_path = build_zip(files)
    tar_path = build_tar(files)

    print(f"已打包 {len(files)} 个文件：")
    print(f"  {zip_path}")
    print(f"  {tar_path}")


if __name__ == "__main__":
    main()
