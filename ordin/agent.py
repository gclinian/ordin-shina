"""Agent：讀檔對欄位 → 對帳 → 理解老闆限制 → 診斷 → 試算 → 提案。

三種執行方式，產出相同結構的執行紀錄（steps + final）：
  1. live     ：LLM 當大腦（有 ANTHROPIC_API_KEY 走 Messages API，否則走本機 `claude -p`）
  2. replay   ：播放錄好的 LLM 執行紀錄（demo 用，不需連線）
  3. scripted ：完全不用 LLM 的決定性流程（內建欄位對應＋規則）

LLM 只負責判斷與說明；所有金額都由 core.py 的工具算出來。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from . import core

MODEL_API = os.environ.get("ORDIN_MODEL", "claude-sonnet-5-5")
MODEL_CLI = os.environ.get("ORDIN_CLI_MODEL", "sonnet")
MAX_STEPS = 16

SYSTEM = """你是「Ordin 實拿」：幫台灣小餐廳老闆把外送帳算清楚的 agent。老闆把平台對帳單、菜單成本、活動設定都放在一個資料夾裡。

你的工作流程：
1. 看有哪些檔案，讀老闆備註與各對帳單的表頭（read_file）。
2. 每個平台的對帳單格式不同。你要判斷每個欄位的意思，用 ingest 提交欄位對應。ingest 會逐筆重算撥款金額做硬檢查，沒通過就代表欄位對錯了，要修正後重來。兩個平台都通過才可以往下。
3. 把老闆用口語講的限制轉成結構化限制（set_constraints）。
4. analyze 取得每個品項、活動、廣告、退款的實拿數字。
5. 對有問題的品項用 price_options 看各種售價的試算，再決定調價或下架。
6. 用 simulate 試算整組動作，確認沒有違反老闆限制（violations 必須是空的）。
7. finish 交出動作清單與給老闆看的說明。

鐵則：
- 你不可以自己做任何算術。說明裡出現的每一個金額、百分比、單數，都必須是工具回傳過的數字，原樣引用。
- 只處理資料顯示有問題的地方（賠錢或幾乎沒賺的品項、賠錢的活動與廣告、退款熱點）。不要全面漲價。
- 老闆說不能動的就不能動；但可以在說明裡提醒他那件事正在賠多少。
- 說明用台灣老闆聽得懂的白話，繁體中文，短句，不要用術語（例如講「每賣一份實拿幾元」而不是「邊際貢獻」）。

工具（每次只能呼叫一個）：
- list_files {}
- read_file {"names": ["檔名", ...], "max_lines": 4}
- ingest {"platform": "A" 或 "B", "mapping": {"orders": {標準欄位: 檔案裡的表頭}, "lines": {...}, "ads": {...}}}
    orders 標準欄位：order_id, date, subtotal(商品小計), merchant_discount(店家負擔的折扣), platform_discount(平台負擔的折扣), promo_id(活動代碼), commission(佣金/服務費), commission_tax(佣金的營業稅), ad_campaign(這筆單來自哪個廣告), adjustment(客訴/退款扣款), refund_item(被退款的品項), payout(撥款金額)
    lines 標準欄位：order_id, item, qty, unit_price
    ads 標準欄位：date, campaign, spend
    檔名固定是 <平台>_orders.csv、<平台>_items.csv、<平台>_ads.csv
- set_constraints {"protected_items": [不可下架的品項], "max_raise": {品項: 最多漲幾元}, "max_raise_pct": 單一品項最多漲的比例(例如 0.2), "locked_promos": [不能退出的活動代碼]}
- analyze {}
- price_options {"targets": [["A","品項"], ...]}
- simulate {"actions": [動作, ...]}
- finish {"actions": [動作, ...], "report": "給老闆看的說明（markdown）"}

動作格式：
- {"type":"set_price","platform":"A","item":"品項","price":135}
- {"type":"delist","platform":"A","item":"品項"}          （這個平台下架，只做內用外帶）
- {"type":"end_promo","platform":"A","promo_id":"A-PROMO1"}
- {"type":"stop_ad","platform":"A","campaign":"A-AD1"}
- {"type":"fix_packaging","item":"品項"}                    （退款熱點：換防漏包材）

