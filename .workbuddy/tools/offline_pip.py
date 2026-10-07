"""pip 连不上源时的离线装包工具。

**背景**：某些环境里 `pip install X` 会一律报
`Could not find a version that satisfies the requirement X (from versions: none)`，
但同机 `curl` / `urllib.request` 访问 PyPI 却返回 200 —— 网络是通的，
是 pip 自己的 HTTP/TLS 栈出了问题（典型原因是安全软件的 HTTPS 中间人拦截）。
`--trusted-host`、`--index-url` 都试过，救不回来。

**绕法**：把这个流程拆开自己走一遍 ——
    1. 抓 simple 索引页
    2. 用 packaging 的标签系统挑出与当前解释器兼容的最新 wheel
    3. 下载到 .wheels/
    4. wheel 本质是个 zip，读里面的 METADATA 解析 Requires-Dist，递归拉依赖
    5. 最后 `pip install --no-index --find-links .wheels`
       —— 这一步 pip 完全不碰网络，只解压本地文件，所以能正常工作

**用法**：
    python .workbuddy/tools/offline_pip.py pyinstaller
    python .workbuddy/tools/offline_pip.py --dry-run pyinstaller
    python .workbuddy/tools/offline_pip.py --index https://pypi.org/simple requests
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename
from packaging.version import InvalidVersion, Version

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WHEEL_DIR = PROJECT_ROOT / ".wheels"
DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

HREF_RE = re.compile(r'href="([^"#]+)(?:#[^"]*)?"')


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "offline-pip/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def is_installed(name: str, specifier) -> bool:
    """已经装了且版本满足要求，就不用再拉。"""
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return False
    if not specifier:
        return True
    try:
        return specifier.contains(Version(dist.version), prereleases=True)
    except InvalidVersion:
        return True


def pick_wheel(index_url: str, name: str, specifier) -> tuple[str, str] | None:
    """在索引页里挑出最合适的 wheel，返回 (下载地址, 文件名)。"""
    url = f"{index_url.rstrip('/')}/{name}/"
    try:
        html = fetch(url).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    compatible = set(sys_tags())
    best: tuple[Version, str, str] | None = None

    for match in HREF_RE.finditer(html):
        href = match.group(1)
        filename = href.rsplit("/", 1)[-1]
        if not filename.endswith(".whl"):
            continue
        try:
            _, version, _, tags = parse_wheel_filename(filename)
        except Exception:  # noqa: BLE001 - 文件名不合规就跳过
            continue
        if not (tags & compatible):
            continue
        if specifier and not specifier.contains(version, prereleases=True):
            continue
        if best is None or version > best[0]:
            best = (version, urllib.parse.urljoin(url, href), filename)

    if best is None:
        return None
    return best[1], best[2]


def wheel_dependencies(wheel_path: Path, parent: str) -> list[tuple[Requirement, str]]:
    """读 wheel 里的 METADATA，取出适用于当前环境的 Requires-Dist。"""
    out: list[tuple[Requirement, str]] = []
    with zipfile.ZipFile(wheel_path) as zf:
        meta_name = next(
            (n for n in zf.namelist()
             if n.endswith(".dist-info/METADATA") and n.count("/") == 1),
            None,
        )
        if meta_name is None:
            return out
        meta = BytesParser().parsebytes(zf.read(meta_name))

    for raw in meta.get_all("Requires-Dist") or []:
        try:
            req = Requirement(raw)
        except Exception:  # noqa: BLE001
            continue
        # extra 依赖（测试、文档之类）不要
        if req.marker is not None and not req.marker.evaluate({"extra": ""}):
            continue
        # 带 extra 标记的会被上面过滤掉；这里再挡一层保险
        if req.marker is not None and "extra" in str(req.marker):
            continue
        out.append((req, parent))
    return out


def resolve(names: list[str], index_url: str, dry_run: bool) -> list[str]:
    """广度优先解析整棵依赖树，把 wheel 下到 .wheels/。返回顶层包名列表。"""
    WHEEL_DIR.mkdir(parents=True, exist_ok=True)

    queue: list[tuple[Requirement, str]] = [
        (Requirement(n), "命令行") for n in names
    ]
    seen: set[str] = set()
    installed: list[str] = []

    while queue:
        req, parent = queue.pop(0)
        key = f"{req.name}{req.specifier}"
        if key in seen:
            continue
        seen.add(key)

        if is_installed(req.name, req.specifier):
            print(f"  跳过 {req.name:28s} 已安装且满足 {req.specifier or '任意版本'}")
            continue

        picked = pick_wheel(index_url, req.name, req.specifier)
        if picked is None:
            if is_installed(req.name, None):
                continue
            print(f"  [警告] 找不到兼容的 wheel: {req.name} {req.specifier}  （来自 {parent}）")
            continue

        url, filename = picked
        target = WHEEL_DIR / filename
        if not target.exists():
            if dry_run:
                print(f"  将下载 {filename}")
            else:
                print(f"  下载   {filename}")
                target.write_bytes(fetch(url, timeout=300))
        else:
            print(f"  已有   {filename}")

        installed.append(req.name)

        if not dry_run:
            for child, _ in wheel_dependencies(target, req.name):
                if not is_installed(child.name, child.specifier):
                    queue.append((child, req.name))

    return installed


def main() -> int:
    ap = argparse.ArgumentParser(description="离线装包（绕过坏掉的 pip 网络栈）")
    ap.add_argument("packages", nargs="+", help="要安装的包名")
    ap.add_argument("--index", default=DEFAULT_INDEX, help=f"simple 索引地址，默认 {DEFAULT_INDEX}")
    ap.add_argument("--dry-run", action="store_true", help="只解析并列出，不下载不安装")
    args = ap.parse_args()

    print(f"索引：{args.index}")
    print(f"解释器：Python {sys.version.split()[0]} ({sys.platform})")
    print(f"轮子目录：{WHEEL_DIR}\n")

    print("解析依赖树：")
    to_install = resolve(args.packages, args.index, args.dry_run)

    if args.dry_run:
        print(
            "\n（--dry-run：只列了顶层包。传递依赖要下载 wheel 才能读它里面的\n"
            "  METADATA 解析出来，所以这里看不到。想看完整依赖树就别加 --dry-run。）"
        )
        return 0

    if not to_install:
        print("\n没有需要安装的东西。")
        return 0

    print(f"\n本地安装 {len(to_install)} 个包（pip 全程不碰网络）：")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--no-index", "--find-links", str(WHEEL_DIR),
        *to_install,
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
