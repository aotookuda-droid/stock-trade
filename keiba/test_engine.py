import unittest

from keiba import engine


def horse(num, ability, style='差し', zone='中', **kw):
    return {'num': num, 'ability': ability, 'style': style, 'gate_zone': zone, **kw}


class EngineTest(unittest.TestCase):
    def test_harville_place_sums_to_three(self):
        p = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.1}
        probs = engine.harville(p)
        self.assertAlmostEqual(sum(probs['place'].values()), 3.0)
        self.assertAlmostEqual(sum(probs['umaren'].values()), 1.0)
        self.assertAlmostEqual(sum(probs['umatan'].values()), 1.0)
        self.assertAlmostEqual(sum(probs['trio'].values()), 1.0)
        self.assertAlmostEqual(sum(probs['trifecta'].values()), 1.0)
        self.assertAlmostEqual(sum(probs['wide'].values()), 3.0)

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

    def pair_race(self, odds):
        hs = [horse(i, 60, gate=(i + 1) // 2) for i in range(1, 9)]
        hs[0]['ability'] = 90
        hs[1]['ability'] = 75
        win = {str(i): 20.0 for i in range(1, 9)}
        return {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': win, **odds}}

    def test_wakuren_replaces_umaren_when_higher(self):
        res = engine.analyze(self.pair_race({'umaren': {'1-2': 30.0}, 'wakuren': {'1-1': 35.0}}))
        pair = [b for b in res['bets'] if b['type'] in ('馬連', '枠連')]
        self.assertEqual([(b['type'], b['horses']) for b in pair], [('枠連', [1, 1])])
        self.assertIn('枠1-1', engine.format_result(res))

    def test_wakuren_ignored_when_lower(self):
        res = engine.analyze(self.pair_race({'umaren': {'1-2': 30.0}, 'wakuren': {'1-1': 25.0}}))
        self.assertEqual([b['type'] for b in res['bets'] if b['type'] in ('馬連', '枠連')], ['馬連'])

    def test_umatan_only_when_order_clear(self):
        clear = engine.analyze(self.pair_race({'umaren': {'1-2': 30.0}, 'umatan': {'1>2': 55.0, '2>1': 70.0}}))
        self.assertIn(('馬単', [1, 2]), [(b['type'], b['horses']) for b in clear['bets']])
        self.assertNotIn(('馬単', [2, 1]), [(b['type'], b['horses']) for b in clear['bets']])
        # 1頭目と2頭目の力が拮抗していれば馬単は買わない
        even = self.pair_race({'umaren': {'1-2': 30.0}, 'umatan': {'1>2': 55.0, '2>1': 55.0}})
        even['horses'][1]['ability'] = 90
        res = engine.analyze(even)
        self.assertFalse([b for b in res['bets'] if b['type'] == '馬単'])

    def test_sanrentan(self):
        res = engine.analyze(self.pair_race({'sanrentan': {'1>2>3': 400.0, '3>2>1': 400.0}}))
        self.assertEqual([b['horses'] for b in res['bets'] if b['type'] == '三連単'], [[1, 2, 3]])

    def test_select_races(self):
        races = [{'name': 'A', 'field_size': 10, 'fav_odds': 1.5},
                 {'name': 'B', 'field_size': 16, 'handicap': True, 'fav_odds': 4.0},
                 {'name': 'C', 'field_size': 18, 'fav_odds': 3.0}]
        self.assertEqual([r['name'] for r in engine.select_races(races)], ['B', 'C'])

    def test_longshot_singles_excluded(self):
        p = {1: 0.5, 2: 0.48, 3: 0.02}
        bets = engine.candidate_bets(p, {'win': {'1': 3.0, '2': 1.5, '3': 200.0}})
        self.assertEqual([b['horses'] for b in bets], [[1]])

    def test_min_budget(self):
        bets = [{'kelly': 0.01}, {'kelly': 0.005}]
        stakes = engine.allocate(bets)
        self.assertEqual(sum(b['stake'] for b in stakes), engine.MIN_BUDGET)
        big = engine.allocate([{'kelly': 0.5}])
        self.assertEqual(big[0]['stake'], engine.RACE_CAP)


if __name__ == '__main__':
    unittest.main()
