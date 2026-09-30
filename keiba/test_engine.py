import unittest

from keiba import engine


def horse(num, ability, style='差し', zone='中', **kw):
    return {'num': num, 'ability': ability, 'style': style, 'gate_zone': zone, **kw}


class EngineTest(unittest.TestCase):
    def test_harville_place_sums_to_three(self):
        p = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
        place, wide, umaren, trio = engine.harville(p)
        self.assertAlmostEqual(sum(place.values()), 3.0)
        self.assertAlmostEqual(sum(umaren.values()), 1.0)
        self.assertAlmostEqual(sum(trio.values()), 1.0)
        self.assertAlmostEqual(sum(wide.values()), 3.0)

    def test_track_bias(self):
        inner_front = [{'num': 1, 'field_size': 16, 'corner4': 1},
                       {'num': 3, 'field_size': 16, 'corner4': 2}]
        self.assertEqual(engine.track_bias(inner_front), ('inner', 'front'))
        self.assertEqual(engine.track_bias([]), ('flat', 'flat'))

    def test_pace(self):
        hs = [horse(i, 60, early_positions=[1, 2, 5]) for i in range(1, 4)]
        self.assertEqual(engine.predict_pace(hs), 'high')
        self.assertEqual(engine.predict_pace(hs[:1]), 'slow')

    def test_bias_fit_signs(self):
        self.assertGreater(engine.bias_fit(horse(1, 0, '逃げ', '内'), 'inner', 'front'), 0)
        self.assertLess(engine.bias_fit(horse(1, 0, '追込', '外'), 'inner', 'front'), 0)
        self.assertGreater(engine.bias_fit(horse(1, 0, '追込', '外'), 'outer', 'closer'), 0)

    def test_unknown_factor_rejected(self):
        with self.assertRaises(ValueError):
            engine.total_score(horse(1, 60, minus=['typo']), 'slow', 'flat', 'flat')

    def test_allocation_within_cap(self):
        hs = [horse(i, 60) for i in range(1, 9)]
        hs[7]['ability'] = 80
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': {str(i): 8.0 for i in range(1, 8)} | {'8': 30.0},
                         'place': {'8': [6.0, 9.0]}}}
        res = engine.analyze(race)
        self.assertTrue(res['bets'])
        self.assertLessEqual(res['total_stake'], engine.RACE_CAP)
        self.assertTrue(all(b['stake'] % 100 == 0 and b['stake'] > 0 for b in res['bets']))
        self.assertTrue(all(b['ev'] >= engine.EV_THRESHOLD for b in res['bets']))

    def test_no_value_means_skip(self):
        hs = [horse(i, 60) for i in range(1, 5)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': {str(i): 3.2 for i in range(1, 5)}}}
        res = engine.analyze(race)
        self.assertEqual(res['bets'], [])
        self.assertIn('見送り', engine.format_result(res))

    def test_select_races(self):
        races = [{'name': 'A', 'field_size': 10, 'fav_odds': 1.5},
                 {'name': 'B', 'field_size': 16, 'handicap': True, 'fav_odds': 4.0},
                 {'name': 'C', 'field_size': 18, 'fav_odds': 3.0}]
        self.assertEqual([r['name'] for r in engine.select_races(races)], ['B', 'C'])


if __name__ == '__main__':
    unittest.main()
