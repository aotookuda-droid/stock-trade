import math
import unittest
from itertools import permutations

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
        leaders = [horse(i, 60, early_positions=[1, 1, 5]) for i in range(1, 4)]
        self.assertEqual(engine.predict_pace(leaders), engine.PACE_PRIOR[3])
        self.assertEqual(engine.predict_pace(leaders[:1]), engine.PACE_PRIOR[1])
        # 番手（2番手）が多いだけの先行馬はハナ候補に数えない
        pressers = [horse(i, 60, early_positions=[2, 2, 1]) for i in range(1, 6)]
        self.assertEqual(engine.predict_pace(pressers), engine.PACE_PRIOR[0])
        for prior in engine.PACE_PRIOR.values():
            self.assertAlmostEqual(sum(prior.values()), 1.0)

    def test_pace_fit_mixes_probabilities(self):
        self.assertEqual(engine.pace_fit('逃げ', 'slow'), engine.PACE_FIT['slow']['逃げ'])
        mixed = engine.pace_fit('逃げ', {'slow': 0.5, 'high': 0.5})
        self.assertAlmostEqual(mixed, (engine.PACE_FIT['slow']['逃げ'] + engine.PACE_FIT['high']['逃げ']) / 2)
        self.assertEqual(engine.pace_label({'slow': 0.2, 'middle': 0.5, 'high': 0.3}), 'middle')
        with self.assertRaises(ValueError):
            engine.pace_probs({'slow': 0.5, 'high': 0.4})

    def test_analyze_accepts_pace_probabilities(self):
        hs = [horse(i, 80 - i, '逃げ' if i == 4 else '差し') for i in range(1, 5)]
        race = {'race': 'test', 'horses': hs, 'odds': {'win': {str(i): 4.0 for i in range(1, 5)}}}
        slow = engine.analyze(dict(race, pace={'slow': 1.0}))
        mixed = engine.analyze(dict(race, pace={'slow': 0.5, 'middle': 0.5}))
        self.assertEqual(mixed['pace'], 'slow')
        self.assertEqual(mixed['pace_probs'], {'slow': 0.5, 'middle': 0.5})
        self.assertGreater(slow['scores'][4], mixed['scores'][4])

    def test_temperament_is_a_minus_factor(self):
        h = horse(1, 70)
        base = engine.total_score(h, 'middle', 'flat', 'flat')
        h['minus'] = ['temperament']
        self.assertEqual(engine.total_score(h, 'middle', 'flat', 'flat'), base - 3)

    def test_bets_outcome_and_pace_sensitivity(self):
        orders = {(1, 2, 3): 0.5, (2, 1, 3): 0.3, (3, 2, 1): 0.2}
        bets = [{'type': '単勝', 'horses': [1], 'stake': 100, 'odds': 3.0},
                {'type': 'ワイド', 'horses': [1, 2], 'stake': 100, 'odds': 2.0}]
        out = engine.bets_outcome(bets, orders)
        # 1-2-3: 300+200、2-1-3: 200、3-2-1: 200 → (0.5×500+0.3×200+0.2×200)/200
        self.assertAlmostEqual(out['return'], (250 + 60 + 40) / 200)
        self.assertAlmostEqual(out['loss'], 0.0)
        hs = [horse(i, 80 - i, '逃げ' if i == 4 else '差し') for i in range(1, 5)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': {'1': 6.0, '2': 6.0, '3': 8.0, '4': 9.0}}}
        res = engine.analyze(race)
        self.assertTrue(res['bets'])
        self.assertEqual(set(res['pace_sensitivity']), {'slow', 'middle', 'high'})
        self.assertIn('展開別', engine.format_result(res))

    # 枠連の horses は枠番。乖離チェックでは枠番を馬番と取り違えず、その枠の出走馬として数える
    def test_divergence_warnings_expand_bracket(self):
        model = {1: 0.1, 2: 0.1, 3: 0.3, 4: 0.3, 5: 0.2}
        market = {1: 0.02, 2: 0.3, 3: 0.3, 4: 0.18, 5: 0.2}
        gates = {1: 1, 2: 1, 3: 2, 4: 2, 5: 3}
        bets = [{'type': '枠連', 'horses': [1, 2], 'ev': 1.3, 'stake': 300},
                {'type': '馬連', 'horses': [4, 5], 'ev': 1.3, 'stake': 100}]
        w = engine.divergence_warnings(model, market, bets, None, gates)
        got = {h['num']: h for h in w['horses']}
        # 枠1（馬1・2）と枠2（馬3・4）を含む。枠番2を馬2と取り違えない
        self.assertEqual(set(got), {1, 4})
        # 馬1は枠連だけ（同じ枠の馬2でも当たるので必須ではない）。馬4は馬連で必須、枠連は枠2を含む
        self.assertAlmostEqual(got[1]['required_share'], 0.0)
        self.assertAlmostEqual(got[1]['bracket_share'], 0.75)
        self.assertEqual(got[1]['gate_mates'], [1, 2])
        self.assertAlmostEqual(got[4]['required_share'], 0.25)
        self.assertEqual(got[4]['required_types'], ['馬連'])
        self.assertAlmostEqual(got[4]['bracket_share'], 0.75)
        lines = engine.format_warnings({'warnings': w})
        self.assertIn('4番が必須 25%（馬連）、4番のいる2枠（3・4番）を含む枠連 75%', lines[0])
        self.assertIn('1番のいる1枠（1・2番）を含む枠連 75%', lines[1])
        self.assertNotIn('1番が必須', lines[1])

    def test_divergence_warnings(self):
        model = {1: 0.25, 2: 0.15, 3: 0.60}
        market = {1: 0.10, 2: 0.30, 3: 0.60}
        bets = [{'type': 'ワイド', 'horses': [1, 3], 'ev': 2.3, 'stake': 300},
                {'type': '馬連', 'horses': [2, 3], 'ev': 1.4, 'stake': 100}]
        w = engine.divergence_warnings(model, market, bets, {'codex': {1: 0.12, 2: 0.2, 3: 0.68}})
        self.assertEqual([h['num'] for h in w['horses']], [1])
        self.assertAlmostEqual(w['horses'][0]['stake_share'], 0.75)
        self.assertAlmostEqual(w['horses'][0]['required_share'], 0.75)
        self.assertEqual([b['horses'] for b in w['tickets']], [[1, 3]])
        # 他の出どころのどれか1つでも近ければ印を付けない
        w = engine.divergence_warnings(model, market, bets, {'codex': {1: 0.2, 2: 0.2, 3: 0.6}})
        self.assertEqual(w['horses'], [])
        # 買い目に入っていない馬には付けない
        self.assertEqual(engine.divergence_warnings(model, market, [], None)['horses'], [])

    def test_bias_fit_signs(self):
        self.assertGreater(engine.bias_fit(horse(1, 0, '逃げ', '内'), 'inner', 'front'), 0)
        self.assertLess(engine.bias_fit(horse(1, 0, '追込', '外'), 'inner', 'front'), 0)
        self.assertGreater(engine.bias_fit(horse(1, 0, '追込', '外'), 'outer', 'closer'), 0)

    def test_jockey_downgrade_is_minus(self):
        h = {'ability': 80, 'style': '差し', 'gate_zone': '中', 'minus': [], 'plus': []}
        base = engine.total_score(h, 'middle', 'flat', 'flat')
        self.assertEqual(engine.total_score(dict(h, minus=['jockey_downgrade']), 'middle', 'flat', 'flat'), base - 4)

    def test_unknown_factor_rejected(self):
        with self.assertRaises(ValueError):
            engine.total_score(horse(1, 60, minus=['typo']), 'slow', 'flat', 'flat')

    def test_flag_adjust_discounts_one_flag(self):
        h = {'ability': 56, 'style': '先行', 'gate_zone': '外', 'minus': ['turf_to_outer'], 'plus': ['breaking_out']}
        base = engine.total_score(h, 'middle', 'flat', 'flat')
        self.assertEqual(engine.total_score(dict(h, flag_adjust={'breaking_out': -1}), 'middle', 'flat', 'flat'), base - 1)
        self.assertEqual(engine.total_score(dict(h, flag_adjust={'turf_to_outer': 1}), 'middle', 'flat', 'flat'), base + 1)

    def test_flag_adjust_only_discounts_present_flags(self):
        h = {'ability': 56, 'style': '先行', 'gate_zone': '外', 'minus': [], 'plus': ['breaking_out']}
        for adj in ({'breaking_out': 1}, {'breaking_out': -4}, {'breaking_out': -6}, {'second_start': -1}):
            with self.assertRaises(ValueError):
                engine.total_score(dict(h, flag_adjust=adj), 'middle', 'flat', 'flat')

    def test_allocation_within_cap(self):
        hs = [horse(i, 60) for i in range(1, 9)]
        hs[7]['ability'] = 80
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': {str(i): 8.0 for i in range(1, 8)} | {'8': 30.0},
                         'place': {'8': [6.0, 9.0]}}}
        # 配分の制約だけを見るので乖離ゲートは外す（8番はモデルが市場の2倍以上に見る）
        res = engine.analyze(race, divergence_gate=False)
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
        orders = engine.harville(p)['trifecta']
        cands = engine.candidate_bets(orders, {'wakuren': {'1-1': 25.0}, 'umaren': {'1-2': 30.0}}, gates)
        self.assertIn(('枠連', [1, 1]), [(b['type'], b['horses']) for b in cands])
        cands = engine.candidate_bets(orders, {'wakuren': {'1-1': 25.0}}, gates)
        self.assertIn(('枠連', [1, 1]), [(b['type'], b['horses']) for b in cands])

    def test_umatan_both_directions_evaluated(self):
        p = engine._normalize({i: 1 / 8 for i in range(1, 9)} | {1: 0.2, 2: 0.18})
        cands = engine.candidate_bets(engine.harville(p)['trifecta'], {'umatan': {'1>2': 60.0, '2>1': 60.0}})
        self.assertEqual(sorted(b['horses'] for b in cands if b['type'] == '馬単'), [[1, 2], [2, 1]])

    def test_sanrentan(self):
        p = engine._normalize({i: 1.0 for i in range(1, 9)} | {1: 4.0, 2: 2.0})
        tri = engine.harville(p)['trifecta']
        cands = engine.candidate_bets(tri, {'sanrentan': {'1>2>3': 400.0, '3>2>1': 400.0}})
        got = {tuple(b['horses']): b['prob'] for b in cands if b['type'] == '三連単'}
        self.assertAlmostEqual(got[(1, 2, 3)], tri[(1, 2, 3)])
        self.assertGreater(got[(1, 2, 3)], got[(3, 2, 1)])

    # 2・3着の補正 λ（参考試算のみ）。1着の確率は変えず、三連単オッズの市場分布には掛けない
    def test_benter_reduces_to_harville_and_keeps_win(self):
        p = engine._normalize({1: 5.0, 2: 3.0, 3: 2.0, 4: 1.0, 5: 0.5})
        tri = engine.harville(p)['trifecta']
        got = engine.benter_orders(p, 1.0, 1.0)
        for k, v in tri.items():
            self.assertAlmostEqual(got[k], v)
        disc = engine.benter_orders(p, .8, .7)
        self.assertAlmostEqual(sum(disc.values()), 1.0)
        win = engine.ticket_probabilities(disc, 3)['win']
        for k, v in p.items():
            self.assertAlmostEqual(win[k], v)
        # 人気薄の2・3着が Harville より上がる
        self.assertGreater(engine.ticket_probabilities(disc, 3)['place'][5], engine.harville(p)['place'][5])

    def test_place_discount_paths(self):
        scores = {1: 80, 2: 70, 3: 65, 4: 60, 5: 52}
        win = {'1': 3.0, '2': 4.0, '3': 6.0, '4': 9.0, '5': 30.0}
        tri = {f'{a}>{b}>{c}': 20.0 + 3 * a + b for (a, b, c) in permutations(scores, 3)}
        both = engine.REFERENCE_PLACE_DISCOUNTS['lambda_both']
        lm, lq = engine.PLACE_DISCOUNT_MODEL, engine.PLACE_DISCOUNT_WIN_MARKET
        mw = engine.model_probabilities(scores)
        # 重み1: モデルの Benter 分布そのもので、1着はモデル勝率のまま
        got, src, _ = engine.order_distribution(scores, {'win': win}, 1.0, place_discount=both)
        self.assertEqual(src, 'model_only')
        self.assertEqual(got, engine.benter_orders(mw, *lm))
        for k, v in engine.ticket_probabilities(got, 3)['win'].items():
            self.assertAlmostEqual(v, mw[k])
        # 重み0・三連単あり: λ は効かず、三連単市場のまま
        plain, _, _ = engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, 0.0)
        got, src, _ = engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, 0.0, place_discount=both)
        self.assertEqual(src, 'sanrentan')
        self.assertEqual(got, plain)
        # 重み0・単勝代用: 両側なら市場側も Benter、モデル側だけなら Harville のまま
        q = engine._normalize({int(k): 1 / v for k, v in win.items()})
        got, src, _ = engine.order_distribution(scores, {'win': win}, 0.0, place_discount=both)
        self.assertEqual(src, 'win_harville')
        for k, v in engine.benter_orders(q, *lq).items():
            self.assertAlmostEqual(got[k], v)
        got, _, _ = engine.order_distribution(scores, {'win': win}, 0.0, place_discount={'model': lm})
        for k, v in engine.harville(q)['trifecta'].items():
            self.assertAlmostEqual(got[k], v)
        # 現行の重み・三連単あり: モデル側の Benter と三連単市場の幾何平均。1着は混合後の分布から集計し直す
        got, _, _ = engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, engine.MODEL_WEIGHT,
                                              place_discount={'model': lm})
        m = engine.benter_orders(mw, *lm)
        mk = engine._normalize({o: 1 / v for o, v in ((engine._ordered(k), v) for k, v in tri.items())})
        w = engine.MODEL_WEIGHT
        ref = engine._normalize({o: m[o] ** w * mk[o] ** (1 - w) for o in m})
        for k, v in ref.items():
            self.assertAlmostEqual(got[k], v)
        # λ なしなら従来どおり
        self.assertEqual(engine.order_distribution(scores, {'win': win, 'sanrentan': tri})[0],
                         engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, place_discount=None)[0])

    # 市場補正は着順分布1つに対して行い、期待値も配分も同じ分布と実オッズで計算する
    def test_order_distribution_pooling(self):
        scores = {1: 80, 2: 70, 3: 65, 4: 60}
        win = {'1': 6.0, '2': 3.0, '3': 4.0, '4': 8.0}
        model, src, _ = engine.order_distribution(scores, {'win': win}, model_weight=1.0)
        self.assertEqual(src, 'model_only')
        self.assertEqual(model, engine.harville(engine.model_probabilities(scores))['trifecta'])
        tri = {f'{a}>{b}>{c}': 50.0 + a for (a, b, c) in model}
        pooled, src, miss = engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, model_weight=0.5)
        self.assertEqual(miss, 0)
        self.assertEqual(src, 'sanrentan')
        self.assertAlmostEqual(sum(pooled.values()), 1.0)
        market, _, _ = engine.order_distribution(scores, {'win': win, 'sanrentan': tri}, model_weight=0.0)
        expect = engine._normalize({o: 1 / (50.0 + o[0]) for o in model})
        for o in model:
            self.assertAlmostEqual(market[o], expect[o])
        # 三連単が揃っていなければ単勝の Harville を市場側に使う
        del tri['1>2>3']
        _, src, miss = engine.order_distribution(scores, {'win': win, 'sanrentan': tri})
        self.assertEqual((src, miss), ('win_harville', 1))
        # 幾何平均: 市場がほぼ否定した着順は混ぜた後も小さい
        tri['1>2>3'] = 1e6
        pooled, _, _ = engine.order_distribution(scores, {'win': win, 'sanrentan': tri})
        self.assertLess(pooled[(1, 2, 3)], 0.1 * model[(1, 2, 3)])

    # 表示上限と同じ値（999999.9 など）は市場分布にも買い目候補にも使わない
    def test_invalid_odds_reason(self):
        self.assertEqual(engine.invalid_odds_reason(999999.9), 'display_cap')
        self.assertEqual(engine.invalid_odds_reason(99999.9), 'display_cap')
        self.assertEqual(engine.invalid_odds_reason([9999.9, 12000.0]), 'display_cap')
        self.assertIsNone(engine.invalid_odds_reason(1.0))  # 100円元返しは実在する
        self.assertEqual(engine.invalid_odds_reason(0.0), 'below_one')
        self.assertEqual(engine.invalid_odds_reason(float('nan')), 'not_number')
        self.assertEqual(engine.invalid_odds_reason('---'), 'not_number')
        for ok in (1.1, 999.9, 99999.8, 158465.3, 1e6):
            self.assertIsNone(engine.invalid_odds_reason(ok))

    # 単勝1.0倍の馬がいても、市場確率に残したまま他の馬を含めて計算が完了する（買い目候補にはしない）
    def test_win_odds_of_one_kept_for_market(self):
        hs = [horse(i, 60 + i) for i in range(1, 6)]
        win = {'1': 1.0, '2': 30.0, '3': 50.0, '4': 80.0, '5': 99.0}
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': win, 'place': {'1': [1.0, 1.0], '2': [3.0, 6.0]}}}
        res = engine.analyze(race)
        self.assertEqual(res['odds_excluded'], {})
        self.assertEqual(set(res['win_market_prob']), {1, 2, 3, 4, 5})
        self.assertGreater(res['win_market_prob'][1], 0.9)
        self.assertAlmostEqual(sum(res['win_prob'].values()), 1.0)
        self.assertNotIn(1, [b['horses'][0] for b in res['bets'] if b['type'] in ('単勝', '複勝')])
        orders = engine.harville({1: 0.97, 2: 0.01, 3: 0.01, 4: 0.01})['trifecta']
        self.assertEqual(engine.candidate_bets(orders, {'win': {'1': 1.0}, 'place': {'1': [1.0, 1.0]}}), [])

    def test_display_cap_odds_excluded_from_market_and_candidates(self):
        hs = [horse(i, 60 + 2 * i) for i in range(1, 6)]
        win = {str(i): o for i, o in zip(range(1, 6), (12.0, 8.0, 5.0, 4.0, 3.0))}
        tri = {f'{a}>{b}>{c}': 50.0 for a, b, c in permutations(range(1, 6), 3)}
        tri['1>2>3'] = 999999.9
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': win, 'sanrentan': tri, 'sanrenpuku': {'1-2-3': 99999.9, '3-4-5': 20.0}}}
        res = engine.analyze(race)
        self.assertEqual(res['market_source'], 'win_harville')
        self.assertEqual(res['sanrentan_missing'], 1)
        self.assertEqual(res['odds_excluded']['sanrentan'], {'count': 1, 'reasons': {'display_cap': 1},
                                                             'items': {'1>2>3': 999999.9}})
        self.assertEqual(res['odds_excluded']['sanrenpuku']['items'], {'1-2-3': 99999.9})
        self.assertIn('除外オッズ 三連単1組・三連複1組（表示上限と同じ値', engine.market_label(res))
        self.assertEqual(race['odds']['sanrentan']['1>2>3'], 999999.9)  # 元のオッズは残す
        # 候補を直接作る場合も払戻倍率として使わない
        orders = engine.harville(engine._normalize({1: 0.3, 2: 0.3, 3: 0.2, 4: 0.1, 5: 0.1}))['trifecta']
        cands = engine.candidate_bets(orders, {'sanrentan': {'1>2>3': 999999.9}, 'sanrenpuku': {'1-2-3': 99999.9}})
        self.assertEqual(cands, [])

    # 市場のみの試算（同じオッズ・予算・制約）と本番との重なり。モデルとの比較は計算用市場で行う
    def test_market_only_check_and_calc_market(self):
        hs = [horse(i, 60 + i) for i in range(1, 6)]
        win = {str(i): o for i, o in zip(range(1, 6), (12.0, 8.0, 5.0, 4.0, 3.0))}
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': win, 'place': {'5': [4.0, 5.0], '1': [1.2, 1.5]}}}
        res = engine.analyze(race)
        m = res['market_only']
        self.assertIn(('複勝', [5]), [(b['type'], b['horses']) for b in m['bets']])
        self.assertIn({'type': '複勝', 'horses': [5]}, m['shared'])
        self.assertAlmostEqual(m['shared_share'], m['shared_stake'] / res['total_stake'])
        self.assertTrue(any(w.startswith('⚠ 市場のみの試算でも買い目あり') for w in engine.format_warnings(res)))
        # 単勝で代用したときの計算用市場の1着確率は単勝市場と同じ
        for k, v in res['win_market_prob'].items():
            self.assertAlmostEqual(res['calc_market_win_prob'][k], v)
        self.assertAlmostEqual(sum(res['model_win_prob'].values()), 1.0)
        # 市場のみの試算そのものには入れ子の試算を付けない
        self.assertIsNone(engine.analyze(race, model_weight=0)['market_only'])

    def test_market_label_in_result(self):
        hs = [horse(i, 60 + i) for i in range(1, 5)]
        race = {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': {str(i): 4.0 for i in range(1, 5)}}}
        self.assertIn('単勝オッズで代用（三連単 不足 24/24組）', engine.format_result(engine.analyze(race)))

    def test_allocation_uses_real_odds_and_same_distribution(self):
        orders = engine.harville(engine._normalize({1: 0.3, 2: 0.2, 3: 0.2, 4: 0.15, 5: 0.15}))['trifecta']
        bets = engine.candidate_bets(orders, {'win': {'5': 9.0}})
        self.assertEqual(len(bets), 1)
        self.assertAlmostEqual(bets[0]['prob'], sum(q for o, q in orders.items() if o[0] == 5))
        kept, _, growth = engine.allocate(bets, orders, capital=10000)
        x = kept[0]['stake']
        p = bets[0]['prob']
        expect = p * math.log((10000 - x + 9.0 * x) / 10000) + (1 - p) * math.log((10000 - x) / 10000)
        self.assertAlmostEqual(growth, expect)

    # 3. 重なる買い目は同時損失を含めて配分する
    def test_duplicate_bet_does_not_double_stake(self):
        p = engine._normalize({1: 0.3, 2: 0.2, 3: 0.2, 4: 0.15, 5: 0.15})
        tri = engine.harville(p)['trifecta']
        one = [engine._bet('単勝', (1,), p[1], 5.0)]
        two = one + [dict(one[0])]
        s1 = sum(b['stake'] for b in engine.allocate(one, tri)[0])
        s2 = sum(b['stake'] for b in engine.allocate(two, tri)[0])
        self.assertEqual(s1, s2)

    def test_umaren_umatan_limited_to_five(self):
        p = engine._normalize({i: 1.0 for i in range(1, 11)} | {1: 3.0, 2: 2.5, 3: 2.0})
        orders = engine.harville(p)['trifecta']
        umatan = {f'{a}>{b}': 1.6 / engine.ticket_probabilities(orders, 3)['umatan'][(a, b)]
                  for a in range(1, 6) for b in range(1, 6) if a != b}
        bets = engine.candidate_bets(orders, {'umatan': umatan, 'win': {'1': 1.5 / p[1]}})
        kept, _, _ = engine.allocate(bets, orders, capital=1_000_000)
        self.assertLessEqual(sum(1 for b in kept if b['type'] in ('馬連', '馬単')), 5)
        kept, _, _ = engine.allocate(bets, orders, capital=1_000_000, type_limits={})
        self.assertGreater(sum(1 for b in kept if b['type'] in ('馬連', '馬単')), 5)

    def test_longshot_bets_excluded(self):
        hs = [horse(i, 60, gate=(i + 1) // 2) for i in range(1, 9)]
        hs[7]['ability'] = 90  # 8番をモデルが高く見る
        win = {str(i): 6.0 for i in range(1, 8)} | {'8': 150.0}
        race = {'race': 'test', 'horses': hs, 'pace': 'middle',
                'odds': {'win': win, 'wide': {'1-8': [40.0, 50.0], '1-2': [3.0, 3.5]}, 'wakuren': {'4-4': 300.0}}}
        res = engine.analyze(race, model_weight=1.0)
        self.assertFalse([b for b in res['bets'] if 8 in b['horses'] and b['type'] != '枠連'])
        self.assertTrue(engine.involves_longshot({'type': '枠連', 'horses': [4, 4]}, {7: 6.0, 8: 150.0}, hs) is False)
        self.assertTrue(engine.involves_longshot({'type': '単勝', 'horses': [8]}, {8: 150.0}, hs))
        # 着順分布には残る（他馬の確率は8番の分だけ小さい）
        self.assertIn(8, res['win_prob'])

    def test_divergence_gate(self):
        hs = [horse(i, 60, gate=(i + 1) // 2) for i in range(1, 9)]
        hs[7]['ability'] = 90  # 8番だけモデルが市場よりはるかに高く見る
        win = {str(i): 6.0 for i in range(1, 8)} | {'8': 50.0}
        race = {'race': 'test', 'horses': hs, 'pace': 'middle', 'odds': {'win': win}}
        free = engine.analyze(race, divergence_gate=False)
        self.assertTrue([b for b in free['bets'] if 8 in b['horses']])
        res = engine.analyze(race)
        self.assertEqual(list(res['divergence_gate']['horses']), [8])
        self.assertFalse([b for b in res['bets'] if 8 in b['horses']])
        self.assertGreater(res['divergence_gate']['excluded'], 0)
        self.assertEqual(res['skip_reason'], 'divergence_gate')
        self.assertTrue(any('乖離ゲート' in line for line in engine.format_warnings(res)))
        # 根拠を確かめた馬はゲートで外さない
        ok = engine.analyze(dict(race, divergence_confirmed={'8': '確認済み'}))
        self.assertEqual(ok['divergence_gate']['horses'], {})
        self.assertTrue([b for b in ok['bets'] if 8 in b['horses']])
        # 枠連は枠の出走馬が全て該当するときだけ外す
        self.assertFalse(engine.relies_on({'type': '枠連', 'horses': [4, 4]}, {8: 5.0}, hs))
        self.assertTrue(engine.relies_on({'type': '枠連', 'horses': [4, 4]}, {7: 5.0, 8: 5.0}, hs))

    def test_ev_threshold_override(self):
        p = engine._normalize({i: 1.0 for i in range(1, 6)})
        orders = engine.harville(p)['trifecta']
        odds = {'win': {str(i): 1.03 / p[i] for i in range(1, 6)}}
        self.assertEqual(engine.candidate_bets(orders, odds), [])
        self.assertEqual(len(engine.candidate_bets(orders, odds, ev_threshold=1.0)), 5)

    # 2026/11/13 の購入ルール変更（期待値1.05・単複の勝率3%未満除外・最低1,000円）
    def test_threshold_is_105(self):
        self.assertEqual(engine.EV_THRESHOLD, 1.05)
        p = engine._normalize({i: 1.0 for i in range(1, 6)})
        orders = engine.harville(p)['trifecta']
        self.assertEqual(len(engine.candidate_bets(orders, {'win': {str(i): 1.06 / p[i] for i in range(1, 6)}})), 5)
        self.assertEqual(engine.candidate_bets(orders, {'win': {str(i): 1.04 / p[i] for i in range(1, 6)}}), [])

    def test_low_win_single_only_for_win_and_place(self):
        win = {1: 0.029, 2: 0.03}
        self.assertTrue(engine.low_win_single({'type': '単勝', 'horses': [1]}, win))
        self.assertTrue(engine.low_win_single({'type': '複勝', 'horses': [1]}, win))
        self.assertFalse(engine.low_win_single({'type': '単勝', 'horses': [2]}, win))
        for t in ('ワイド', '馬連', '馬単', '三連複', '三連単'):
            self.assertFalse(engine.low_win_single({'type': t, 'horses': [1, 2]}, win))

    def test_analyze_drops_longshot_win_but_keeps_exotics(self):
        nums = range(1, 9)
        race = {'race': 'T', 'horses': [{'num': n, 'gate': n, 'name': str(n), 'ability': 70, 'style': '差し',
                                         'gate_zone': '中', 'minus': [], 'plus': []} for n in nums]}
        # 8番は混合後の勝率が3%未満。単勝・ワイドとも期待値1.05を超えるオッズを付ける（乖離ゲートは別の仕組みなので外す）
        win = {str(n): 3.5 for n in range(1, 8)}
        win['8'] = 90.0
        race['odds'] = {'win': win, 'wide': {'1-8': [200.0, 220.0]}}
        res = engine.analyze(race, market='win', divergence_gate=False)
        self.assertLess(res['win_prob'][8], engine.WIN_PLACE_MIN_PROB)
        self.assertFalse(any(b['type'] == '単勝' and b['horses'] == [8] for b in res['bets']))
        self.assertGreaterEqual(res['low_win_excluded'], 1)
        self.assertTrue(any(b['type'] == 'ワイド' and b['horses'] == [1, 8] for b in res['bets']))

    def test_raise_to_minimum_keeps_ratio(self):
        p = engine._normalize({i: 1.0 for i in range(1, 6)})
        orders = engine.harville(p)['trifecta']
        bets = [dict(engine._bet('単勝', (1,), p[1], 6.0), stake=300), dict(engine._bet('単勝', (2,), p[2], 6.0), stake=100)]
        out, growth = engine.raise_to_minimum(bets, orders, 4000)
        self.assertEqual(sum(b['stake'] for b in out), 1000)
        self.assertEqual([(b['horses'], b['stake'], b['kelly_stake']) for b in out], [([1], 800, 300), ([2], 200, 100)])
        self.assertIsNotNone(growth)
        # 上限（土日の残額）が1,000円未満ならそこまで。1,000円以上なら変えない。買い目がなければ0円のまま
        self.assertEqual(sum(b['stake'] for b in engine.raise_to_minimum(bets, orders, 600)[0]), 600)
        big = [dict(bets[0], stake=1200)]
        self.assertEqual(engine.raise_to_minimum(big, orders, 4000), (big, None))
        self.assertEqual(engine.raise_to_minimum([], orders, 4000), ([], None))

    def test_frame_and_zone(self):
        self.assertEqual([engine.frame_of(n, 18) for n in (1, 2, 3, 13, 15, 16, 18)], [1, 1, 2, 7, 7, 8, 8])
        self.assertEqual([engine.frame_of(n, 16) for n in (1, 11, 16)], [1, 6, 8])
        self.assertEqual([engine.frame_of(n, 10) for n in (3, 7, 10)], [3, 7, 8])
        self.assertEqual([engine.frame_of(n, 12) for n in (4, 5, 11)], [4, 5, 8])
        self.assertEqual(engine.frame_of(5, 7), 5)
        self.assertEqual([engine.gate_zone_of(g) for g in range(1, 9)], ['内', '内', '中', '中', '中', '中', '外', '外'])

    def test_rules_change_date_recorded(self):
        cfg = engine.engine_config()
        self.assertEqual(cfg['rules_changed'][0]['date'], '2026-11-13')
        self.assertEqual((cfg['ev_threshold'], cfg['win_place_min_prob'], cfg['min_stake']), (1.05, 0.03, 1000))

    # 4. 100円単位で選び直し、丸めで消えた場合は理由を分ける
    def test_tiny_edges_still_allocated(self):
        # 旧方式では予算200円を15点に比例配分して全て100円未満→0点になった例
        p = engine._normalize({i: 1.0 for i in range(1, 8)})
        tri = engine.harville(p)['trifecta']
        bets = [engine._bet('三連単', o, tri[o], 1.21 / tri[o]) for o in list(tri)[:15]]
        self.assertTrue(all(b['kelly'] < 0.001 for b in bets))
        kept, reason, _ = engine.allocate(bets, tri, capital=80000)
        self.assertTrue(kept)
        self.assertIsNone(reason)
        # 基準資金8,000円では、この薄い優位には100円でも期待対数成長が増えない
        self.assertEqual(engine.allocate(bets, tri, capital=8000)[:2], ([], 'no_unit_allocation'))

    def test_unit_rounding_reason(self):
        p = engine._normalize({i: 1.0 for i in range(1, 9)})
        tri = engine.harville(p)['trifecta']
        bets = [engine._bet('三連単', (1, 2, 3), tri[(1, 2, 3)], 1.25 / tri[(1, 2, 3)])]
        kept, reason, _ = engine.allocate(bets, tri, capital=500)
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
                              'fetched_at': {'win': '2026-10-04T15:17:30+09:00'}}}
        engine.validate_final(race)
        for t in ('2026-10-04T15:20:05+09:00', '2026-10-04T15:14:50+09:00'):
            race['odds_meta']['fetched_at']['win'] = t
            with self.assertRaises(ValueError):
                engine.validate_final(race)
        race['odds_meta'] = {'kind': 'predicted', 'source': 'x', 'fetched_at': {'win': '2026-10-04T15:17:30+09:00'}}
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
        results = [{'race': 'A', 'growth': 0.004}, {'race': 'B', 'growth': 0.010},
                   {'race': 'C', 'growth': 0.0}, {'race': 'D', 'growth': 0.006}]
        self.assertEqual([r['race'] for r in engine.select_races(results)], ['B', 'D'])
        self.assertEqual(engine.select_races([{'race': 'C', 'growth': 0.0}]), [])


if __name__ == '__main__':
    unittest.main()