每一輪只輸出一個 JSON 物件，不要有其他文字：
{"thought": "一句話說明你現在在做什麼、為什麼", "tool": "工具名", "args": {...}}
"""


# ------------------------------------------------------------------ LLM 後端
def llm_api(system, prompt):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": MODEL_API, "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        body = json.load(r)
    return "".join(b.get("text", "") for b in body["content"])


def llm_cli(system, prompt):
    cmd = ["claude", "-p", "--model", MODEL_CLI, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--no-session-persistence", "--output-format", "json", "--system-prompt", system]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=240, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI 失敗：{r.stderr[:300] or r.stdout[:300]}")
    return json.loads(r.stdout)["result"]


def backend():
    pref = os.environ.get("ORDIN_LLM", "auto")
    if pref == "api" or (pref == "auto" and os.environ.get("ANTHROPIC_API_KEY")):
        return "api", llm_api
    return "cli", llm_cli


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    depth, in_s, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_s:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_s = False
            continue
        if ch == '"':
            in_s = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("回覆不是合法的 JSON")


# ------------------------------------------------------------------ 工具（Session）
class Session:
    def __init__(self, shop_dir):
        self.dir = shop_dir
        self.ctx = core.load_shop(shop_dir)
        self.orders, self.ads, self.reports = {}, {}, {}
        self.cons = {"protected_items": [], "max_raise": {}, "max_raise_pct": 0.2, "locked_promos": []}
        self.an = None
        self.ingest_attempts = 0

    # -- 資料 --
    def all_orders(self):
        return [o for p in sorted(self.orders) for o in self.orders[p]]

    def all_ads(self):
        return {k: v for p in self.ads for k, v in self.ads[p].items()}

    def ready(self):
        return all(self.reports.get(p, {}).get("ok") for p in self.ctx["plat"])

    # -- 工具 --
    def list_files(self, **_):
        return {"files": sorted(f for f in os.listdir(self.dir) if not f.startswith("_") and not f.startswith("."))}

    def read_file(self, names=None, max_lines=4, name=None, **_):
        names = names or ([name] if name else [])
        out = {}
        for n in names[:8]:
            path = os.path.join(self.dir, os.path.basename(n))
            if not os.path.exists(path) or os.path.basename(n).startswith("_"):
                out[n] = "找不到這個檔案"
                continue
            with open(path, encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            lim = 40 if n.endswith((".txt", ".json")) or n in ("promos.csv", "menu_costs.csv") else int(max_lines)
            out[n] = {"total_lines": len(lines), "head": lines[:lim]}
        return out

    def ingest(self, platform=None, mapping=None, **_):
        self.ingest_attempts += 1
        if platform not in self.ctx["plat"]:
            return {"ok": False, "errors": ["platform 必須是 A 或 B"]}
        o, a, rep = core.ingest(self.dir, platform, mapping or {}, self.ctx)
        self.reports[platform] = rep
        if rep["ok"]:
            self.orders[platform], self.ads[platform] = o, a
            self.an = None
        return rep

    def set_constraints(self, **kw):
        for k in ("protected_items", "max_raise", "max_raise_pct", "locked_promos"):
            if k in kw and kw[k] is not None:
                self.cons[k] = kw[k]
        return {"constraints": self.cons}

    def _need(self):
        if not self.ready():
            return {"error": "兩個平台的對帳單都要先通過 ingest 的檢查"}
        if self.an is None:
            self.an = core.analyze(self.ctx, self.all_orders(), self.all_ads())
        return None

    def analyze(self, **_):
        e = self._need()
        if e:
            return e
        an = self.an
        return {
            "totals": an["totals"],
            "problem_items": [{k: r[k] for k in ("platform", "item", "price", "dine", "qty", "sales", "unit_now", "unit_next", "month_next", "status")} for r in an["items"] if r["status"] != "ok"],
            "ok_item_count": sum(r["status"] == "ok" for r in an["items"]),
            "top_revenue_items": [{k: r[k] for k in ("platform", "item", "sales", "unit_next", "status")} for r in sorted(an["items"], key=lambda r: -r["sales"])[:5]],
            "promos": an["promos"], "ads": an["ads"], "refunds": an["refunds"][:3],
            "legend": "unit_next＝新費率下每賣一份實拿幾元；status: loss＝賠錢, thin＝幾乎沒賺；ads 的 loss＝就算帶來的單全算廣告功勞也不划算, doubt＝若一半的單本來就會來則不划算",
        }

    def price_options(self, targets=None, **_):
        e = self._need()
        if e:
            return e
        orders, ads = self.all_orders(), self.all_ads()
        prm = core.EstParams(self.ctx, orders)
        base = core.project(self.ctx, orders, ads, [], prm)["total"]
        a = core.aov(orders)
        out = []
        for p, n in (targets or [])[:12]:
            if p not in self.ctx["plat"] or n not in self.ctx["plat"][p]["prices"]:
                out.append({"platform": p, "item": n, "error": "沒有這個品項"})
                continue
            cur = self.ctx["plat"][p]["prices"][n]
            cap = core._cap(self.ctx, p, n, self.cons)
            opts, price = [], cur + core.PRICE_STEP
            while price <= cap:
                opts.append({"price": price, "unit_after": round(core.list_margin(self.ctx, p, n, a, price=price), 1),
                             "est_month_delta": round(core.delta(self.ctx, orders, ads, [{"type": "set_price", "platform": p, "item": n, "price": price}], prm, base))})
                price += core.PRICE_STEP
            out.append({"platform": p, "item": n, "current_price": cur, "max_allowed_price": cap, "can_delist": n not in self.cons["protected_items"],
                        "options": opts, "delist_est_month_delta": round(core.delta(self.ctx, orders, ads, [{"type": "delist", "platform": p, "item": n}], prm, base))})
        return {"options": out, "note": "est_month_delta＝預估每月多留下幾元（假設售價每漲 1% 銷量掉 1.5%）"}

    def simulate(self, actions=None, **_):
        e = self._need()
        if e:
            return e
        actions = [a for a in (actions or []) if isinstance(a, dict)]
        try:
            return self._sim(actions)
        except (KeyError, TypeError, ValueError) as ex:
            return {"error": f"動作格式有誤：{type(ex).__name__}: {ex}"}

    def _sim(self, actions):
        orders, ads = self.all_orders(), self.all_ads()
        prm = core.EstParams(self.ctx, orders)
        base = core.project(self.ctx, orders, ads, [], prm)["total"]
        per = [dict(a, est_delta=round(core.delta(self.ctx, orders, ads, [a], prm, base))) for a in actions]
        return {"base_next_month": round(base), "est_total_delta": round(core.delta(self.ctx, orders, ads, actions, prm, base)),
                "per_action": per, "violations": core.validate_actions(self.ctx, actions, self.cons)}

    def finalize(self, actions, report, notes=None):
        sim = self._sim(actions)
        acts = sorted(sim["per_action"], key=lambda a: -a["est_delta"])
        for a in acts:
            if a["type"] == "set_price":
                a.setdefault("old_price", self.ctx["plat"][a["platform"]]["prices"][a["item"]])
            a["reason"] = core.describe_action(self.ctx, self.an, a, self.all_orders())
        return {"actions": acts, "report": report, "notes": notes or [], "est_total": sim["est_total_delta"], "base_next": sim["base_next_month"],
                "violations": sim["violations"], "change_list": core.change_list(self.ctx, acts), "constraints": self.cons}


def _short(x, n=6000):
    s = json.dumps(x, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…（截斷）"


# ------------------------------------------------------------------ 三種執行方式
def run_live(shop_dir, on_step=None, verbose=False):
    name, call = backend()
    s = Session(shop_dir)
    steps, transcript, final = [], [], None
    t0 = time.time()
    for i in range(MAX_STEPS):
        prompt = "到目前為止的過程：\n" + ("\n".join(transcript) if transcript else "（還沒開始）") + "\n\n請輸出下一步的 JSON。"
        raw = call(SYSTEM, prompt)
        try:
            msg = parse_json(raw)
            tool, args = msg.get("tool"), msg.get("args") or {}
        except Exception as ex:
            transcript.append(f"[第 {i + 1} 步] 你的回覆不是合法 JSON（{ex}），請只輸出一個 JSON 物件。")
            continue
        if tool == "finish":
            if not s.ready():
                result = {"error": "對帳單還沒全部通過檢查，不能結束"}
            else:
                try:
                    final = s.finalize(args.get("actions") or [], args.get("report") or "")
                    result = {"ok": not final["violations"], "violations": final["violations"], "est_total_delta": final["est_total"]}
                    if final["violations"]:
                        final = None
                        result["error"] = "有動作違反老闆限制，請修正後再 finish"
                except Exception as ex:
                    result = {"error": f"動作格式有誤：{ex}"}
        elif tool in ("list_files", "read_file", "ingest", "set_constraints", "analyze", "price_options", "simulate"):
            try:
                result = getattr(s, tool)(**args)
            except Exception as ex:
                result = {"error": f"{type(ex).__name__}: {ex}"}
        else:
            result = {"error": f"沒有 {tool} 這個工具"}
        step = {"i": len(steps) + 1, "thought": msg.get("thought", ""), "tool": tool, "args": args, "result": result, "t": round(time.time() - t0, 1)}
        steps.append(step)
        if on_step:
            on_step(step)
        if verbose:
            print(f"  [{step['i']}] {tool}: {step['thought'][:70]}", file=sys.stderr)
        transcript.append(f"[第 {step['i']} 步] 你呼叫 {tool}，參數 {_short(args, 2500)}\n工具回傳：{_short(result)}")
        if final:
            break
    return _pack(s, steps, final, mode="live", backend=name, model=MODEL_API if name == "api" else MODEL_CLI, seconds=round(time.time() - t0, 1))


def run_scripted(shop_dir, on_step=None):
    """不用 LLM：內建欄位對應、預先結構化的老闆限制、規則式建議。"""
    s = Session(shop_dir)
    steps = []

    def do(thought, tool, args, result=None):
        result = getattr(s, tool)(**args) if result is None else result
        step = {"i": len(steps) + 1, "thought": thought, "tool": tool, "args": args, "result": result, "t": 0}
        steps.append(step)
        if on_step:
            on_step(step)
        return result

    do("先看老闆給了哪些檔案。", "list_files", {})
    for p in sorted(s.ctx["plat"]):
        rep = do(f"用內建的平台 {p} 欄位對應讀入對帳單，逐筆重算撥款金額。", "ingest", {"platform": p, "mapping": core.KNOWN_MAPPINGS[p]})
        if not rep["ok"]:
            return _pack(s, steps, None, mode="scripted", error=f"平台 {p} 的對帳單不是內建認得的格式（{len(rep['errors'])} 個預期的表頭找不到，例如：{rep['errors'][0]}）。這種情況需要 LLM 模式來判斷欄位。")
    cons_path = os.path.join(shop_dir, "_truth", "world.json")
    cons = json.load(open(cons_path, encoding="utf-8"))["constraints"] if os.path.exists(cons_path) else {}
    do("套用老闆的限制（決定性模式使用預先結構化的版本；LLM 模式會從老闆備註自己讀出來）。", "set_constraints", cons)
    do("計算每個品項、活動、廣告在新費率下的實拿。", "analyze", {})
    pb = core.playbook(s.ctx, s.all_orders(), s.all_ads(), s.cons, s.an)
    targets = [[a["platform"], a["item"]] for a in pb["actions"] if a["type"] in ("set_price", "delist")]
    do("對有問題的品項試算各種售價與下架的影響。", "price_options", {"targets": targets})
    acts = [{k: v for k, v in a.items() if k not in ("est_delta", "reason", "old_price")} for a in pb["actions"]]
    do("把整組動作一起試算，並檢查有沒有違反老闆限制。", "simulate", {"actions": acts})
    report = scripted_report(s, pb)
    final = s.finalize(acts, report, pb["notes"])
    do("交出變更清單。", "finish", {"actions": acts}, {"ok": True, "violations": [], "est_total_delta": final["est_total"]})
    return _pack(s, steps, final, mode="scripted")


def scripted_report(s, pb):
    t = s.an["totals"]
    L = [f"### 上個月外送的帳", f"- 外送賣了 {t['sales']:,} 元，平台撥款 {t['payout']:,} 元。",
         f"- 扣掉食材、包材和廣告費，實際留下 {t['actual_contribution']:,} 元。",
         f"- 平台 A 新費率上路後，同樣的生意每月會再少 {abs(t['rate_change_impact']):,} 元。", "", "### 建議先做這幾件事"]
    for a in pb["actions"]:
        L.append(f"- {a['reason']}（預估每月多留 {a['est_delta']:,} 元）")
    for n in pb["notes"]:
        L.append(f"- 提醒：{n['text']}")
    L += ["", f"全部照做，預估每月多留下 {pb['est_total']:,} 元。這是用保守假設估的，實際要看客人反應。"]
    return "\n".join(L)


def _pack(s, steps, final, **meta):
    out = dict(meta, shop=s.ctx["shop"], steps=steps, final=final, ingest=s.reports, ingest_attempts=s.ingest_attempts, ok=final is not None)
    if s.ready():
        if s.an is None:
            s.an = core.analyze(s.ctx, s.all_orders(), s.all_ads())
        out["analysis"] = s.an
    return out


# ------------------------------------------------------------------ 評分（只在有正解的模擬店家上）
def score(shop_dir, run):
    """把 agent 的結果和正解比：問題處理率、限制遵守、真實模擬器下的效果、報告數字可回溯率。"""
    truth = json.load(open(os.path.join(shop_dir, "_truth", "world.json"), encoding="utf-8"))
    m = {"completed": bool(run.get("ok")), "steps": len(run["steps"]), "seconds": run.get("seconds"), "ingest_attempts": run.get("ingest_attempts"),
         "ingest_ok": all(r.get("ok") and r.get("payout_match") == 1.0 for r in run.get("ingest", {}).values()) and len(run.get("ingest", {})) == 2}
    if not run.get("ok"):
        return m
    f, t, tc = run["final"], truth["truth"], truth["constraints"]
    acts = f["actions"]
    m["constraints_correct"] = (sorted(f["constraints"].get("protected_items", [])) == sorted(tc["protected_items"])
                                and {k: float(v) for k, v in f["constraints"].get("max_raise", {}).items()} == {k: float(v) for k, v in tc["max_raise"].items()}
                                and sorted(f["constraints"].get("locked_promos", [])) == sorted(tc["locked_promos"]))
    ctx = core.load_shop(shop_dir)
    m["violations_vs_truth"] = len(core.validate_actions(ctx, acts, tc))
    handled = {"loss_item": {f"{a['platform']}|{a['item']}" for a in acts if a["type"] in ("set_price", "delist")},
               "loss_promo": {a["promo_id"] for a in acts if a["type"] == "end_promo"},
               "loss_ad": {a["campaign"] for a in acts if a["type"] == "stop_ad"},
               "refund_hotspot": {a["item"] for a in acts if a["type"] == "fix_packaging"}}
    tot = hit = 0
    for c, keys in t.items():
        keys = [k for k in keys if not (c == "loss_promo" and k in tc["locked_promos"])]
        tot += len(keys)
        hit += len(set(keys) & handled[c])
        m[f"{c}_handled"] = f"{len(set(keys) & handled[c])}/{len(keys)}"
    m["issues_total"], m["issues_handled"] = tot, hit
    # 真實模擬器
    orders, ads = [], {}
    for p in ("A", "B"):
        o, a, _ = core.ingest(shop_dir, p, _truth_mapping(truth["formats"][p]), ctx)
        orders += o
        ads.update(a)
    tp = core.TrueParams(truth, ctx, orders)
    base = core.project(ctx, orders, ads, [], tp)["total"]
    m["true_delta"] = round(core.delta(ctx, orders, ads, acts, tp, base))
    m["est_delta"] = f["est_total"]
    pb = core.playbook(ctx, orders, ads, tc)
    m["rule_playbook_true_delta"] = round(core.delta(ctx, orders, ads, pb["actions"], tp, base))
    # 報告裡的數字是否都能在工具輸出中找到
    seen = set(re.findall(r"\d+(?:\.\d+)?", json.dumps([s["result"] for s in run["steps"]], ensure_ascii=False).replace(",", "")))
    seen |= {x.rstrip("0").rstrip(".") for x in list(seen) if "." in x}
    nums = [x.replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", f["report"])]
    nums = [x for x in nums if len(x.split(".")[0]) >= 3]
    ok = [x for x in nums if x in seen or x.rstrip("0").rstrip(".") in seen]
    m["report_numbers"], m["report_numbers_traceable"] = len(nums), len(ok)
    m["report_numbers_untraceable"] = sorted(set(nums) - set(ok))[:10]
    return m


def _truth_mapping(fmt):
    from . import world
    return {kind: {c: world.HEADERS[kind][c][fmt] for c in world.HEADERS[kind]} for kind in world.HEADERS}


if __name__ == "__main__":
    # python3 -m ordin.agent <shop_dir> [live|scripted] [輸出檔]
    d = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "scripted"
    r = run_live(d, verbose=True) if mode == "live" else run_scripted(d)
    if os.path.exists(os.path.join(d, "_truth", "world.json")):
        r["score"] = score(d, r)
        print(json.dumps(r["score"], ensure_ascii=False, indent=1))
    if len(sys.argv) > 3:
        json.dump(r, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False)
