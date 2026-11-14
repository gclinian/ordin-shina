"""模擬店家產生器。

產生一家擬真的小店：菜單與成本、兩個外送平台一個月的對帳單（兩種不同格式）、
促銷活動、廣告、退款，以及「正解」（預先埋好的問題與 agent 看不到的隱藏參數）。

全部是模擬資料，不是任何真實平台的匯出檔。欄位設計參考平台公開說明的費用項目。
"""
import csv
import json
import os
import random

ARCH = {
    "便當": {
        "shop": "小滿便當",
        "items": [
            ("排骨便當", "main", 110), ("雞腿便當", "main", 125), ("控肉便當", "main", 105),
            ("魚排便當", "main", 100), ("素食便當", "main", 90), ("雙主菜便當", "main", 150),
            ("滷雞腿", "side", 60), ("滷蛋", "side", 15), ("燙青菜", "side", 40),
            ("貢丸湯", "soup", 35), ("味噌湯", "soup", 25), ("古早味紅茶", "drink", 20),
        ],
    },
    "麵食": {
        "shop": "巷仔內麵館",
        "items": [
            ("紅燒牛肉麵", "main", 160), ("牛肉湯麵", "main", 110), ("榨菜肉絲麵", "main", 80),
            ("麻醬麵", "main", 60), ("紅油抄手", "main", 75), ("招牌水餃", "main", 70),
            ("滷味拼盤", "side", 90), ("皮蛋豆腐", "side", 45), ("燙青菜", "side", 40),
            ("酸辣湯", "soup", 45), ("蛋花湯", "soup", 30), ("酸梅湯", "drink", 25),
        ],
    },
    "早午餐": {
        "shop": "晨光早午餐",
        "items": [
            ("總匯三明治", "main", 85), ("豬排起司堡", "main", 75), ("鐵板麵加蛋", "main", 65),
            ("法式吐司", "main", 75), ("鮪魚蛋吐司", "main", 50), ("起司蛋餅", "main", 45),
            ("蘿蔔糕", "side", 40), ("薯餅", "side", 25), ("雞塊", "side", 45),
            ("玉米濃湯", "soup", 35), ("大冰奶", "drink", 30), ("美式咖啡", "drink", 45),
        ],
    },
}
FOOD_RATIO = {"main": (0.36, 0.46), "side": (0.28, 0.40), "soup": (0.25, 0.35), "drink": (0.15, 0.28)}
PACK = {"main": (7, 9), "side": (3, 4), "soup": (6, 8), "drink": (3, 4)}
ELAS = {"main": (0.8, 1.8), "side": (1.2, 2.5), "soup": (1.2, 2.5), "drink": (1.5, 3.0)}
TAX = 0.05
MONTH = (2026, 10, 31)
FIX_COST = 3  # 改用防漏包材，每份多 3 元


def r5(x):
    return int(round(x / 5.0)) * 5


def list_margin(price, rate, food, pack, bag, aov):
    """牌價下、不含促銷與退款的每份邊際貢獻。"""
    return price * (1 - rate * (1 + TAX)) - food - pack - bag * price / aov


