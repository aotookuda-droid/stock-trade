import math
import unittest

from keiba import engine
from keiba.research import calibration as C
from keiba.research import market_check as M
from keiba.research import pacefit_check as F


def race(ranks):
    return {'pace': 'slow', 'leaders': 0,
            'horses': [{'num': i + 1, 'rank': r, 'odds': 4.0, 'style': '先行', 'obj': 0.0} for i, r in enumerate(ranks)]}


PRIOR = {k: {'slow': 1 / 3, 'middle': 1 / 3, 'high': 1 / 3} for k in range(4)}


class DeadHeatTest(unittest.TestCase):
    def test_second_place_dead_heat_has_no_order_loss(self):
        ll, br, order = F.per_race([race(['1', '2', '2', '4', '5'])], PRIOR, 1.0, 'market')[0]
        self.assertIsNotNone(ll)
        self.assertIsNotNone(br)
        self.assertIsNone(order)  # 0 ではなく評価しない

    def test_win_dead_heat_has_no_win_loss(self):
        self.assertEqual(F.per_race([race(['1', '1', '3', '4', '5'])], PRIOR, 1.0, 'market')[0], (None, None, None))

    def test_third_place_dead_heat_and_counts(self):
        races = [race(['1', '2', '3', '3', '5']), race(['1', '2', '3', '4', '5'])]
        self.assertIsNone(F.per_race(races, PRIOR, 1.0, 'market')[0][2])
        self.assertEqual(F.counts(races), (2, 1))
        # 平均は評価できるレースだけ（同着のレースに0を入れない）
        order = F.evaluate(races, PRIOR, 1.0, 'market')[2]
        self.assertAlmostEqual(order, F.per_race(races[1:], PRIOR, 1.0, 'market')[0][2])


def card(rows):
    return {'horses': [{'num': i + 1, 'rank': r, 'odds': o} for i, (r, o) in enumerate(rows)]}


class CalibrationTest(unittest.TestCase):
    def test_did_not_finish_stays_in_denominator_and_scratch_is_refunded(self):
        r = card([('1', 2.0), ('2', 4.0), ('中', 8.0), ('3', 8.0), ('4', 20.0), ('取', None), ('除', None)])
        for mod in (C, M):
            q = mod.market_win(r)
            self.assertEqual(set(q), {1, 2, 3, 4, 5})
            self.assertAlmostEqual(sum(q.values()), 1.0)
        # 中止の馬は2・3着の候補として分母に残る
        l1, l2, l3 = C.per_race(r)
        p = C.market_win(r)
        self.assertAlmostEqual(l2, -math.log(p[2] / (1 - p[1])))
        self.assertAlmostEqual(l3, -math.log(p[4] / (1 - p[1] - p[2])))

    def test_no_correction_is_harville(self):
        p = {1: .5, 2: .3, 3: .15, 4: .05}
        ref = engine.harville(p)['trifecta']
        got = C.benter_orders(p)
        for k, v in ref.items():
            self.assertAlmostEqual(got[k], v)
        self.assertAlmostEqual(sum(C.benter_orders(p, .8, .6).values()), 1.0)

    def test_dead_heats_are_not_scored(self):
        self.assertEqual(C.per_race(card([('1', 2.0), ('1', 4.0), ('3', 8.0), ('4', 9.0), ('5', 20.0)])), (None, None, None))
        l1, l2, l3 = C.per_race(card([('1', 2.0), ('2', 4.0), ('2', 8.0), ('4', 9.0), ('5', 20.0)]))
        self.assertIsNotNone(l1)
        self.assertEqual((l2, l3), (None, None))

    def test_kappa_only_discounts_longshots(self):
        r = card([('1', 2.0), ('2', 4.0), ('3', 8.0), ('4', 150.0), ('5', 300.0)])
        q, p = C.market_win(r), C.win_probs(r, kappa=.5)
        self.assertLess(p[4] / q[4], 1)
        self.assertAlmostEqual(p[4] / p[5], q[4] / q[5])
        self.assertAlmostEqual(p[1] / p[2], q[1] / q[2])


if __name__ == '__main__':
    unittest.main()
