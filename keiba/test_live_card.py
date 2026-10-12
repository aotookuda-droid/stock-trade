import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from keiba import engine, live_card

RECORD = Path(__file__).parent / 'records' / '20261004_毎日王冠.json'


def _data(html):
    return json.loads(re.search(r'^const D = (\{.*\});$', html, re.M).group(1))


class LiveCardTest(unittest.TestCase):
    def build(self, odds, asof=None, scratched=()):
        got = {'odds': odds, 'asof': asof, 'fetched_at': {}, 'scratched': list(scratched)}
        with mock.patch.object(live_card, 'fetch_odds', return_value=got), tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'card.html'
            live_card.main(str(out), str(RECORD))
            return _data(out.read_text(encoding='utf-8'))

    def test_title_follows_race_date(self):
        got = {'odds': None, 'asof': None, 'fetched_at': {}, 'scratched': []}
        with mock.patch.object(live_card, 'fetch_odds', return_value=got), tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'card.html'
            live_card.main(str(out), str(RECORD))
            html = out.read_text(encoding='utf-8')
        self.assertIn('<title>10/4 重賞 ', html)
        self.assertNotIn('__TITLE__', html)
        races = [{'race': '2026/10/11 東京11R'}, {'race': '2026/10/12 京都11R'}]
        self.assertEqual(live_card.page_title(races, datetime(2026, 10, 9)), '10/11・10/12 重賞 前日馬柱')
        self.assertEqual(live_card.page_title(races, datetime(2026, 10, 11, 15)), '10/11・10/12 重賞 馬柱')

    def test_falls_back_to_predicted_odds(self):
        d = self.build(None)
        race = d['races'][0]
        self.assertFalse(race['live'])
        self.assertIsNone(d['asof_iso'])
        self.assertAlmostEqual(sum(h['p'] for h in race['horses']), 1.0, places=3)

    def test_four_probabilities_are_shown_separately(self):
        race = self.build(None)['races'][0]
        for key in ('model', 'calc', 'p', 'mkt'):
            self.assertAlmostEqual(sum(h[key] for h in race['horses']), 1.0, places=3)
        # 単勝で代用したので、計算用市場は単勝市場と同じ
        self.assertEqual(race['market_source'], 'win_harville')
        for h in race['horses']:
            self.assertAlmostEqual(h['calc'], h['mkt'], places=3)
        self.assertIn('単勝オッズで代用', race['market'])
        self.assertIn('total_stake', race['market_only'])
        # 緩い基準の参考試算も並べる（買わない仮想買い目）
        self.assertEqual(race['loose']['ev_threshold'], engine.REFERENCE_LOOSE['ev_threshold'])
        self.assertTrue(all(b['ev'] >= race['loose']['ev_threshold'] for b in race['loose']['bets']))
        self.assertEqual(race['loose']['stake'], sum(b['stake'] for b in race['loose']['bets']))

    def test_scratched_horse_is_excluded(self):
        rec = json.loads(RECORD.read_text(encoding='utf-8'))
        win = {str(h['num']): h['pred_odds_1002'] for h in rec['horses'] if h['num'] != 8}
        d = self.build({'win': win}, '2026-10-03 15:00:00', scratched=[8])
        horses = {h['num']: h for h in d['races'][0]['horses']}
        self.assertTrue(horses[8]['scratched'])
        self.assertEqual(horses[8]['p'], 0.0)
        self.assertAlmostEqual(sum(h['p'] for h in horses.values()), 1.0, places=3)
        self.assertEqual(d['asof_iso'], '2026-10-03T15:00:00+09:00')

    def test_missing_odds_without_scratch_falls_back(self):
        rec = json.loads(RECORD.read_text(encoding='utf-8'))
        win = {str(h['num']): h['pred_odds_1002'] for h in rec['horses'] if h['num'] != 8}
        d = self.build({'win': win}, '2026-10-03 15:00:00')
        race = d['races'][0]
        self.assertFalse(race['live'])
        self.assertEqual(race['missing'], [8])
        self.assertFalse(race['horses'][7]['scratched'])
        self.assertIn('単勝オッズ欠損', d['header'])

    def test_jockey_name_from_base_notes_only(self):
        d = self.build(None)
        horses = {h['num']: h for h in d['races'][0]['horses']}
        self.assertEqual(horses[4]['jockey'], '西村淳')  # ルール照合メモの「短期免許騎手は…」を拾わない
        self.assertEqual(horses[7]['jockey'], '菊沢')

    def test_training_flags_marked_new(self):
        d = self.build(None)
        horses = {h['num']: h for h in d['races'][0]['horses']}
        self.assertEqual(horses[1]['new'], ['final_work_strong'])
        self.assertEqual(horses[13]['new'], ['second_start'])  # ルール照合での追加も★
        self.assertIn('叩き2戦目', horses[13]['rule'])
        self.assertNotIn('叩き2戦目', horses[13]['train'])