def make_world(seed, arch=None):
    rnd = random.Random(seed)
    arch = arch or list(ARCH)[seed % 3]
    spec = ARCH[arch]
    bag = rnd.choice([4, 5])
    items = {}
    for name, cat, price in spec["items"]:
        lo, hi = FOOD_RATIO[cat]
        items[name] = {
            "cat": cat, "dine": price, "food": round(price * rnd.uniform(lo, hi)),
            "pack": rnd.randint(*PACK[cat]), "pop": rnd.uniform(0.5, 1.5),
        }
    mains = [n for n in items if items[n]["cat"] == "main"]
    others = [n for n in items if items[n]["cat"] != "main"]
    # 讓前兩個主餐特別熱賣
    hot = rnd.sample(mains, 2)
    for h in hot:
        items[h]["pop"] = rnd.uniform(2.0, 3.0)

    rate_a = rnd.choice([0.30, 0.32, 0.325])
    plats = {
        "A": {"label": "平台 A（熊貓型・模擬）", "rate": rate_a, "rate_next": min(0.35, rate_a + 0.025),
              "next_effective": "2026-11-01", "base": "net", "tax": TAX, "fmt": 0},
        "B": {"label": "平台 B（UE 型・模擬）", "rate": rnd.choice([0.325, 0.35]), "base": "gross",
              "tax": TAX, "fmt": 1},
    }
    plats["B"]["rate_next"] = plats["B"]["rate"]
    plats["B"]["next_effective"] = None

    # ---- 埋入賠錢品項：食材成本高、平台售價沒有加價 ----
    n_loss = rnd.randint(2, 4)
    cand = [(p, i) for p in "AB" for i in mains] + [(p, i) for p in "AB" for i in rnd.sample(others, 2)]
    planted = set()
    planted.add((rnd.choice("AB"), hot[0]))  # 至少一個熱賣品項
    while len(planted) < n_loss:
        planted.add(rnd.choice(cand))
    aov_est = 2.0 * sum(items[m]["dine"] for m in mains) / len(mains)
    for p in "AB":
        mk = rnd.uniform(1.10, 1.25)
        plats[p]["prices"] = {n: r5(items[n]["dine"] * mk) for n in items}
    for (p, i) in sorted(planted):  # 排序以確保跨程序可重現
        plats[p]["prices"][i] = items[i]["dine"]
        items[i]["food"] = max(items[i]["food"], round(items[i]["dine"] * rnd.uniform(0.50, 0.60)))
    for (p, i) in sorted(planted):  # 排序以確保跨程序可重現
        while list_margin(plats[p]["prices"][i], plats[p]["rate_next"], items[i]["food"], items[i]["pack"], bag, aov_est) > -2:
            items[i]["food"] += 1
    for p in "AB":
        for n in items:
            if (p, n) in planted:
                continue
            while list_margin(plats[p]["prices"][n], plats[p]["rate_next"], items[n]["food"], items[n]["pack"], bag, aov_est) < 4:
                plats[p]["prices"][n] += 5

    # ---- 隱藏參數（agent 看不到）----
    hidden = {"eps": {}, "sigma": {}, "refund": {}, "promo_uplift": {}, "ad_alpha": {}}
    for p in "AB":
        for n in items:
            hidden["eps"][f"{p}|{n}"] = round(rnd.uniform(*ELAS[items[n]["cat"]]), 2)
            hidden["sigma"][f"{p}|{n}"] = round(rnd.uniform(0.2, 0.5), 2)
    hot_refund = rnd.choice([n for n in items if items[n]["cat"] == "soup"] + [m for m in mains if "麵" in m or "湯" in m][:1])
    items[hot_refund]["pop"] = max(items[hot_refund]["pop"], 1.4)
    for n in items:
        hidden["refund"][n] = 0.004
    hidden["refund"][hot_refund] = round(rnd.uniform(0.09, 0.15), 3)
    hidden["fix_eff"] = round(rnd.uniform(0.5, 0.9), 2)

    # ---- 促銷活動 ----
    def promo_bogo(p, pid):
        it = rnd.choice(mains)
        return {"id": pid, "name": f"{it} 買一送一", "type": "item_pct", "item": it, "pct": 0.5, "share": 1.0, "bogo": True}

    def promo_deep(p, pid):
        it = rnd.choice(mains)
        return {"id": pid, "name": f"{it} 6 折", "type": "item_pct", "item": it, "pct": 0.4, "share": 1.0}

    def promo_drink(p, pid):
        it = rnd.choice([n for n in items if items[n]["cat"] == "drink"])
        return {"id": pid, "name": f"{it} 8 折", "type": "item_pct", "item": it, "pct": 0.2, "share": 1.0}

    def promo_cofund(p, pid):
        return {"id": pid, "name": "滿 300 折 30（平台共同負擔）", "type": "order_amount", "threshold": 300, "amount": 30, "share": 0.5}

    def promo_gray(p, pid):
        return {"id": pid, "name": "滿 199 折 40（店家全額負擔）", "type": "order_amount", "threshold": 199, "amount": 40, "share": 1.0}

    bad = [promo_bogo, promo_deep]
    good = [promo_drink, promo_cofund, promo_gray]
    rnd.shuffle(bad)
    bad_plat = rnd.choice("AB")
    for p in "AB":
        makers = []
        if p == bad_plat:
            makers.append(bad[0])
            if rnd.random() < 0.4:
                makers.append(bad[1])
        elif rnd.random() < 0.3:
            makers.append(bad[1])
        makers += rnd.sample(good, rnd.randint(1, 2))
        plats[p]["promos"] = {}
        for k, mk in enumerate(makers):
            pm = mk(p, f"{p}-PROMO{k + 1}")
            plats[p]["promos"][pm["id"]] = pm
            hidden["promo_uplift"][pm["id"]] = round(rnd.uniform(0.3, 1.0), 2)

    # ---- 產生訂單 ----
    def pick(pool, k=1, exclude=()):
        pool = [n for n in pool if n not in exclude]
        ws = [items[n]["pop"] for n in pool]
        out = []
        for _ in range(k):
            if not pool:
                break
            c = rnd.choices(pool, ws)[0]
            j = pool.index(c)
            pool.pop(j)
            ws.pop(j)
            out.append(c)
        return out

    def new_basket(p, must=None, must_qty=None, min_sub=0):
        for _ in range(200):
            n_lines = rnd.choices([1, 2, 3], [0.35, 0.40, 0.25])[0]
            first = must if (must and items[must]["cat"] == "main") else pick(mains)[0]
            names = [first] + pick(others, n_lines - 1)
            if must and must not in names:
                names.append(must)
            lines = [[n, 2 if rnd.random() < 0.15 else 1, plats[p]["prices"][n]] for n in names]
            if must and must_qty:
                for ln in lines:
                    if ln[0] == must:
                        ln[1] = must_qty
            if sum(q * pr for _, q, pr in lines) >= min_sub:
                return lines
        return lines

    orders = []
    oid = [0]

    def add_order(p, lines, promo=None, inc_promo=False, ad=None, inc_ad=False):
        oid[0] += 1
        day = rnd.randint(1, MONTH[2])
        o = {"platform": p, "order_id": f"{p}{seed:02d}-{oid[0]:05d}", "date": f"{MONTH[0]}-{MONTH[1]:02d}-{day:02d}",
             "lines": lines, "promo_id": promo, "inc_promo": inc_promo, "ad_campaign": ad, "inc_ad": inc_ad}
        orders.append(o)
        return o

    for p in "AB":
        n_org = rnd.randint(600, 1000) if p == "A" else rnd.randint(400, 800)
        organic = [add_order(p, new_basket(p)) for _ in range(n_org)]
        for pid, pm in plats[p]["promos"].items():
            u = hidden["promo_uplift"][pid]
            if pm["type"] == "item_pct":
                q = [o for o in organic if not o["promo_id"] and any(l[0] == pm["item"] for l in o["lines"])]
                take = [o for o in q if rnd.random() < (0.6 if pm.get("bogo") else 0.95)]
                for o in take:
                    o["promo_id"] = pid
                    if pm.get("bogo"):
                        for l in o["lines"]:
                            if l[0] == pm["item"]:
                                l[1] = 2
                for _ in range(int(round(u * len(take)))):
                    add_order(p, new_basket(p, must=pm["item"], must_qty=2 if pm.get("bogo") else None), promo=pid, inc_promo=True)
            else:
                q = [o for o in organic if not o["promo_id"] and sum(l[1] * l[2] for l in o["lines"]) >= pm["threshold"]]
                take = [o for o in q if rnd.random() < 0.9]
                for o in take:
                    o["promo_id"] = pid
                for _ in range(int(round(u * len(take)))):
                    add_order(p, new_basket(p, min_sub=pm["threshold"]), promo=pid, inc_promo=True)

    # ---- 廣告 ----
    plats["A"]["ads_spec"] = ["A-AD1", "A-AD2"] if rnd.random() < 0.5 else ["A-AD1"]
    plats["B"]["ads_spec"] = ["B-AD1"]
    all_camps = plats["A"]["ads_spec"] + plats["B"]["ads_spec"]
    bad_ads = set(rnd.sample(all_camps, 1 if rnd.random() < 0.8 else 2))
    for p in "AB":
        porders = [o for o in orders if o["platform"] == p and not o["ad_campaign"]]
        for camp in plats[p]["ads_spec"]:
            alpha = round(rnd.uniform(0.3, 0.7), 2)
            hidden["ad_alpha"][camp] = alpha
            free = [o for o in porders if not o["ad_campaign"]]
            non_inc = rnd.sample(free, int(len(free) * rnd.uniform(0.06, 0.14)))
            for o in non_inc:
                o["ad_campaign"] = camp
            n_inc = int(round(len(non_inc) * alpha / (1 - alpha)))
            for _ in range(n_inc):
                add_order(p, new_basket(p), ad=camp, inc_ad=True)

    # ---- 退款、對帳金額 ----
    for o in orders:
        pl = plats[o["platform"]]
        sub = sum(q * pr for _, q, pr in o["lines"])
        disc = pdisc = 0
        if o["promo_id"]:
            pm = pl["promos"][o["promo_id"]]
            if pm["type"] == "item_pct":
                full = sum(q * pr for n, q, pr in o["lines"] if n == pm["item"]) * pm["pct"]
            else:
                full = pm["amount"]
            disc = int(round(full * pm["share"]))
            pdisc = int(round(full - disc))
        base = sub - disc if pl["base"] == "net" else sub
        com = int(round(pl["rate"] * base))
        tax = int(round(com * TAX))
        adj, ritem = 0, ""
        for n, q, pr in o["lines"]:
            if rnd.random() < hidden["refund"][n]:
                adj, ritem = q * pr, n
                break
        o.update(subtotal=sub, merchant_discount=disc, platform_discount=pdisc, commission=com, commission_tax=tax,
                 adjustment=adj, refund_item=ritem, payout=sub - disc - com - tax - adj)

    world = {"seed": seed, "arch": arch, "shop": spec["shop"] + "（模擬店家）", "items": items, "bag": bag,
             "plats": plats, "orders": orders, "hidden": hidden, "planted_items": sorted(planted),
             "hot_refund": hot_refund, "bad_ads": sorted(bad_ads)}

    # ---- 廣告花費：依實際貢獻決定，讓「賠錢廣告」與「划算廣告」界線清楚 ----
    from . import core  # 延後匯入避免循環
    ctx = core.ctx_from_world(world)
    res = core.project(ctx, orders, {}, [], core.TrueParams(world))
    world["ad_spend"] = {}
    for p in "AB":
        for camp in plats[p]["ads_spec"]:
            contrib = res["by_ad"].get((p, camp), 0.0)
            n = sum(1 for o in orders if o["ad_campaign"] == camp)
            if camp in bad_ads:
                spend = max(contrib, n * 6.0) * rnd.uniform(1.3, 2.0)
            else:
                spend = max(60.0, contrib * hidden["ad_alpha"][camp] * rnd.uniform(0.3, 0.6))
            world["ad_spend"][camp] = int(round(spend))

    # ---- 內用／外帶銷量（POS 摘要）----
    world["direct"] = {n: int(items[n]["pop"] * rnd.uniform(250, 500)) for n in items}

    # ---- 老闆的限制 ----
    protected = hot[0]
    locked = rnd.choice(sorted(plats["A"]["promos"]))
    world["constraints"] = {"protected_items": [protected], "max_raise": {protected: 10}, "max_raise_pct": 0.2,
                            "locked_promos": [locked]}
    world["owner_note"] = (
        f"老闆備註：\n1. {protected}是我們的招牌，不能下架，要漲的話最多漲 10 元。\n"
        f"2. 平台 A 的「{plats['A']['promos'][locked]['name']}」（活動代碼 {locked}）是跟業務簽好的，這個月還不能退。\n"
        "3. 其他品項可以調，但單一品項不要一次漲超過兩成，客人會嚇到。\n")
    world["truth"] = core.truth_issues(world)
    return world


