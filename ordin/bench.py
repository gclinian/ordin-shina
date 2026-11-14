"""驗證：在埋了已知問題的模擬店家上，量找出率與決策效果。

用法：python3 -m ordin.bench [店家數，預設 30]
輸出：results/benchmark.json、results/benchmark.md
"""
import json
import os
import statistics as st
import sys
import tempfile
from collections import defaultdict

from . import core, world

CLASSES = ["loss_item", "loss_promo", "loss_ad", "refund_hotspot"]
CLASS_ZH = {"loss_item": "賠錢品項", "loss_promo": "賠錢活動", "loss_ad": "賠錢廣告", "refund_hotspot": "退款熱點"}
SCEN = {"低彈性（×0.5）": 0.5, "基準": 1.0, "高彈性（×1.5）": 1.5}


def run(n=30, out_dir="results"):
    det = {m: {c: {"tp": 0, "fp": 0, "fn": 0} for c in CLASSES} for m in ("engine", "revenue_only", "commission_only")}
    truth_n = defaultdict(int)
    dec = {s: defaultdict(list) for s in SCEN}
    calib, viol, shops = [], defaultdict(int), []
    recon_ok = 0
    for seed in range(1, n + 1):
        w = world.make_world(seed)
        d = tempfile.mkdtemp()
        world.export(w, d)
        ctx = core.load_shop(d)
        orders, ads = [], {}
        ok = True
        for p in "AB":
            o, a, rep = core.ingest(d, p, core.KNOWN_MAPPINGS[p], ctx)
            ok = ok and rep["ok"] and rep["payout_match"] == 1.0
            orders += o
            ads.update(a)
        recon_ok += ok
        an = core.analyze(ctx, orders, ads)
        t = w["truth"]
        k = {c: len(t[c]) for c in CLASSES}
        found = {"engine": an["detected"], "revenue_only": core.baseline_revenue_only(ctx, orders, ads, k),
                 "commission_only": core.baseline_commission_only(ctx, orders)}
        for c in CLASSES:
            truth_n[c] += len(t[c])
            for m, f in found.items():
                det[m][c]["tp"] += len(set(f[c]) & set(t[c]))
                det[m][c]["fp"] += len(set(f[c]) - set(t[c]))
                det[m][c]["fn"] += len(set(t[c]) - set(f[c]))
        pb = core.playbook(ctx, orders, ads, w["constraints"], an)
        pb_all = core.playbook(ctx, orders, ads, w["constraints"], an, scope="all")
        strategies = {
            "agent": pb["actions"], "agent_all": pb_all["actions"],
            "blanket10": core.strategy_blanket(ctx, 0.10), "breakeven": core.strategy_breakeven(ctx),
            "quit_worse": core.strategy_quit_worse(ctx, orders, ads),
        }
        for name, acts in strategies.items():
            viol[name] += len(core.validate_actions(ctx, acts, w["constraints"]))
        row = {"seed": seed, "arch": w["arch"], "sales": an["totals"]["sales"], "issues": sum(k.values()), "n_actions": len(pb["actions"]), "est": pb["est_total"]}
        for sname, mult in SCEN.items():
            tp = core.TrueParams(w, eps_mult=mult)
            base = core.project(ctx, w["orders"], w["ad_spend"], [], tp)["total"]
            dec[sname]["base"].append(base)
            for name, acts in strategies.items():
                dec[sname][name].append(core.delta(ctx, w["orders"], w["ad_spend"], acts, tp, base))
            if mult == 1.0:
                row["base"] = round(base)
                row["real"] = round(dec[sname]["agent"][-1])
                calib.append((pb["est_total"], dec[sname]["agent"][-1]))
        tp1 = core.TrueParams(w)
        row["rate_impact"] = round(core.project(ctx, w["orders"], w["ad_spend"], [], tp1)["total"] - core.project(ctx, w["orders"], w["ad_spend"], [], tp1, rate_key="rate")["total"])
        shops.append(row)

    def prf(x):
        tp, fp, fn = x["tp"], x["fp"], x["fn"]
        return {"tp": tp, "fp": fp, "fn": fn, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
                "precision": round(tp / (tp + fp), 3) if tp + fp else None}

    res = {"n_shops": n, "reconciliation_ok": recon_ok, "truth_counts": dict(truth_n),
           "detection": {m: {c: prf(det[m][c]) for c in CLASSES} for m in det}, "decision": {}, "violations": dict(viol), "shops": shops}
    for m in det:
        tot = {k2: sum(det[m][c][k2] for c in CLASSES) for k2 in ("tp", "fp", "fn")}
        res["detection"][m]["all"] = prf(tot)
    for sname in SCEN:
        dd = dec[sname]
        r = {"base_mean": round(st.mean(dd["base"]))}
        for name in ("agent", "agent_all", "blanket10", "breakeven", "quit_worse"):
            xs = dd[name]
            r[name] = {"mean": round(st.mean(xs)), "median": round(st.median(xs)), "min": round(min(xs)), "max": round(max(xs)),
                       "share_positive": round(sum(x > 0 for x in xs) / n, 3),
                       "agent_wins": round(sum(a >= x for a, x in zip(dd["agent"], xs)) / n, 3)}
        res["decision"][sname] = r
    res["sales_mean"] = round(st.mean(s["sales"] for s in shops))
    res["rate_impact_mean"] = round(st.mean(s["rate_impact"] for s in shops))
    res["calibration"] = {"est_mean": round(st.mean(e for e, _ in calib)), "real_mean": round(st.mean(r for _, r in calib)),
                          "mean_abs_pct_err": round(st.mean(abs(e - r) / abs(r) for e, r in calib if r), 3)}
    os.makedirs(out_dir, exist_ok=True)
    json.dump(res, open(os.path.join(out_dir, "benchmark.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(out_dir, "benchmark.md"), "w", encoding="utf-8").write(to_md(res))
    return res


def to_md(r):
    L = [f"# 驗證結果（{r['n_shops']} 家模擬店家，全部為模擬資料）", "",
         f"- 對帳硬檢查（逐筆重算撥款金額與對帳單 100% 一致）：{r['reconciliation_ok']}/{r['n_shops']} 家通過",
         f"- 平均每家外送月營收（平台售價計）：{r['sales_mean']:,} 元；平台 A 新費率對每家每月的影響：{r['rate_impact_mean']:,} 元", "",
         "## 1. 找出率（正解由資料產生器決定）", "", "| 問題類型 | 正解數 | 本系統 找出率／精確率 | 只看營收報表 找出率 | 只扣抽成 找出率 |", "|---|---|---|---|---|"]
    d = r["detection"]
    f = lambda v: "—" if v is None else f"{v * 100:.0f}%"
    for c in CLASSES + ["all"]:
        n = r["truth_counts"].get(c, sum(r["truth_counts"].values()))
        L.append(f"| {CLASS_ZH.get(c, '合計')} | {n} | {f(d['engine'][c]['recall'])}／{f(d['engine'][c]['precision'])} | {f(d['revenue_only'][c]['recall'])} | {f(d['commission_only'][c]['recall'])} |")
    L += ["", "## 2. 決策效果：下個月外送邊際貢獻的變化（元／月，每家平均；由隱藏真實參數的模擬器評分）", ""]
    names = {"agent": "本系統（只動有問題的地方）", "agent_all": "本系統（全品項在上限內試算）", "blanket10": "全面漲 10%", "breakeven": "全面漲到打平價（店內價÷(1−抽成)）", "quit_worse": "退出較差的平台"}
    for s, x in r["decision"].items():
        L += [f"### {s}（不動作時每家平均 {x['base_mean']:,} 元／月）", "", "| 做法 | 平均 | 中位數 | 最差 | 有改善的店家比例 | 本系統不輸它的比例 |", "|---|---|---|---|---|---|"]
        for k, zh in names.items():
            v = x[k]
            L.append(f"| {zh} | {v['mean']:+,} | {v['median']:+,} | {v['min']:+,} | {v['share_positive'] * 100:.0f}% | {v['agent_wins'] * 100:.0f}% |")
        L.append("")
    c = r["calibration"]
    L += ["## 3. 預估準不準（基準情境）", "", f"- 本系統事前預估平均 {c['est_mean']:+,} 元／月；模擬器實際結果平均 {c['real_mean']:+,} 元／月；平均絕對誤差 {c['mean_abs_pct_err'] * 100:.0f}%", "",
          "## 4. 違反老闆限制的動作數（30 家合計）", ""]
    for k, zh in names.items():
        L.append(f"- {zh}：{r['violations'][k]}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(to_md(run(n)))
