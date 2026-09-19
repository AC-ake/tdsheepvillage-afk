"""一键打包发行版：调用 PyInstaller 打包成单文件 exe，组装发行文件夹并压缩成 zip。

用法：双击 build_exe.bat，或在本目录执行 python make_release.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "tdsheepvillage-afk1.2"
DIST = ROOT / "dist"
RELEASE_DIR = DIST / "release" / APP_NAME
FOLDERS = ("templates", "图像")
DOCS = ("README.md", "更新日志.md", "发行说明-1.2.md")


def build() -> None:
    """按 spec 文件打包成单文件 exe（打包参数都写在 spec 里，不会走偏）。"""
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", f"{APP_NAME}.spec"],
        cwd=ROOT, check=True,
    )


def collect() -> None:
    """把 exe、识图模板、图片文件夹和文档收进发行文件夹。"""
    if RELEASE_DIR.exists(): shutil.rmtree(RELEASE_DIR)
    RELEASE_DIR.mkdir(parents=True)
    shutil.copy2(DIST / f"{APP_NAME}.exe", RELEASE_DIR / f"{APP_NAME}.exe")
    for name in FOLDERS:
        if (ROOT / name).is_dir(): shutil.copytree(ROOT / name, RELEASE_DIR / name)
    for name in DOCS:
        if (ROOT / name).is_file(): shutil.copy2(ROOT / name, RELEASE_DIR / name)


def pack() -> Path:
    """压缩成可以直接上传 GitHub 发行版的 zip。"""
    target = DIST / f"{APP_NAME}.zip"
    if target.exists(): target.unlink()
    shutil.make_archive(str(DIST / APP_NAME), "zip", DIST / "release", APP_NAME)
    return target


def main() -> None:
    print("[1/3] 打包 exe ...")
    build()
    print("[2/3] 组装发行文件夹 ...")
    collect()
    print("[3/3] 压缩成 zip ...")
    target = pack()
    print(f"完成：{target}")
    print(f"发行文件夹：{RELEASE_DIR}")


if __name__ == "__main__":
    main()