# ------------------------------------------------------------------ 匯出 CSV
# 每個標準欄位有三種表頭寫法：0＝平台 A 格式、1＝平台 B 格式、2＝沒看過的新格式
HEADERS = {
    "orders": {
        "order_id": ["訂單編號", "order_id", "單號"],
        "date": ["訂單日期", "order_date", "成立時間"],
        "subtotal": ["商品小計", "sales_incl_tax", "餐點金額(含稅)"],
        "merchant_discount": ["店家負擔折扣", "promo_merchant_funded", "商家補貼優惠"],
        "platform_discount": ["平台負擔折扣", "promo_platform_funded", "平台補貼優惠"],
        "promo_id": ["活動代碼", "promo_code", "優惠方案"],
        "commission": ["佣金", "marketplace_fee", "服務費"],
        "commission_tax": ["佣金營業稅", "marketplace_fee_tax", "服務費稅額"],
        "ad_campaign": ["廣告活動", "ad_campaign", "推廣來源"],
        "adjustment": ["調整金額", "order_error_adjustment", "客訴扣款"],
        "refund_item": ["調整說明品項", "error_item", "問題餐點"],
        "payout": ["撥款金額", "total_payout", "實際入帳"],
    },
    "lines": {
        "order_id": ["訂單編號", "order_id", "單號"],
        "item": ["品項", "item_name", "餐點名稱"],
        "qty": ["數量", "quantity", "份數"],
        "unit_price": ["單價", "price", "售價"],
    },
    "ads": {
        "date": ["日期", "date", "扣款日"],
        "campaign": ["廣告活動", "campaign_name", "推廣活動"],
        "spend": ["花費", "ad_spend", "推廣費"],
    },
}
NEG_DEDUCT = {0: False, 1: True, 2: True}  # 扣款是否以負數呈現
DATE_FMT = {0: "{y}-{m:02d}-{d:02d}", 1: "{m:02d}/{d:02d}/{y}", 2: "{y}/{m}/{d}"}
DEDUCT = {"merchant_discount", "commission", "commission_tax", "adjustment", "spend"}


