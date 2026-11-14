"""單元測試：用手算得出來的小例子確認錢沒有算錯。執行：python3 run.py test"""
import os
import shutil
import sys
import tempfile
import unittest

import warnings

warnings.simplefilter("ignore", ResourceWarning)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ordin import agent, core, world  # noqa: E402


def tiny_ctx():
    return {"shop": "t", "items": {"便當": {"cat": "main", "dine": 100, "food": 40, "pack": 8}, "湯": {"cat": "soup", "dine": 30, "food": 10, "pack": 6}},
            "bag": 4, "direct": {},
            "plat": {"A": {"rate": 0.30, "rate_next": 0.35, "base": "net", "tax": 0.05, "prices": {"便當": 120, "湯": 40},
                           "promos": {"P1": {"id": "P1", "name": "便當 5 折", "type": "item_pct", "item": "便當", "pct": 0.5, "share": 1.0}}}}}


class Zero:  # 不退款、彈性 1 的簡單參數
    def eps(self, p, n): return 1.0
    def keep_promo(self, o): return 0.5
    def keep_ad(self, o): return 0.5
    def sigma(self, p, n): return 0.0
    def substitute(self, p, n): return None
    def refund(self, n): return 0.0
    def fix_eff(self, n): return 0.0


def order(lines, promo=None, ad=None):
    return {"platform": "A", "order_id": "x", "lines": lines, "promo_id": promo, "ad_campaign": ad, "subtotal": sum(q * p for _, q, p in lines)}


