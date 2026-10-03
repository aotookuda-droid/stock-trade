import json
import shutil
import tempfile
import unittest
from datetime import datetime
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
        shutil.copy(RECORD, self.rec)
        shutil.copy(LEDGER, self.led)
        horses = json.loads(RECORD.read_text(encoding='utf-8'))['horses']
        self.win = {str(h['num']): h['pred_odds_1002'] for h in horses}

    def tearDown(self):
        shutil.rmtree(self.dir)

    def fetch(self, scratched=(), drop=()):
        win = {k: v for k, v in self.win.items() if int(k) not in drop}
        return lambda rid: {'odds': {'win': win}, 'asof': '2026-10-04 14:27:00',
                            'fetched_at': {'win': '2026-10-04T14:27:35+09:00'}, 'scratched': list(scratched)}

    def test_decides_within_window_and_records_ledger(self):
        slept = []
        self.assertEqual(raceday.run(self.rec, self.led, now=at('14:22:00'), sleep=slept.append, fetch=self.fetch()), 'decided')
        self.assertAlmostEqual(slept[0], 330)
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual(rec['odds_meta']['kind'], 'observed')
        led = json.loads(self.led.read_text(encoding='utf-8'))
        kyoto = [e for e in led['races'] if e['race_id'] == '202608040211'][0]
        self.assertEqual((kyoto['status'], kyoto['stake']), ('decided', rec['result']['total_stake']))

    def test_late_start_is_skip(self):
        self.assertEqual(raceday.run(self.rec, self.led, now=at('14:31:00'), fetch=self.fetch()), 'missed')
        led = json.loads(self.led.read_text(encoding='utf-8'))
        self.assertEqual(led['races'][0]['stake'], 0)

    def test_too_early_does_nothing(self):
        self.assertEqual(raceday.run(self.rec, self.led, now=at('14:00:00'), fetch=self.fetch()), 'early')

    def test_unexplained_missing_odds_stops(self):
        with self.assertRaises(SystemExit):
            raceday.run(self.rec, self.led, now=at('14:28:00'), sleep=lambda s: None, fetch=self.fetch(drop=[5]))

    def test_explicit_scratch_is_excluded(self):
        raceday.run(self.rec, self.led, now=at('14:28:00'), sleep=lambda s: None, fetch=self.fetch(scratched=[5], drop=[5]))
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        self.assertEqual(rec['scratched'], [5])
        self.assertNotIn('5', rec['result']['win_prob'])


if __name__ == '__main__':
    unittest.main()
