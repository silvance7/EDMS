"""打包入口。

单独放一个顶层入口，而不是直接拿 `app/main.py` 当入口——
直接拿它当入口的话，PyInstaller 会把 sys.path[0] 设成 `app/` 目录，
`import app` 这套包结构就解析不到了。放在项目根，包结构才是完整的。

    python run.py            等价于 python -m app.main
    python run.py --show     启动并直接打开主窗口
"""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
