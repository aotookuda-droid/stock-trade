import math
import unittest

from keiba import engine
from keiba.research import calibration as C
from keiba.research import form_check as FC
from keiba.research import market_check as M
from keiba.research import pacefit_check as F
from keiba.research import scale_calibration as SC
from keiba.research import trends as T


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

    def test_demoted_horse_keeps_its_official_place(self):
        r = card([('1', 2.0), ('2', 4.0), ('3(降)', 8.0), ('4', 9.0), ('5', 20.0)])
        self.assertEqual(C.finish(r, 3), 3)
        self.assertIsNotNone(C.per_race(r)[2])

    def test_kappa_only_discounts_longshots(self):
        r = card([('1', 2.0), ('2', 4.0), ('3', 8.0), ('4', 150.0), ('5', 300.0)])
        q, p = C.market_win(r), C.win_probs(r, kappa=.5)
        self.assertLess(p[4] / q[4], 1)
        self.assertAlmostEqual(p[4] / p[5], q[4] / q[5])
        self.assertAlmostEqual(p[1] / p[2], q[1] / q[2])


    def test_pooled_matches_engine_mixing(self):
        from keiba.research import calibration_model as CM
        r = card([('1', 2.0), ('2', 4.0), ('3', 8.0), ('中', 15.0), ('4', 30.0)])
        r['obj'] = {1: 0.0, 2: -3.0, 3: -5.0, 4: -8.0, 5: -10.0}
        one = (1.0, 1.0)
        market = C.benter_orders(C.market_win(r))
        for k, v in CM.pooled(r, 0.0, one, one).items():
            self.assertAlmostEqual(v, market[k])
        scores = r['obj']
        odds = {'win': {str(h['num']): h['odds'] for h in r['horses']}}
        ref, _, _ = engine.order_distribution(scores, odds, model_weight=.25, market='win')
        got = CM.pooled(r, .25, one, one)
        for k, v in ref.items():
            self.assertAlmostEqual(got[k], v)


if __name__ == '__main__':
    unittest.main()


class TrendsTest(unittest.TestCase):
    def test_stopped_horse_counts_as_runner_but_scratch_does_not(self):
        horses = [{'rank': r, 'waku': 1, 'pop': 1, 'sexage': '牝4', 'passage': '1-1'} for r in ('1', '中', '取')]
        stats = T.table([{'field': 3, 'date': '2023-10-14', 'horses': [dict(h, hist=None) for h in horses]}])
        self.assertEqual(stats['年齢']['4歳'], [2, 1, 1])


class FormCheckTest(unittest.TestCase):
    def run_(self, passage, field=16, course='芝1800', date='2026-01-01'):
        return {'date': date, 'race_id': None, 'name': '', 'venue': '', 'field': field, 'rank': '1',
                'course': course, 'passage': passage, 'margin': '0.0'}

    def test_corner4_uses_last_corner_and_skips_jumps(self):
        self.assertAlmostEqual(FC.corner4(self.run_('12-10-4')), 4 / 16)
        self.assertIsNone(FC.corner4(self.run_('')))
        self.assertIsNone(FC.corner4(self.run_('3-3', course='障3000')))

    def test_recent_c4_ignores_runs_on_or_after_the_race_day(self):
        hist = (self.run_('1-1', date='2026-02-01'), self.run_('8-8', date='2026-01-01'))
        self.assertEqual([v for _, v in FC.recent_c4(hist, '2026-02-01')], [0.5])

    def test_labels(self):
        self.assertEqual([FC.c4_label(v) for v in (0.25, 0.26, 0.75, 0.9)], ['前', '中前', '中後', '後'])
        self.assertEqual([FC.level_label(n) for n in (0, 3, 8, 9)], list(FC.LEVEL_LABELS))
        self.assertEqual(FC.after_label(2, 2), 'その後の出走3未満')
        self.assertEqual([FC.after_label(10, k) for k in (1, 2, 4)], ['2割未満', '2〜4割', '4割以上'])


class ScaleCalibrationTest(unittest.TestCase):
    def test_rank_mapping_keeps_market_sizes_in_model_order(self):
        import numpy as np
        m = np.array([0.5, 0.3, 0.2])
        p = SC.by_rank(np.array([1.0, 3.0, 2.0]), m)
        self.assertEqual(list(p), [0.2, 0.5, 0.3])

    def test_spread_matches_market_log_spread(self):
        import numpy as np
        m = np.array([0.5, 0.3, 0.15, 0.05])
        p = SC.by_spread(1.0)(np.array([70.0, 60.0, 65.0, 50.0]), m)
        self.assertAlmostEqual(float(np.log(p).std()), float(np.log(m).std()), places=6)
        self.assertAlmostEqual(float(p.sum()), 1.0)
