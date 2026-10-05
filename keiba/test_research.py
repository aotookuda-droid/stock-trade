import unittest

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


if __name__ == '__main__':
    unittest.main()
