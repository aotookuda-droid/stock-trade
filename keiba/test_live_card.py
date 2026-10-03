import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from keiba import live_card

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

    def test_falls_back_to_predicted_odds(self):
        d = self.build(None)
        race = d['races'][0]
        self.assertFalse(race['live'])
        self.assertIsNone(d['asof_iso'])
        self.assertAlmostEqual(sum(h['p'] for h in race['horses']), 1.0, places=3)

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

    def test_training_flags_marked_new(self):
        d = self.build(None)
        horses = {h['num']: h for h in d['races'][0]['horses']}
        self.assertEqual(horses[1]['new'], ['final_work_strong'])
        self.assertEqual(horses[13]['new'], ['second_start'])  # ルール照合での追加も★
        self.assertIn('叩き2戦目', horses[13]['rule'])
        self.assertNotIn('叩き2戦目', horses[13]['train'])


if __name__ == '__main__':
    unittest.main()