def _w(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def export(world, out_dir, fmt_override=None):
    """把模擬店家寫成老闆手上會有的檔案。fmt_override 例如 {"B": 2} 可讓某平台改用沒看過的表頭。"""
    os.makedirs(os.path.join(out_dir, "_truth"), exist_ok=True)
    items, plats = world["items"], world["plats"]
    _w(os.path.join(out_dir, "menu_costs.csv"), ["品項", "類別", "內用價", "食材成本", "外送包材成本"],
       [[n, v["cat"], v["dine"], v["food"], v["pack"]] for n, v in items.items()])
    _w(os.path.join(out_dir, "platform_menu.csv"), ["平台", "品項", "平台售價"],
       [[p, n, plats[p]["prices"][n]] for p in "AB" for n in items])
    _w(os.path.join(out_dir, "promos.csv"), ["平台", "活動代碼", "活動名稱", "類型", "指定品項", "折扣比例", "門檻", "折抵金額", "店家負擔比例", "買一送一"],
       [[p, pid, pm["name"], pm["type"], pm.get("item", ""), pm.get("pct", ""), pm.get("threshold", ""), pm.get("amount", ""), pm["share"], 1 if pm.get("bogo") else ""]
        for p in "AB" for pid, pm in plats[p]["promos"].items()])
    _w(os.path.join(out_dir, "pos_direct.csv"), ["品項", "內用外帶銷量"], [[n, q] for n, q in world["direct"].items()])
    contract = {"bag_cost": world["bag"], "shop": world["shop"], "month": "2026-10"}
    for p in "AB":
        contract[p] = {k: plats[p][k] for k in ("label", "rate", "rate_next", "next_effective", "base", "tax")}
    with open(os.path.join(out_dir, "contract.json"), "w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out_dir, "owner_note.txt"), "w", encoding="utf-8") as f:
        f.write(world["owner_note"])
    rnd = random.Random(world["seed"] + 999)
    fmts = {}
    for p in "AB":
        fmt = (fmt_override or {}).get(p, plats[p]["fmt"])
        fmts[p] = fmt
        sign = -1 if NEG_DEDUCT[fmt] else 1

        def dt(s):
            y, m, d = map(int, s.split("-"))
            return DATE_FMT[fmt].format(y=y, m=m, d=d)

        cols = list(HEADERS["orders"])
        rows = []
        for o in world["orders"]:
            if o["platform"] != p:
                continue
            row = []
            for c in cols:
                v = o[c] if c != "date" else dt(o["date"])
                if c in DEDUCT:
                    v = sign * v
                row.append("" if v is None else v)
            rows.append(row)
        _w(os.path.join(out_dir, f"{p}_orders.csv"), [HEADERS["orders"][c][fmt] for c in cols], rows)
        _w(os.path.join(out_dir, f"{p}_items.csv"), [HEADERS["lines"][c][fmt] for c in HEADERS["lines"]],
           [[o["order_id"], n, q, pr] for o in world["orders"] if o["platform"] == p for n, q, pr in o["lines"]])
        ad_rows = []
        for camp in plats[p]["ads_spec"]:
            total = world["ad_spend"][camp]
            days = MONTH[2]
            per = [total // days] * days
            per[-1] += total - sum(per)
            rnd.shuffle(per)
            for d, s in enumerate(per, 1):
                ad_rows.append([dt(f"2026-10-{d:02d}"), camp, sign * s])
        _w(os.path.join(out_dir, f"{p}_ads.csv"), [HEADERS["ads"][c][fmt] for c in HEADERS["ads"]], ad_rows)
    truth = {k: world[k] for k in ("seed", "arch", "shop", "hidden", "planted_items", "hot_refund", "bad_ads", "constraints", "truth")}
    truth["formats"] = fmts
    truth["order_flags"] = {o["order_id"]: [int(o["inc_promo"]), int(o["inc_ad"])] for o in world["orders"]}
    with open(os.path.join(out_dir, "_truth", "world.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False)
    return fmts
