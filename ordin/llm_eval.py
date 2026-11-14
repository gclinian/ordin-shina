"""用真的 LLM agent 在多家模擬店家上端到端跑一遍並評分。

每家店的平台 B 對帳單都改用「沒看過的表頭」，內建欄位對應讀不了，一定要 LLM 自己對欄位。
輸出：results/llm_eval.json、recordings/eval_seed<N>.live.json
"""
import json
import os
import statistics as st
import tempfile
from concurrent.futures import ThreadPoolExecutor

from . import agent, world


def one(seed):
    d = tempfile.mkdtemp(prefix=f"ordin_eval_{seed}_")
    world.export(world.make_world(seed), d, fmt_override={"B": 2})
    scripted_ok = agent.run_scripted(d)["ok"]
    try:
        r = agent.run_live(d)
        m = agent.score(d, r)
        json.dump(r, open(f"recordings/eval_seed{seed}.live.json", "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as ex:
        m = {"completed": False, "error": f"{type(ex).__name__}: {ex}"}
    m.update(seed=seed, scripted_mode_could_read=scripted_ok)
    print(json.dumps(m, ensure_ascii=False), flush=True)
    return m


def main(seeds):
    os.makedirs("recordings", exist_ok=True)
    os.makedirs("results", exist_ok=True)
    with ThreadPoolExecutor(max_workers=3) as ex:
        rows = list(ex.map(one, seeds))
    done = [r for r in rows if r.get("completed")]
    summ = {"n": len(rows), "completed": len(done), "backend": agent.backend()[0], "model": agent.MODEL_CLI if agent.backend()[0] == "cli" else agent.MODEL_API,
            "scripted_mode_could_read": sum(r["scripted_mode_could_read"] for r in rows)}
    if done:
        summ.update(
            ingest_ok=sum(r["ingest_ok"] for r in done), constraints_correct=sum(r["constraints_correct"] for r in done),
            violations=sum(r["violations_vs_truth"] for r in done), issues_total=sum(r["issues_total"] for r in done),
            issues_handled=sum(r["issues_handled"] for r in done), true_delta_mean=round(st.mean(r["true_delta"] for r in done)),
            rule_playbook_true_delta_mean=round(st.mean(r["rule_playbook_true_delta"] for r in done)),
            report_numbers=sum(r["report_numbers"] for r in done), report_numbers_traceable=sum(r["report_numbers_traceable"] for r in done),
            steps_mean=round(st.mean(r["steps"] for r in done), 1), seconds_mean=round(st.mean(r["seconds"] for r in done)))
    json.dump({"summary": summ, "rows": rows}, open("results/llm_eval.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False, indent=1))
