"""計算核心：讀檔對帳、每品項每通路的實拿、診斷、影響模擬、建議動作。

原則：所有金額都由這裡的程式計算，LLM 不做任何算術。
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime

FIX_COST = 3          # 改用防漏包材每份多 3 元（假設）
THIN = 0.08           # 每份留下不到售價 8% 視為「做白工」
PRICE_STEP = 5

CANON = {
    "orders": ["order_id", "date", "subtotal", "merchant_discount", "platform_discount", "promo_id", "commission",
               "commission_tax", "ad_campaign", "adjustment", "refund_item", "payout"],
    "lines": ["order_id", "item", "qty", "unit_price"],
    "ads": ["date", "campaign", "spend"],
}
# 已知平台格式的內建欄位對應（決定性模式用；沒看過的格式要靠 LLM 對欄位）
KNOWN_MAPPINGS = {
    "A": {"orders": dict(zip(CANON["orders"], ["訂單編號", "訂單日期", "商品小計", "店家負擔折扣", "平台負擔折扣", "活動代碼", "佣金", "佣金營業稅", "廣告活動", "調整金額", "調整說明品項", "撥款金額"])),
          "lines": dict(zip(CANON["lines"], ["訂單編號", "品項", "數量", "單價"])),
          "ads": dict(zip(CANON["ads"], ["日期", "廣告活動", "花費"]))},
    "B": {"orders": dict(zip(CANON["orders"], ["order_id", "order_date", "sales_incl_tax", "promo_merchant_funded", "promo_platform_funded", "promo_code", "marketplace_fee", "marketplace_fee_tax", "ad_campaign", "order_error_adjustment", "error_item", "total_payout"])),
          "lines": dict(zip(CANON["lines"], ["order_id", "item_name", "quantity", "price"])),
          "ads": dict(zip(CANON["ads"], ["date", "campaign_name", "ad_spend"]))},
}


# ------------------------------------------------------------------ 讀檔
def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _num(s):
    s = str(s).replace(",", "").strip()
    return abs(float(s)) if s else 0.0


def _date(s):
    s = s.strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"看不懂的日期格式：{s}")


def load_shop(d):
    """讀老闆自己的檔案：菜單成本、平台售價、活動、合約費率。"""
    con = json.load(open(os.path.join(d, "contract.json"), encoding="utf-8"))
    items = {r["品項"]: {"cat": r["類別"], "dine": float(r["內用價"]), "food": float(r["食材成本"]), "pack": float(r["外送包材成本"])}
             for r in _read(os.path.join(d, "menu_costs.csv"))}
    plat = {}
    for p in ("A", "B"):
        plat[p] = dict(con[p], prices={}, promos={})
    for r in _read(os.path.join(d, "platform_menu.csv")):
        plat[r["平台"]]["prices"][r["品項"]] = float(r["平台售價"])
    for r in _read(os.path.join(d, "promos.csv")):
        pm = {"id": r["活動代碼"], "name": r["活動名稱"], "type": r["類型"], "share": float(r["店家負擔比例"])}
        if pm["type"] == "item_pct":
            pm.update(item=r["指定品項"], pct=float(r["折扣比例"]), bogo=bool(r["買一送一"]))
        else:
            pm.update(threshold=float(r["門檻"]), amount=float(r["折抵金額"]))
        plat[r["平台"]]["promos"][pm["id"]] = pm
    direct = {r["品項"]: int(r["內用外帶銷量"]) for r in _read(os.path.join(d, "pos_direct.csv"))}
    return {"shop": con["shop"], "month": con["month"], "items": items, "bag": float(con["bag_cost"]), "plat": plat, "direct": direct}


def ctx_from_world(w):
    plat = {p: {k: v for k, v in w["plats"][p].items()} for p in ("A", "B")}
    return {"shop": w["shop"], "month": "2026-10", "items": w["items"], "bag": w["bag"], "plat": plat, "direct": w.get("direct", {})}


def ingest(d, platform, mapping, ctx, files=None):
    """依欄位對應讀入對帳單，轉成標準格式，並做三項硬檢查。回傳 (orders, ad_spend, report)。"""
    files = files or {"orders": f"{platform}_orders.csv", "lines": f"{platform}_items.csv", "ads": f"{platform}_ads.csv"}
    report = {"platform": platform, "ok": False, "errors": []}
    try:
        raw_o, raw_l, raw_a = (_read(os.path.join(d, files[k])) for k in ("orders", "lines", "ads"))
        for kind, raw in (("orders", raw_o), ("lines", raw_l), ("ads", raw_a)):
            for c in CANON[kind]:
                col = mapping.get(kind, {}).get(c)
                if col is None:
                    report["errors"].append(f"{kind} 缺少標準欄位 {c} 的對應")
                elif raw and col not in raw[0]:
                    report["errors"].append(f"{files[kind]} 沒有「{col}」這個欄位（要對應到 {c}）")
        if report["errors"]:
            return [], {}, report
        mo, ml, ma = mapping["orders"], mapping["lines"], mapping["ads"]
        lines = defaultdict(list)
        for r in raw_l:
            lines[r[ml["order_id"]]].append([r[ml["item"]], float(r[ml["qty"]]), _num(r[ml["unit_price"]])])
        orders = []
        for r in raw_o:
            o = {"platform": platform, "order_id": r[mo["order_id"]], "date": _date(r[mo["date"]]),
                 "promo_id": r[mo["promo_id"]] or None, "ad_campaign": r[mo["ad_campaign"]] or None,
                 "refund_item": r[mo["refund_item"]] or ""}
            for c in ("subtotal", "merchant_discount", "platform_discount", "commission", "commission_tax", "adjustment", "payout"):
                o[c] = _num(r[mo[c]])
            o["payout"] = float(str(r[mo["payout"]]).replace(",", "") or 0)  # 撥款可能為負（整單退款），保留正負號
            o["lines"] = lines.get(o["order_id"], [])
            orders.append(o)
        ad_spend = defaultdict(float)
        for r in raw_a:
            ad_spend[r[ma["campaign"]]] += _num(r[ma["spend"]])
    except Exception as e:  # 欄位對錯通常會在這裡炸掉
        report["errors"].append(f"讀檔失敗：{type(e).__name__}: {e}")
        return [], {}, report
    pl = ctx["plat"][platform]
    n = len(orders) or 1
    pay_ok = sum(abs(o["subtotal"] - o["merchant_discount"] - o["commission"] - o["commission_tax"] - o["adjustment"] - o["payout"]) <= 1 for o in orders)
    line_ok = sum(abs(sum(q * pr for _, q, pr in o["lines"]) - o["subtotal"]) <= 1 for o in orders)
    com_ok = sum(abs(pl["rate"] * (o["subtotal"] - (o["merchant_discount"] if pl["base"] == "net" else 0)) - o["commission"]) <= 1 for o in orders)
    base_sum = sum(o["subtotal"] - (o["merchant_discount"] if pl["base"] == "net" else 0) for o in orders) or 1
    unknown = sorted({nm for o in orders for nm, _, _ in o["lines"] if nm not in ctx["items"]})
    report.update(
        orders=len(orders), payout_total=round(sum(o["payout"] for o in orders)), ad_spend_total=round(sum(ad_spend.values())),
        payout_match=round(pay_ok / n, 4), lines_match=round(line_ok / n, 4), commission_match=round(com_ok / n, 4),
        effective_rate=round(sum(o["commission"] for o in orders) / base_sum, 4), contract_rate=pl["rate"], unknown_items=unknown)
    if report["payout_match"] < 0.999:
        report["errors"].append("逐筆重算的撥款金額和對帳單不一致，欄位對應可能有錯")
    if report["lines_match"] < 0.999:
        report["errors"].append("品項明細加總和商品小計不一致")
    if report["commission_match"] < 0.98:
        report["errors"].append("佣金和合約費率算出來的不一致")
    if unknown:
        report["errors"].append(f"對帳單有菜單成本表裡沒有的品項：{unknown[:5]}")
    report["ok"] = not report["errors"]
    return (orders if report["ok"] else []), dict(ad_spend), report


# ------------------------------------------------------------------ 參數（假設 vs 真實）
def _substitutes(ctx, orders):
    sold = defaultdict(float)
    for o in orders:
        for n, q, _ in o["lines"]:
            sold[(o["platform"], n)] += q
    sub = {}
    for p in ctx["plat"]:
        for n, v in ctx["items"].items():
            cands = [(sold[(p, m)], m) for m, mv in ctx["items"].items() if m != n and mv["cat"] == v["cat"]]
            sub[(p, n)] = max(cands)[1] if cands else None
    return sub


class EstParams:
    """agent 的假設：不知道真實彈性與活動增量，用保守的固定假設。"""
    name = "est"

    def __init__(self, ctx, orders, eps=1.5, promo_inc=0.5, ad_inc=0.5, sigma=0.3, fix_eff=0.7):
        self.e, self.pi, self.ai, self.s, self.fe = eps, promo_inc, ad_inc, sigma, fix_eff
        self.sub = _substitutes(ctx, orders)
        n_lines, n_ref = defaultdict(int), defaultdict(int)
        for o in orders:
            for n, _, _ in o["lines"]:
                n_lines[n] += 1
            if o.get("adjustment", 0) > 0 and o.get("refund_item"):
                n_ref[o["refund_item"]] += 1
        base = sum(n_ref.values()) / max(1, sum(n_lines.values()))
        self.ref = {n: (n_ref[n] + 20 * base) / (n_lines[n] + 20) for n in ctx["items"]}
        self.obs_refund = {n: (n_ref[n], n_lines[n]) for n in ctx["items"]}
        self.base_refund = base

    def eps(self, p, n): return self.e
    def keep_promo(self, o): return 1 - self.pi
    def keep_ad(self, o): return 1 - self.ai
    def sigma(self, p, n): return self.s
    def substitute(self, p, n): return self.sub.get((p, n))
    def refund(self, n): return self.ref.get(n, self.base_refund)
    def fix_eff(self, n): return self.fe


class TrueParams:
    """模擬世界的真實參數（只有評分用的模擬器看得到）。"""
    name = "true"

    def __init__(self, w, ctx=None, orders=None, eps_mult=1.0):
        self.h = w["hidden"]
        self.m = eps_mult
        if "orders" in w:
            orders = w["orders"]
            ctx = ctx or ctx_from_world(w)
            self.flags = {o["order_id"]: (o["inc_promo"], o["inc_ad"]) for o in orders}
        else:
            self.flags = {k: tuple(v) for k, v in w["order_flags"].items()}
        self.sub = _substitutes(ctx, orders)

    def eps(self, p, n): return self.h["eps"][f"{p}|{n}"] * self.m
    def keep_promo(self, o): return 0.0 if self.flags[o["order_id"]][0] else 1.0
    def keep_ad(self, o): return 0.0 if self.flags[o["order_id"]][1] else 1.0
    def sigma(self, p, n): return self.h["sigma"][f"{p}|{n}"]
    def substitute(self, p, n): return self.sub.get((p, n))
    def refund(self, n): return self.h["refund"][n]
    def fix_eff(self, n): return self.h["fix_eff"]


# ------------------------------------------------------------------ 影響模擬
def project(ctx, orders, ad_spend, actions, prm, rate_key="rate_next"):
    """給定一組動作，推估「下個月」的外送邊際貢獻（期望值）。

    需求模型（假設）：線性，售價每漲 1%，該品項銷量掉 eps%。
    下架：sigma 比例的客人改點同類別最熱賣的品項，其餘流失。
    退出活動／停廣告：屬於「因為活動才來」的訂單會消失，其餘訂單照原價留下。
    """
    newp, delist, ended, stopped, fixed, quit_ = {}, set(), set(), set(), set(), set()
    for a in actions:
        t = a["type"]
        if t == "set_price":
            newp[(a["platform"], a["item"])] = float(a["price"])
        elif t == "delist":
            delist.add((a["platform"], a["item"]))
        elif t == "end_promo":
            ended.add(a["promo_id"])
        elif t == "stop_ad":
            stopped.add(a["campaign"])
        elif t == "fix_packaging":
            fixed.add(a["item"])
        elif t == "quit_platform":
            quit_.add(a["platform"])
    items, bag = ctx["items"], ctx["bag"]
    out = {"total": 0.0, "by_platform": defaultdict(float), "by_item": defaultdict(float), "qty": defaultdict(float),
           "sales": defaultdict(float), "by_promo": defaultdict(float), "by_ad": defaultdict(float),
           "n_promo": defaultdict(float), "n_ad": defaultdict(float), "orders": defaultdict(float)}
    for o in orders:
        p = o["platform"]
        if p in quit_:
            continue
        pl = ctx["plat"][p]
        w = 1.0
        promo = o.get("promo_id")
        pm = pl["promos"].get(promo) if promo else None
        promo_off = bool(pm) and promo in ended
        if promo_off:
            w *= prm.keep_promo(o)
        ad = o.get("ad_campaign")
        if ad and ad in stopped:
            w *= prm.keep_ad(o)
        if w <= 0:
            continue
        lines, q0 = [], 0.0
        for n, q, pr in o["lines"]:
            q0 += q
            if promo_off and pm.get("bogo") and n == pm["item"]:
                q = q / 2.0
            if (p, n) in delist:
                s = prm.substitute(p, n)
                if s and (p, s) not in delist:
                    lines.append((s, q * prm.sigma(p, n), newp.get((p, s), pl["prices"][s])))
            else:
                np_ = newp.get((p, n), pr)
                f = max(0.0, 1 - prm.eps(p, n) * (np_ / pr - 1)) if np_ != pr else 1.0
                if f > 0:
                    lines.append((n, q * f, np_))
        sub = sum(q * pr for _, q, pr in lines)
        if sub <= 0:
            continue
        disc_item, disc_order = 0.0, 0.0
        if pm and not promo_off:
            if pm["type"] == "item_pct":
                disc_item = pm["pct"] * pm["share"]
            else:
                disc_order = pm["amount"] * pm["share"] * min(1.0, sub / max(1.0, o["subtotal"]))
        d_item_total = sum(q * pr for n, q, pr in lines if pm and n == pm.get("item")) * disc_item
        disc = d_item_total + disc_order
        base = sub - disc if pl["base"] == "net" else sub
        fee = pl[rate_key] * base * (1 + pl["tax"])
        bagc = bag * min(1.0, sum(q for _, q, _ in lines) / q0)
        c_total = 0.0
        for n, q, pr in lines:
            val = q * pr
            share = val / sub
            own_disc = val * disc_item if (pm and n == pm.get("item")) else 0.0
            ref = val * prm.refund(n) * ((1 - prm.fix_eff(n)) if n in fixed else 1.0)
            cost = q * (items[n]["food"] + items[n]["pack"] + (FIX_COST if n in fixed else 0))
            c = val - own_disc - share * disc_order - share * fee - cost - share * bagc - ref
            c_total += c
            out["by_item"][(p, n)] += w * c
            out["qty"][(p, n)] += w * q
            out["sales"][(p, n)] += w * val
        out["total"] += w * c_total
        out["by_platform"][p] += w * c_total
        out["orders"][p] += w
        if pm and not promo_off:
            out["by_promo"][(p, promo)] += w * c_total
            out["n_promo"][(p, promo)] += w
        if ad and ad not in stopped:
            out["by_ad"][(p, ad)] += w * c_total
            out["n_ad"][(p, ad)] += w
    out["ad_spend"] = sum(s for camp, s in ad_spend.items() if camp not in stopped and camp[0] not in quit_)
    out["total"] -= out["ad_spend"]
    return out


def delta(ctx, orders, ad_spend, actions, prm, base=None):
    base = base if base is not None else project(ctx, orders, ad_spend, [], prm)["total"]
    return project(ctx, orders, ad_spend, actions, prm)["total"] - base


def aov(orders):
    s, n = defaultdict(float), defaultdict(int)
    for o in orders:
        s[o["platform"]] += o["subtotal"]
        n[o["platform"]] += 1
    return {p: s[p] / n[p] for p in s}


def list_margin(ctx, p, n, a, rate_key="rate_next", price=None):
    """牌價下每賣一份的實拿：售價 − 佣金(含稅) − 食材 − 包材 − 提袋餐具分攤。"""
    pl, it = ctx["plat"][p], ctx["items"][n]
    price = pl["prices"][n] if price is None else price
    return price * (1 - pl[rate_key] * (1 + pl["tax"])) - it["food"] - it["pack"] - ctx["bag"] * price / a[p]


# ------------------------------------------------------------------ 診斷
def analyze(ctx, orders, ad_spend):
    prm = EstParams(ctx, orders)
    a = aov(orders)
    now = project(ctx, orders, ad_spend, [], prm, rate_key="rate")
    nxt = project(ctx, orders, ad_spend, [], prm)
    items = []
    for p in ctx["plat"]:
        pl = ctx["plat"][p]
        for n, it in ctx["items"].items():
            q = nxt["qty"].get((p, n), 0)
            if q <= 0:
                continue
            price = pl["prices"][n]
            m_now, m_next = list_margin(ctx, p, n, a, "rate"), list_margin(ctx, p, n, a)
            items.append({
                "key": f"{p}|{n}", "platform": p, "item": n, "cat": it["cat"], "price": price, "dine": it["dine"],
                "qty": round(q), "sales": round(nxt["sales"][(p, n)]), "unit_now": round(m_now, 1), "unit_next": round(m_next, 1),
                "pct_next": round(m_next / price, 3), "month_next": round(nxt["by_item"][(p, n)]),
                "dine_unit": round(it["dine"] - it["food"], 1), "naive_unit": round(price * (1 - pl["rate_next"]) - it["food"], 1),
                "status": "loss" if m_next < 0 else ("thin" if m_next < THIN * price else "ok")})
    promos = []
    for p in ctx["plat"]:
        for pid, pm in ctx["plat"][p]["promos"].items():
            po = [o for o in orders if o["platform"] == p and o["promo_id"] == pid]
            c = nxt["by_promo"].get((p, pid), 0.0)
            promos.append({"key": pid, "platform": p, "name": pm["name"], "orders": len(po),
                           "merchant_discount": round(sum(o["merchant_discount"] for o in po)),
                           "contribution": round(c), "per_order": round(c / max(1, len(po)), 1),
                           "status": "loss" if c < 0 else "ok"})
    ads = []
    for camp, spend in sorted(ad_spend.items()):
        p = camp[0]
        ao = [o for o in orders if o["ad_campaign"] == camp]
        c = nxt["by_ad"].get((p, camp), 0.0)
        ads.append({"key": camp, "platform": p, "spend": round(spend), "orders": len(ao), "sales": round(sum(o["subtotal"] for o in ao)),
                    "contribution": round(c), "net_if_all_incremental": round(c - spend),
                    "status": "loss" if c < spend else ("doubt" if c * 0.5 < spend else "ok")})
    refunds = []
    for n in ctx["items"]:
        k, ln = prm.obs_refund[n]
        rate = k / ln if ln else 0.0
        amt = sum(o["adjustment"] for o in orders if o.get("refund_item") == n)
        hot = ln >= 15 and rate >= 0.05 and rate >= 4 * prm.base_refund
        refunds.append({"key": n, "item": n, "refund_orders": k, "orders_with_item": ln, "rate": round(rate, 4), "amount": round(amt), "status": "hot" if hot else "ok"})
    refunds.sort(key=lambda r: -r["rate"])
    var_cost = sum(q * (ctx["items"][n]["food"] + ctx["items"][n]["pack"]) for o in orders for n, q, _ in o["lines"]) + ctx["bag"] * len(orders)
    totals = {
        "sales": round(sum(o["subtotal"] for o in orders)), "payout": round(sum(o["payout"] for o in orders)),
        "commission": round(sum(o["commission"] + o["commission_tax"] for o in orders)),
        "merchant_discount": round(sum(o["merchant_discount"] for o in orders)), "refunds": round(sum(o["adjustment"] for o in orders)),
        "ad_spend": round(sum(ad_spend.values())), "var_cost": round(var_cost),
        "actual_contribution": round(sum(o["payout"] for o in orders) - var_cost - sum(ad_spend.values())),
        "contribution_now": round(now["total"]), "contribution_next": round(nxt["total"]),
        "rate_change_impact": round(nxt["total"] - now["total"]),
        "direct_contribution": round(sum(q * (ctx["items"][n]["dine"] - ctx["items"][n]["food"]) for n, q in ctx["direct"].items())),
        "direct_sales": round(sum(q * ctx["items"][n]["dine"] for n, q in ctx["direct"].items())),
        "orders": len(orders), "by_platform_next": {p: round(v) for p, v in nxt["by_platform"].items()},
        "aov": {p: round(v) for p, v in a.items()},
    }
    det = {"loss_item": sorted(r["key"] for r in items if r["status"] == "loss"),
           "loss_promo": sorted(r["key"] for r in promos if r["status"] == "loss"),
           "loss_ad": sorted(r["key"] for r in ads if r["status"] == "loss"),
           "refund_hotspot": sorted(r["key"] for r in refunds if r["status"] == "hot")}
    return {"items": items, "promos": promos, "ads": ads, "refunds": refunds, "totals": totals, "detected": det}


def truth_issues(w):
    ctx = ctx_from_world(w)
    orders, ad_spend = w["orders"], w["ad_spend"]
    a = aov(orders)
    res = project(ctx, orders, ad_spend, [], TrueParams(w))
    sold = {(o["platform"], n) for o in orders for n, _, _ in o["lines"]}
    return {
        "loss_item": sorted(f"{p}|{n}" for (p, n) in sold if list_margin(ctx, p, n, a) < 0),
        "loss_promo": sorted(pid for p in ctx["plat"] for pid in ctx["plat"][p]["promos"] if res["by_promo"].get((p, pid), 0) < 0),
        "loss_ad": sorted(c for c, s in ad_spend.items() if res["by_ad"].get((c[0], c), 0) < s),
        "refund_hotspot": [w["hot_refund"]],
    }


# ------------------------------------------------------------------ 對照組：老闆現在怎麼看
def baseline_revenue_only(ctx, orders, ad_spend, k):
    """只看營收報表：把營收最低的當成問題。k＝各類真正問題的數量（對對照組很寬鬆的設定）。"""
    rev = defaultdict(float)
    for o in orders:
        for n, q, pr in o["lines"]:
            rev[f"{o['platform']}|{n}"] += q * pr
    prev, arev = defaultdict(float), defaultdict(float)
    for o in orders:
        if o["promo_id"]:
            prev[o["promo_id"]] += o["subtotal"]
        if o["ad_campaign"]:
            arev[o["ad_campaign"]] += o["subtotal"]
    low = lambda d, kk: sorted(sorted(d, key=lambda x: d[x])[:kk])
    return {"loss_item": low(rev, k["loss_item"]), "loss_promo": low(prev, k["loss_promo"]), "loss_ad": low(arev, k["loss_ad"]), "refund_hotspot": []}


def baseline_commission_only(ctx, orders):
    """只扣抽成：售價 ×(1−抽成) − 食材成本 < 0 才算賠錢。活動、廣告、退款沒有方法可判斷。"""
    sold = {(o["platform"], n) for o in orders for n, _, _ in o["lines"]}
    return {"loss_item": sorted(f"{p}|{n}" for (p, n) in sold if ctx["plat"][p]["prices"][n] * (1 - ctx["plat"][p]["rate_next"]) - ctx["items"][n]["food"] < 0),
            "loss_promo": [], "loss_ad": [], "refund_hotspot": []}


# ------------------------------------------------------------------ 建議動作
def _cap(ctx, p, n, cons):
    price = ctx["plat"][p]["prices"][n]
    cap = price * (1 + cons.get("max_raise_pct", 0.2))
    if n in cons.get("max_raise", {}):
        cap = min(cap, price + cons["max_raise"][n])
    return int(cap // PRICE_STEP) * PRICE_STEP


def validate_actions(ctx, actions, cons):
    """檢查動作有沒有違反老闆的限制。回傳違規清單（空的代表全部合規）。"""
    v = []
    for a in actions:
        t = a.get("type")
        if t == "delist" and a["item"] in cons.get("protected_items", []):
            v.append(f"{a['item']} 是不可下架的品項")
        if t == "set_price":
            if a["item"] not in ctx["plat"][a["platform"]]["prices"]:
                v.append(f"平台 {a['platform']} 沒有 {a['item']}")
            elif float(a["price"]) > _cap(ctx, a["platform"], a["item"], cons) + 1e-9:
                v.append(f"{a['platform']} {a['item']} 漲到 {a['price']} 超過上限 {_cap(ctx, a['platform'], a['item'], cons)}")
        if t == "end_promo" and a["promo_id"] in cons.get("locked_promos", []):
            v.append(f"活動 {a['promo_id']} 目前不能退出")
        if t == "quit_platform":
            v.append("退出整個平台不在允許的動作內")
    return v


def playbook(ctx, orders, ad_spend, cons, an=None, scope="problem"):
    """決定性的建議規則：預設只動「賠錢或做白工」的地方（scope="all" 則每個品項都在上限內試算），每個動作都先模擬過。"""
    an = an or analyze(ctx, orders, ad_spend)
    prm = EstParams(ctx, orders)
    base = project(ctx, orders, ad_spend, [], prm)["total"]
    a = aov(orders)
    acts, notes = [], []
    for r in an["promos"]:
        if r["status"] == "loss":
            if r["key"] in cons.get("locked_promos", []):
                notes.append({"kind": "locked_promo", "key": r["key"], "text": f"「{r['name']}」每單平均 {r['per_order']} 元，是賠的，但老闆說這個月不能退，到期後建議不要續約。"})
            else:
                acts.append({"type": "end_promo", "platform": r["platform"], "promo_id": r["key"],
                             "reason": f"「{r['name']}」上個月 {r['orders']} 單，扣掉折扣後每單平均實拿 {r['per_order']} 元，做越多賠越多。"})
    for r in an["ads"]:
        if r["status"] == "loss":
            acts.append({"type": "stop_ad", "platform": r["platform"], "campaign": r["key"],
                         "reason": f"廣告 {r['key']} 花了 {r['spend']:,} 元，帶來的 {r['orders']} 單總共只實拿 {r['contribution']:,} 元；就算這些單全是廣告帶來的也不划算。"})
        elif r["status"] == "doubt":
            notes.append({"kind": "ad_doubt", "key": r["key"], "text": f"廣告 {r['key']} 花 {r['spend']} 元、帶單實拿 {r['contribution']} 元：如果有一半的單本來就會來，就不划算。建議先把預算減半觀察兩週。"})
    for r in an["items"]:
        if r["status"] == "ok" and scope != "all":
            continue
        p, n = r["platform"], r["item"]
        cap = _cap(ctx, p, n, cons)
        best = None
        price = r["price"] + PRICE_STEP
        while price <= cap:
            d = delta(ctx, orders, ad_spend, [{"type": "set_price", "platform": p, "item": n, "price": price}], prm, base)
            if best is None or d > best[1]:
                best = (price, d)
            price += PRICE_STEP
        d_del = delta(ctx, orders, ad_spend, [{"type": "delist", "platform": p, "item": n}], prm, base)
        protected = n in cons.get("protected_items", [])
        if best and (list_margin(ctx, p, n, a, price=best[0]) >= 0 or protected or best[1] >= d_del) and best[1] > 0:
            m_new = list_margin(ctx, p, n, a, price=best[0])
            acts.append({"type": "set_price", "platform": p, "item": n, "price": best[0], "old_price": r["price"],
                         "reason": f"平台 {p} 的{n}：售價 {r['price']:.0f} 元，新費率下每賣一份實拿 {r['unit_next']} 元，上個月賣了 {r['qty']} 份。調到 {best[0]:.0f} 元後每份實拿 {m_new:.1f} 元。"})
        elif not protected and d_del > 0 and r["status"] != "ok":
            acts.append({"type": "delist", "platform": p, "item": n,
                         "reason": f"平台 {p} 的{n}：每賣一份實拿 {r['unit_next']} 元，漲到上限 {cap} 元仍然不划算，建議這個平台先下架、只做內用外帶。"})
    for r in an["refunds"]:
        if r["status"] == "hot":
            acts.append({"type": "fix_packaging", "item": r["item"],
                         "reason": f"{r['item']}有 {r['rate'] * 100:.1f}% 的訂單被退款（{r['refund_orders']}/{r['orders_with_item']}），上個月被扣 {r['amount']:,} 元。建議換防漏包材（每份多約 {FIX_COST} 元）。"})
    for x in acts:
        x["est_delta"] = round(delta(ctx, orders, ad_spend, [x], prm, base))
    for x in acts:
        if x["type"] == "fix_packaging" and x["est_delta"] <= 0:
            notes.append({"kind": "refund_no_fix", "key": x["item"], "text": x["reason"].split("建議")[0] + f"但試算後，每份多花 {FIX_COST} 元換包材省下的退款不夠補，先不建議換；可以先檢查封口方式，或這個品項外送時改成選配。"})
    acts = [x for x in acts if x["est_delta"] > 0]
    acts.sort(key=lambda x: -x["est_delta"])
    return {"actions": acts, "notes": notes, "est_total": round(delta(ctx, orders, ad_spend, acts, prm, base)), "base_next": round(base)}


def strategy_blanket(ctx, pct):
    return [{"type": "set_price", "platform": p, "item": n, "price": max(pr + PRICE_STEP, round(pr * (1 + pct) / 5) * 5)}
            for p in ctx["plat"] for n, pr in ctx["plat"][p]["prices"].items()]


def strategy_breakeven(ctx):
    """網路流傳的打平算法：平台售價 = 店內價 ÷ (1 − 抽成)。"""
    return [{"type": "set_price", "platform": p, "item": n, "price": max(pr, round(ctx["items"][n]["dine"] / (1 - ctx["plat"][p]["rate_next"]) / 5) * 5)}
            for p in ctx["plat"] for n, pr in ctx["plat"][p]["prices"].items()]


def strategy_quit_worse(ctx, orders, ad_spend):
    res = project(ctx, orders, ad_spend, [], EstParams(ctx, orders))
    net = {p: res["by_platform"][p] - sum(s for c, s in ad_spend.items() if c[0] == p) for p in ctx["plat"]}
    return [{"type": "quit_platform", "platform": min(net, key=net.get)}]


def change_list(ctx, actions):
    """把動作轉成「照著改後台」的變更清單。"""
    out = defaultdict(list)
    for a in actions:
        t = a["type"]
        if t == "set_price":
            old = a.get("old_price", ctx["plat"][a["platform"]]["prices"][a["item"]])
            out[a["platform"]].append(f"菜單 → {a['item']}：售價 {old:.0f} → {float(a['price']):.0f} 元")
        elif t == "delist":
            out[a["platform"]].append(f"菜單 → {a['item']}：設為「暫停供應」")
        elif t == "end_promo":
            out[a["platform"]].append(f"行銷活動 → 結束「{ctx['plat'][a['platform']]['promos'][a['promo_id']]['name']}」（{a['promo_id']}）")
        elif t == "stop_ad":
            out[a["platform"]].append(f"廣告 → 停止 {a['campaign']}")
        elif t == "fix_packaging":
            out["店內"].append(f"包材 → {a['item']} 改用防漏碗蓋並加封膜")
    return {k: v for k, v in out.items()}


def describe_action(ctx, an, a, orders):
    """用分析結果替一個動作產生白話說明（數字全部來自計算結果）。"""
    t = a["type"]
    if t in ("set_price", "delist"):
        r = next((x for x in an["items"] if x["platform"] == a["platform"] and x["item"] == a["item"]), None)
        if not r:
            return ""
        if t == "delist":
            return f"平台 {a['platform']} 的{a['item']}：新費率下每賣一份實拿 {r['unit_next']} 元，上個月賣了 {r['qty']} 份。這個平台先下架，只做內用外帶。"
        m_new = list_margin(ctx, a["platform"], a["item"], aov(orders), price=float(a["price"]))
        return f"平台 {a['platform']} 的{a['item']}：售價 {r['price']:.0f} 元，新費率下每賣一份實拿 {r['unit_next']} 元，上個月賣了 {r['qty']} 份。調到 {float(a['price']):.0f} 元後每份實拿 {m_new:.1f} 元。"
    if t == "end_promo":
        r = next((x for x in an["promos"] if x["key"] == a["promo_id"]), None)
        return f"「{r['name']}」上個月 {r['orders']} 單，扣掉折扣後每單平均實拿 {r['per_order']} 元。" if r else ""
    if t == "stop_ad":
        r = next((x for x in an["ads"] if x["key"] == a["campaign"]), None)
        return f"廣告 {r['key']} 花了 {r['spend']:,} 元，帶來的 {r['orders']} 單總共實拿 {r['contribution']:,} 元。" if r else ""
    if t == "fix_packaging":
        r = next((x for x in an["refunds"] if x["item"] == a["item"]), None)
        return f"{r['item']}有 {r['rate'] * 100:.1f}% 的訂單被退款（{r['refund_orders']}/{r['orders_with_item']}），上個月被扣 {r['amount']:,} 元。換防漏包材每份多約 {FIX_COST} 元。" if r else ""
    return ""