if __name__ == '__main__':
    unittest.main()


class LoadCodexTest(unittest.TestCase):
    DECISION = live_card.datetime.fromisoformat('2026-10-11T15:35:00+09:00')

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        patcher = mock.patch.object(live_card, 'CODEX_RECORDS', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def write(self, **over):
        rec = {'model': {'id': 'codex-independent-win-v2', 'author': 'Codex', 'status': 'unvalidated'},
               'probability_preparation': {'status': 'ready'},
               'prediction_meta': {'frozen_at': '2026-10-11T15:00:00+09:00'},
               'scenarios': [{'id': 'a', 'weight': 0.5, 'known_at': '2026-10-11T14:00:00+09:00',
                              'strengths': {'1': 6, '2': 3, '3': 1}},
                             {'id': 'b', 'weight': 0.5, 'known_at': '2026-10-11T14:00:00+09:00',
                              'strengths': {'1': 1, '2': 3, '3': 6}}],
               'horses': []}
        rec.update(over)
        (self.root / 'r.json').write_text(json.dumps(rec), encoding='utf-8')
        (self.root / 'index.json').write_text(json.dumps({'R': 'r.json'}), encoding='utf-8')
        return rec

    def test_independent_codex(self):
        self.write()
        c = live_card.load_codex('R', (), self.DECISION, [1, 2, 3])
        self.assertTrue(c['independent'])
        self.assertEqual(c['kind'], 'codex')
        self.assertIsNone(c['reason'])
        self.assertAlmostEqual(c['win'][1], 0.35)
        self.assertAlmostEqual(c['win'][2], 0.30)

    def test_claude_made_or_unknown_author_is_reference(self):
        self.write(model={'id': 'claude-reviewed-scenarios-20261003', 'status': 'unvalidated'})
        c = live_card.load_codex('R', (), self.DECISION, [1, 2, 3])
        self.assertFalse(c['independent'])
        self.assertEqual(c['kind'], 'codex_reference')
        self.assertEqual(c['author'], '不明')

    def test_not_adopted_when_incomplete_or_late(self):
        cases = [({'probability_preparation': {'status': 'draft'}}, 'draft'),
                 ({'prediction_meta': {'frozen_at': '2026-10-11T15:40:00+09:00'}}, '判断時刻より後に確定'),
                 ({'prediction_meta': {}}, '確定時刻'),
                 ({'scenarios': [{'id': 'a', 'weight': 1, 'strengths': {'1': 1, '2': 2}}]}, '網羅していない'),
                 ({'scenarios': [{'id': 'a', 'weight': 1, 'strengths': {'1': 1, '2': 0, '3': 1}}]}, '正の有限値でない'),
                 ({'horses': [{'num': 1, 'evidence': [{'known_at': '2026-10-11T15:36:00+09:00', 'note': 'x'}]}]},
                  '判断時刻より後の根拠')]
        for over, word in cases:
            self.write(**over)
            c = live_card.load_codex('R', (), self.DECISION, [1, 2, 3])
            self.assertIsNone(c['win'], word)
            self.assertIn(word, c['reason'])

    def test_scratch_is_removed_within_each_scenario(self):
        self.write()
        c = live_card.load_codex('R', [3], self.DECISION, [1, 2, 3])
        # シナリオa: 1=6/9, 2=3/9、シナリオb: 1=1/4, 2=3/4 を等しい重みで平均（混合後の再正規化 0.35/0.65 とは違う）
        self.assertAlmostEqual(c['win'][1], (6 / 9 + 1 / 4) / 2)
        self.assertNotIn(3, c['win'])
        self.assertIn('取消 [3]番', c['scratch_note'])


class HorseIdsTest(unittest.TestCase):
    def test_reads_horse_number_not_gate(self):
        from keiba.research import history
        page = ('<tr class="HorseList"><td class="Waku2 Txt_C">2</td><td class="Umaban2 Txt_C">3</td>'
                '<a href="https://db.netkeiba.com/horse/2019102632">x</a></tr>'
                '<tr class="HorseList" id="tr_3"><td class="Waku1">1</td><td class="Waku">1</td>'
                '<a href="https://db.netkeiba.com/horse/2020100001">y</a></tr>')
        with tempfile.TemporaryDirectory() as d, mock.patch.object(history, 'CACHE', Path(d)), \
                mock.patch.object(history, '_get', return_value=page):
            self.assertEqual(live_card.horse_ids('R'), {3: '2019102632', 1: '2020100001'})

    def test_reference_failure_does_not_break_card(self):
        with mock.patch.object(live_card, 'horse_ids', side_effect=OSError('offline')):
            self.assertIsNone(live_card.reference_abilities({'race_id': 'R', 'race': '2026/10/04'}, []))
