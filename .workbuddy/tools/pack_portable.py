"""把 dist/EDMS/ 打包成发布用的绿色便携版 zip。

    .venv/Scripts/python.exe .workbuddy/tools/pack_portable.py [版本号]

产物：`dist/EDMS_Portable_<版本号>.zip`（默认 v1.0.0），解压后结构：

    EDMS/
    ├── EDMS.exe            启动器（打包前自动恢复正名，防手动改名带进包）
    └── _internal/          全部运行时

**排除** data/ 与 log/（首次运行会自动生成）。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "v1.0.0"
    src = ROOT / "dist" / "EDMS"
    if not src.is_dir():
        print(f"找不到 {src} —— 先跑打包命令生成 dist/EDMS/")
        return 1

    # 启动器正名：如果是被手动改名过的文件（比如 xx.exe 之外的 *.exe），先改回来
    target = src / "EDMS.exe"
    if not target.exists():
        candidates = sorted(src.glob("*.exe"))
        if not candidates:
            print(f"{src} 里没有 .exe 启动器，目录不完整")
            return 1
        candidates[0].rename(target)
        print(f"启动器已恢复正名：{candidates[0].name} -> EDMS.exe")

    internal = src / "_internal"
    if not internal.is_dir():
        print("找不到 _internal/ —— 打包目录不完整（onedir 需要它）")
        return 1

    out = ROOT / "dist" / f"EDMS_Portable_{version}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.write(target, "EDMS/EDMS.exe")
        count += 1
        for p in internal.rglob("*"):
            if p.is_file():
                z.write(p, "EDMS/" + p.relative_to(src).as_posix())
                count += 1

    mb = out.stat().st_size / 1024 / 1024
    print(f"已生成 {out}")
    print(f"  {count} 个文件，{mb:.1f} MB")
    print("  （不含 data/ 与 log/，首次运行自动生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
