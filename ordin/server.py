"""本機伺服器（只用標準函式庫）。預設 http://localhost:4103"""
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import agent, core

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA, REC, RES, WEB = (os.path.join(ROOT, x) for x in ("data", "recordings", "results", "web"))
SESSIONS, JOBS = {}, {}


def shop_dir(shop):
    d = os.path.join(DATA, os.path.basename(shop))
    if not os.path.isdir(d):
        raise FileNotFoundError(shop)
    return d


def rec_path(shop):
    return os.path.join(REC, f"{os.path.basename(shop)}.live.json")


def replay(shop):
    """回放錄好的 LLM 執行紀錄：LLM 的每個決定用錄好的，每個數字現場重算。"""
    rec = json.load(open(rec_path(shop), encoding="utf-8"))
    s = agent.Session(shop_dir(shop))
    steps, final = [], None
    for st in rec["steps"]:
        tool, args = st["tool"], st["args"]
        if tool == "finish":
            try:
                final = s.finalize(args.get("actions") or [], args.get("report") or "")
                result = {"ok": not final["violations"], "violations": final["violations"], "est_total_delta": final["est_total"]}
                if final["violations"]:
                    final = None
            except Exception as ex:
                result = {"error": str(ex)}
        elif hasattr(s, tool):
            try:
                result = getattr(s, tool)(**args)
            except Exception as ex:
                result = {"error": f"{type(ex).__name__}: {ex}"}
        else:
            result = st["result"]
        steps.append(dict(st, result=result))
    SESSIONS[shop] = s
    return agent._pack(s, steps, final, mode="replay", backend=rec.get("backend"), model=rec.get("model"), seconds=rec.get("seconds"))


def run(shop, mode, on_step=None):
    d = shop_dir(shop)
    if mode == "replay":
        return replay(shop)
    r = agent.run_live(d, on_step=on_step) if mode == "live" else agent.run_scripted(d, on_step=on_step)
    # 用這次通過檢查的欄位對應重建工作階段，供介面即時試算
    s = agent.Session(d)
    for st in r["steps"]:
        if st["tool"] in ("ingest", "set_constraints"):
            getattr(s, st["tool"])(**st["args"])
    SESSIONS[shop] = s
    return r


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, q = self.path.partition("?")
        qs = dict(x.split("=", 1) for x in q.split("&") if "=" in x)
        try:
            if path in ("/", "/index.html"):
                return self._send(open(os.path.join(WEB, "index.html"), "rb").read(), ctype="text/html; charset=utf-8")
            if path == "/api/shops":
                out = []
                for s in sorted(os.listdir(DATA)):
                    if os.path.isdir(os.path.join(DATA, s)):
                        con = json.load(open(os.path.join(DATA, s, "contract.json"), encoding="utf-8"))
                        out.append({"id": s, "name": con["shop"], "has_recording": os.path.exists(rec_path(s)),
                                    "llm": "api" if os.environ.get("ANTHROPIC_API_KEY") else "claude CLI"})
                return self._send(out)
            if path == "/api/files":
                d = shop_dir(qs["shop"])
                s = agent.Session(d)
                names = s.list_files()["files"]
                return self._send({"files": s.read_file(names=names, max_lines=3) | s.read_file(names=names[8:], max_lines=3)})
            if path == "/api/job":
                j = JOBS[qs["id"]]
                return self._send({"steps": j["steps"], "done": j["done"], "run": j.get("run"), "error": j.get("error")})
            if path == "/api/benchmark":
                out = {}
                for n in ("benchmark", "llm_eval"):
                    p = os.path.join(RES, n + ".json")
                    out[n] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
                return self._send(out)
            return self._send({"error": "not found"}, 404)
        except Exception as ex:
            return self._send({"error": f"{type(ex).__name__}: {ex}"}, 500)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            if self.path == "/api/run":
                shop, mode = body["shop"], body.get("mode", "scripted")
                if mode != "live":
                    return self._send(run(shop, mode))
                jid = uuid.uuid4().hex[:8]
                JOBS[jid] = {"steps": [], "done": False}

                def work():
                    try:
                        JOBS[jid]["run"] = run(shop, "live", on_step=JOBS[jid]["steps"].append)
                    except Exception as ex:
                        JOBS[jid]["error"] = f"{type(ex).__name__}: {ex}"
                    JOBS[jid]["done"] = True

                threading.Thread(target=work, daemon=True).start()
                return self._send({"job": jid})
            if self.path == "/api/simulate":
                s = SESSIONS[body["shop"]]
                sim = s.simulate(actions=body.get("actions") or [])
                if "error" not in sim:
                    sim["change_list"] = core.change_list(s.ctx, body.get("actions") or [])
                return self._send(sim)
            return self._send({"error": "not found"}, 404)
        except Exception as ex:
            return self._send({"error": f"{type(ex).__name__}: {ex}"}, 500)


def serve(port=4103):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Ordin 實拿 已啟動：http://localhost:{port}  （Ctrl+C 結束）")
    srv.serve_forever()
