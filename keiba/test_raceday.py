import fcntl
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from itertools import permutations
from pathlib import Path

from keiba import engine, raceday

RECORD = Path(__file__).parent / 'records' / '20261004_京都大賞典.json'
LEDGER = Path(__file__).parent / 'records' / 'ledger_20261003.json'


def at(hms):
    return lambda: datetime.fromisoformat(f'2026-10-04T{hms}+09:00')


class RacedayTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rec = self.dir / 'r.json'
        self.led = self.dir / 'l.json'
        # 実レコードは10/4に判断済みなので、判断前の状態（decision・result などを外し、台帳を未判断）に戻して使う
        rec = json.loads(RECORD.read_text(encoding='utf-8'))
        for key in ('decision', 'result', 'reference', 'probabilities', 'odds_meta'):
            rec.pop(key, None)
        rec['odds'] = {}
        led = json.loads(LEDGER.read_text(encoding='utf-8'))
        for e in led['races']:
            for key in ('status', 'stake', 'decided_at'):
                e.pop(key, None)
        self.rec.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        self.led_open = json.dumps(led, ensure_ascii=False)
        self.led.write_text(self.led_open, encoding='utf-8')
        horses = rec['horses']
        self.win = {str(h['num']): h['pred_odds_1002'] for h in horses}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def fetch(self, scratched=(), drop=(), tri_sequence=None):
        win = {k: v for k, v in self.win.items() if int(k) not in drop}
        seq = list(tri_sequence or [None])

        def f(rid):
            tri = seq.pop(0) if len(seq) > 1 else seq[0]
            odds = {'win': win} | ({'sanrentan': tri} if tri else {})
            return {'odds': odds, 'asof': '2026-10-04 15:17:00', 'scratched': list(scratched),
                    'fetched_at': {k: '2026-10-04T15:17:35+09:00' for k in odds}}
        return f

    def full_tri(self, drop_one=False):
        nums = [int(k) for k in self.win]
        tri = {f'{a}>{b}>{c}': 500.0 for a, b, c in permutations(nums, 3)}
        if drop_one:
            tri.pop('1>2>3')
        return tri

    def test_decides_within_window_and_records_ledger(self):
        slept = []
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:12:00'), sleep=slept.append, fetch=self.fetch()), 'decided')
        self.assertAlmostEqual(slept[0], 330)
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual(rec['odds_meta']['kind'], 'observed')
        led = json.loads(self.led.read_text(encoding='utf-8'))
        kyoto = [e for e in led['races'] if e['race_id'] == '202608040211'][0]
        self.assertEqual((kyoto['status'], kyoto['stake']), ('decided', rec['result']['total_stake']))
        # 答え合わせ用に出どころ別の勝率を判断時点で記録する（買い目には使わない）
        probs = rec['probabilities']
        self.assertLessEqual({'claude_model', 'market_win', 'final'}, set(probs))
        for p in probs.values():
            self.assertAlmostEqual(sum(p.values()), 1, places=3)

    def test_late_start_is_skip(self):
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:21:00'), fetch=self.fetch()), 'missed')
        led = json.loads(self.led.read_text(encoding='utf-8'))
        self.assertEqual(led['races'][0]['stake'], 0)

    def test_too_early_does_nothing(self):
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:00:00'), fetch=self.fetch()), 'early')

    def test_unexplained_missing_odds_stops(self):
        with self.assertRaises(SystemExit):
            raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=self.fetch(drop=[5]))

    def test_explicit_scratch_is_excluded(self):
        raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=self.fetch(scratched=[5], drop=[5]))
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual(rec['scratched'], [5])
        self.assertNotIn('5', rec['result']['win_prob'])

    def test_rerun_after_decision_does_not_overwrite(self):
        raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=self.fetch())
        rec_before, led_before = self.rec.read_text(encoding='utf-8'), self.led.read_text(encoding='utf-8')
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:18:30'), sleep=lambda s: None,
                                     fetch=self.fetch(drop=[1, 2])), 'already')
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:30:00'), fetch=self.fetch()), 'already')
        self.assertEqual(engine.main(str(self.rec), str(self.led)), 'already')
        self.assertEqual(engine.main(str(self.rec)), 'already')
        self.assertEqual(self.rec.read_text(encoding='utf-8'), rec_before)
        self.assertEqual(self.led.read_text(encoding='utf-8'), led_before)

    def test_recovers_ledger_after_interrupted_save(self):
        raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=self.fetch())
        stake = json.loads(self.rec.read_text(encoding='utf-8'))['decision']['stake']
        self.led.write_text(self.led_open, encoding='utf-8')  # 台帳だけ保存前に止まった状態（未判断の台帳に戻す）
        rec_before = self.rec.read_text(encoding='utf-8')
        self.assertEqual(raceday.run(self.rec, self.led, now=at('15:18:30'), fetch=self.fetch()), 'already')
        led = json.loads(self.led.read_text(encoding='utf-8'))
        self.assertEqual(led['races'][0]['stake'], stake)
        self.assertEqual(self.rec.read_text(encoding='utf-8'), rec_before)

    def test_concurrent_run_is_refused(self):
        with open(f'{self.led}.lock', 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit):
                raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=self.fetch())
        self.assertNotIn('decision', json.loads(self.rec.read_text(encoding='utf-8')))

    def test_trifecta_refetched_when_incomplete(self):
        slept = []
        fetch = self.fetch(tri_sequence=[self.full_tri(drop_one=True), self.full_tri()])
        raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=slept.append, fetch=fetch)
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual(rec['result']['market_source'], 'sanrentan')
        self.assertEqual(slept, [10.0])

    def test_trifecta_still_missing_falls_back_visibly(self):
        fetch = self.fetch(tri_sequence=[self.full_tri(drop_one=True)])
        raceday.run(self.rec, self.led, now=at('15:18:00'), sleep=lambda s: None, fetch=fetch)
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual((rec['result']['market_source'], rec['result']['sanrentan_missing']), ('win_harville', 1))
        self.assertIn('単勝オッズで代用（三連単 不足 1/4896組）', engine.format_result(rec['result']))


if __name__ == '__main__':
    unittest.main()
