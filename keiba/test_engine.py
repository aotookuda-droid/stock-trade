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
        self.assertLessEqual(len(res['bets']), engine.MAX_TICKETS)
        self.assertTrue(all(b['stake'] % 100 == 0 and b['stake'] > 0 for b in res['bets']))
        self.assertTrue(all(b['ev'] >= engine.EV_THRESHOLD for b in res['bets']))

    def test_no_value_means_skip(self):
        hs = [horse(i, 60) for i in range(1, 5)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': {str(i): 3.2 for i in range(1, 5)}}}
        res = engine.analyze(race)
        self.assertEqual(res['bets'], [])
        self.assertEqual(res['skip_reason'], 'no_value')
        self.assertIn('見送り', engine.format_result(res))

    # 1. 複勝の的中範囲は発売開始時の頭数で決まる
    def test_place_small_field(self):
        p = {i: 1 / 7 for i in range(1, 8)}
        self.assertAlmostEqual(engine.harville(p, engine.place_slots(7))['place'][1], 2 / 7)
        self.assertAlmostEqual(engine.harville(p, engine.place_slots(8))['place'][1], 3 / 7)
        self.assertEqual(engine.place_slots(4), 0)

    def test_place_uses_sales_start_field_after_scratch(self):
        hs = [horse(i, 60, gate=i) for i in range(1, 9)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle', 'scratched': [8],
                'odds': {'win': {str(i): 7.0 for i in range(1, 8)}}}
        self.assertEqual(engine.analyze(race)['place_slots'], 3)  # 発売開始時8頭→取消で7頭でも3着まで
        race['field_size_at_sales_start'] = 7
        self.assertEqual(engine.analyze(race)['place_slots'], 2)

    # 2. 枠連・馬単は馬連と独立に評価する
    def pair_race(self, odds):
        hs = [horse(i, 60, gate=(i + 1) // 2) for i in range(1, 9)]
        hs[0]['ability'] = 90
        hs[1]['ability'] = 75
        win = {str(i): 20.0 for i in range(1, 9)}
        return {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': win, **odds}}

    def test_wakuren_evaluated_without_umaren(self):
        p = {i: 1 / 8 for i in range(1, 9)} | {1: 0.3, 2: 0.2}
        p = engine._normalize(p)
        gates = {i: (i + 1) // 2 for i in range(1, 9)}
        cands, _ = engine.candidate_bets(p, {'wakuren': {'1-1': 25.0}, 'umaren': {'1-2': 30.0}}, gates, model_weight=1.0)
        self.assertIn(('枠連', [1, 1]), [(b['type'], b['horses']) for b in cands])
        cands, _ = engine.candidate_bets(p, {'wakuren': {'1-1': 25.0}}, gates, model_weight=1.0)
        self.assertIn(('枠連', [1, 1]), [(b['type'], b['horses']) for b in cands])

    def test_umatan_both_directions_evaluated(self):
        p = engine._normalize({i: 1 / 8 for i in range(1, 9)} | {1: 0.2, 2: 0.18})
        cands, _ = engine.candidate_bets(p, {'umatan': {'1>2': 60.0, '2>1': 60.0}}, model_weight=1.0)
        self.assertEqual(sorted(b['horses'] for b in cands if b['type'] == '馬単'), [[1, 2], [2, 1]])

    def test_sanrentan(self):
        p = engine._normalize({i: 1.0 for i in range(1, 9)} | {1: 4.0, 2: 2.0})
        cands, tri = engine.candidate_bets(p, {'sanrentan': {'1>2>3': 400.0, '3>2>1': 400.0}}, model_weight=1.0)
        got = {tuple(b['horses']): b['prob'] for b in cands if b['type'] == '三連単'}
        self.assertAlmostEqual(got[(1, 2, 3)], tri[(1, 2, 3)])
        self.assertGreater(got[(1, 2, 3)], got[(3, 2, 1)])

    def test_ticket_market_blend(self):
        b = engine._bet('馬単', (1, 2), 0.05, 100.0, model_weight=1.0)
        self.assertAlmostEqual(b['ev'], 5.0)
        b = engine._bet('馬単', (1, 2), 0.05, 100.0, model_weight=0.5)
        self.assertAlmostEqual(b['prob'], (0.05 * 0.0075) ** 0.5)
        self.assertAlmostEqual(b['payout_odds'] * b['model_prob'], b['ev'])
        self.assertEqual(engine._bet('単勝', (1,), 0.05, 100.0, model_weight=0.5)['ev'], 5.0)

    # 3. 重なる買い目は同時損失を含めて配分する
    def test_duplicate_bet_does_not_double_stake(self):
        p = engine._normalize({1: 0.3, 2: 0.2, 3: 0.2, 4: 0.15, 5: 0.15})
        tri = engine.harville(p)['trifecta']
        one = [engine._bet('単勝', (1,), p[1], 5.0)]
        two = one + [dict(one[0])]
        s1 = sum(b['stake'] for b in engine.allocate(one, tri)[0])
        s2 = sum(b['stake'] for b in engine.allocate(two, tri)[0])
        self.assertEqual(s1, s2)

    # 4. 100円単位で選び直し、丸めで消えた場合は理由を分ける
    def test_tiny_edges_still_allocated(self):
        # 旧方式では予算200円を15点に比例配分して全て100円未満→0点になった例
        p = engine._normalize({i: 1.0 for i in range(1, 8)})
        tri = engine.harville(p)['trifecta']
        bets = [engine._bet('三連単', o, tri[o], 1.21 / tri[o], model_weight=1.0) for o in list(tri)[:15]]
        self.assertTrue(all(b['kelly'] < 0.001 for b in bets))
        kept, reason = engine.allocate(bets, tri)
        self.assertTrue(kept)
        self.assertIsNone(reason)

    def test_unit_rounding_reason(self):
        p = engine._normalize({i: 1.0 for i in range(1, 9)})
        tri = engine.harville(p)['trifecta']
        bets = [engine._bet('三連単', (1, 2, 3), tri[(1, 2, 3)], 1.25 / tri[(1, 2, 3)])]
        kept, reason = engine.allocate(bets, tri, capital=500)
        self.assertEqual((kept, reason), ([], 'no_unit_allocation'))

    # 5. 土日8,000円と3レース目
    def test_weekend_cap_and_third_race(self):
        ledger = {'cap': 8000, 'races': [
            {'race_id': 'A', 'primary': True, 'status': 'pending'},
            {'race_id': 'B', 'primary': True, 'status': 'pending'},
            {'race_id': 'C', 'primary': False, 'status': 'pending'}]}
        self.assertEqual(engine.weekend_cap(ledger, 'C'), 0)  # 本命2レースの予約で残りなし
        self.assertEqual(engine.weekend_cap(ledger, 'A'), 4000)
        engine.record_decision(ledger, 'A', 2500, 't')
        self.assertEqual(engine.weekend_cap(ledger, 'C'), 1500)
        self.assertEqual(engine.weekend_cap(ledger, 'B'), 4000)
        engine.record_decision(ledger, 'B', 4000, 't')
        self.assertEqual(engine.weekend_cap(ledger, 'C'), 1500)
        with self.assertRaises(ValueError):
            engine.record_decision(ledger, 'A', 100, 't')

    # 6. 最終提案は時刻条件を満たす実取得オッズだけ
    def test_validate_final(self):
        race = {'start_at': '2026-10-04T15:30:00+09:00', 'odds': {'win': {'1': 2.0}},
                'odds_meta': {'kind': 'observed', 'source': 'netkeiba',
                              'fetched_at': {'win': '2026-10-04T14:27:30+09:00'}}}
        engine.validate_final(race)
        for t in ('2026-10-04T14:30:05+09:00', '2026-10-04T14:15:00+09:00'):
            race['odds_meta']['fetched_at']['win'] = t
            with self.assertRaises(ValueError):
                engine.validate_final(race)
        race['odds_meta'] = {'kind': 'predicted', 'source': 'x', 'fetched_at': {'win': '2026-10-04T14:27:30+09:00'}}
        with self.assertRaises(ValueError):
            engine.validate_final(race)

    def test_missing_win_odds_is_error_not_scratch(self):
        hs = [horse(i, 60) for i in range(1, 6)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': {str(i): 5.0 for i in range(1, 5)}}}
        with self.assertRaises(ValueError):
            engine.analyze(race)
        race['scratched'] = [5]
        self.assertNotIn(5, engine.analyze(race)['win_prob'])

    def test_select_races(self):
        races = [{'name': 'A', 'field_size': 10, 'fav_odds': 1.5},
                 {'name': 'B', 'field_size': 16, 'handicap': True, 'fav_odds': 4.0},
                 {'name': 'C', 'field_size': 18, 'fav_odds': 3.0}]
        self.assertEqual([r['name'] for r in engine.select_races(races)], ['B', 'C'])


if __name__ == '__main__':
    unittest.main()
