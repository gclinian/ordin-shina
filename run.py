#!/usr/bin/env python3
"""Ordin 實拿：一行指令啟動。

  python3 run.py                 啟動 demo（http://localhost:4103）
  python3 run.py bench [N]       在 N 家模擬店家上跑驗證（預設 30）
  python3 run.py gen <seed> <dir> [B格式]   產生一家模擬店家
  python3 run.py agent <dir> [live|scripted] [輸出檔]   在命令列跑 agent
  python3 run.py llm-eval [seeds...]   用真的 LLM 在多家模擬店家上跑並評分
  python3 run.py test            跑單元測試
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)


def ensure_demo():
    from ordin import world
    if not os.path.exists("data/demo/contract.json"):
        w = world.make_world(99)
        world.export(w, "data/demo")
        world.export(w, "data/demo_newformat", fmt_override={"B": 2})


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        ensure_demo()
        from ordin import server
        server.serve(int(os.environ.get("PORT", 4103)))
    elif cmd == "bench":
        from ordin import bench
        print(bench.to_md(bench.run(int(sys.argv[2]) if len(sys.argv) > 2 else 30)))
    elif cmd == "gen":
        from ordin import world
        fmt = {"B": int(sys.argv[4])} if len(sys.argv) > 4 else None
        world.export(world.make_world(int(sys.argv[2])), sys.argv[3], fmt_override=fmt)
        print("已產生", sys.argv[3])
    elif cmd == "agent":
        sys.argv = ["ordin.agent"] + sys.argv[2:]
        import runpy
        runpy.run_module("ordin.agent", run_name="__main__")
    elif cmd == "llm-eval":
        from ordin import llm_eval
        llm_eval.main([int(x) for x in sys.argv[2:]] or [1, 2, 3, 4, 5])
    elif cmd == "test":
        import unittest
        sys.exit(not unittest.main(module=None, argv=["x", "discover", "-s", "tests", "-v"], exit=False).result.wasSuccessful())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
