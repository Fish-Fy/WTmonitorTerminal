"""公开版入口；自检只使用临时数据，不连接游戏。"""
import sys

if "--self-test" in sys.argv:
    from share_smoke import run
    raise SystemExit(run())

from main import main
raise SystemExit(main())