class TestMoney(unittest.TestCase):
    def test_plain_order(self):
        # 120 元便當，抽成 35%、稅 5%：120 − 120×0.35×1.05 − 40 − 8 − 4 = 23.9
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]])], {}, [], Zero())
        self.assertAlmostEqual(r["total"], 23.9, places=6)

    def test_current_rate(self):
        # 用現行費率 30%：120 − 120×0.30×1.05 − 52 = 30.2
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]])], {}, [], Zero(), rate_key="rate")
        self.assertAlmostEqual(r["total"], 30.2, places=6)

    def test_promo_order_is_loss(self):
        # 5 折、店家全額負擔、佣金以折後金額計：120 − 60 − 60×0.3675 − 52 = −14.05
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]], promo="P1")], {}, [], Zero())
        self.assertAlmostEqual(r["total"], -14.05, places=6)
        self.assertAlmostEqual(r["by_promo"][("A", "P1")], -14.05, places=6)

    def test_end_promo(self):
        # 退出活動：一半的單留下且回到原價 → 0.5 × 23.9
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]], promo="P1")], {}, [{"type": "end_promo", "platform": "A", "promo_id": "P1"}], Zero())
        self.assertAlmostEqual(r["total"], 11.95, places=6)

    def test_price_raise_linear_demand(self):
        # 漲 10%（132 元），彈性 1 → 銷量 0.9；每份 132×0.6325 − 48 = 35.49；提袋 4×0.9
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]])], {}, [{"type": "set_price", "platform": "A", "item": "便當", "price": 132}], Zero())
        self.assertAlmostEqual(r["total"], 0.9 * 35.49 - 3.6, places=6)

    def test_ads_are_subtracted(self):
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]], ad="A-AD1")], {"A-AD1": 50}, [], Zero())
        self.assertAlmostEqual(r["total"], 23.9 - 50, places=6)
        r = core.project(tiny_ctx(), [order([["便當", 1, 120]], ad="A-AD1")], {"A-AD1": 50}, [{"type": "stop_ad", "platform": "A", "campaign": "A-AD1"}], Zero())
        self.assertAlmostEqual(r["total"], 0.5 * 23.9, places=6)

    def test_constraints(self):
        ctx, cons = tiny_ctx(), {"protected_items": ["便當"], "max_raise": {"便當": 10}, "max_raise_pct": 0.2, "locked_promos": ["P1"]}
        bad = [{"type": "delist", "platform": "A", "item": "便當"}, {"type": "set_price", "platform": "A", "item": "便當", "price": 135},
               {"type": "set_price", "platform": "A", "item": "湯", "price": 50}, {"type": "end_promo", "platform": "A", "promo_id": "P1"}]
        self.assertEqual(len(core.validate_actions(ctx, bad, cons)), 4)
        ok = [{"type": "set_price", "platform": "A", "item": "便當", "price": 130}, {"type": "set_price", "platform": "A", "item": "湯", "price": 45}]
        self.assertEqual(core.validate_actions(ctx, ok, cons), [])


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = world.make_world(7)
        cls.d = tempfile.mkdtemp()
        world.export(cls.w, cls.d)
        cls.ctx = core.load_shop(cls.d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_world_is_reproducible(self):
        w2 = world.make_world(7)
        self.assertEqual([o["payout"] for o in self.w["orders"]], [o["payout"] for o in w2["orders"]])

    def test_reconciliation_passes_with_right_mapping(self):
        for p in "AB":
            _, _, rep = core.ingest(self.d, p, core.KNOWN_MAPPINGS[p], self.ctx)
            self.assertTrue(rep["ok"], rep)
            self.assertEqual(rep["payout_match"], 1.0)
            self.assertEqual(rep["payout_total"], sum(o["payout"] for o in self.w["orders"] if o["platform"] == p))

    def test_reconciliation_catches_wrong_mapping(self):
        m = {k: dict(v) for k, v in core.KNOWN_MAPPINGS["A"].items()}
        m["orders"]["commission"], m["orders"]["commission_tax"] = m["orders"]["commission_tax"], m["orders"]["commission"]
        self.assertFalse(core.ingest(self.d, "A", m, self.ctx)[2]["ok"])  # 佣金和稅對調
        m = {k: dict(v) for k, v in core.KNOWN_MAPPINGS["A"].items()}
        m["orders"]["merchant_discount"], m["orders"]["platform_discount"] = m["orders"]["platform_discount"], m["orders"]["merchant_discount"]
        self.assertFalse(core.ingest(self.d, "A", m, self.ctx)[2]["ok"])  # 店家折扣和平台折扣對調
        self.assertFalse(core.ingest(self.d, "B", core.KNOWN_MAPPINGS["A"], self.ctx)[2]["ok"])  # 拿錯格式

    def test_statement_total_matches_actual_contribution(self):
        orders, ads = [], {}
        for p in "AB":
            o, a, _ = core.ingest(self.d, p, core.KNOWN_MAPPINGS[p], self.ctx)
            orders += o
            ads.update(a)
        an = core.analyze(self.ctx, orders, ads)
        t = an["totals"]
        # 會計恆等式：營收 − 佣金稅 − 店家折扣 − 退款 = 撥款
        self.assertEqual(t["sales"] - t["commission"] - t["merchant_discount"] - t["refunds"], t["payout"])
        self.assertEqual(an["detected"], {k: sorted(v) for k, v in self.w["truth"].items()})

    def test_scripted_agent_end_to_end(self):
        r = agent.run_scripted(self.d)
        self.assertTrue(r["ok"])
        self.assertEqual(r["final"]["violations"], [])
        m = agent.score(self.d, r)
        # 每個埋入的問題不是有對應動作，就是有寫進提醒（例如試算後不划算的包材更換）
        noted = sum(n["kind"] == "refund_no_fix" for n in r["final"]["notes"])
        self.assertEqual(m["issues_handled"] + noted, m["issues_total"])
        self.assertEqual(m["report_numbers"], m["report_numbers_traceable"])

    def test_scripted_fails_loudly_on_unknown_format(self):
        d2 = tempfile.mkdtemp()
        world.export(self.w, d2, fmt_override={"B": 2})
        r = agent.run_scripted(d2)
        self.assertFalse(r["ok"])
        shutil.rmtree(d2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
