import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from keiba import engine, raceday, settlement

HERE = Path(__file__).parent
PAGE = (HERE / 'fixtures' / 'result_202605040111.html').read_text(encoding='utf-8')
OFFICIAL = settlement.parse_result(PAGE)


def bet(t, hs, x):
    return {'type': t, 'horses': hs, 'stake': x}


class SettlementTest(unittest.TestCase):
    def test_parse_official_page(self):
        self.assertEqual(OFFICIAL['finish'][:3], [5, 4, 1])
        self.assertEqual(OFFICIAL['payouts']['複勝'], {'5': 150, '4': 120, '1': 210})
        self.assertEqual(OFFICIAL['payouts']['ワイド']['1-5'], 580)
        self.assertEqual(OFFICIAL['payouts']['馬単'], {'5>4': 1200})
        self.assertEqual(OFFICIAL['payouts']['三連単'], {'5>4>1': 5610})
        self.assertEqual(OFFICIAL['payouts']['枠連'], {'3-3': 520})

    def test_settle_hits_and_misses(self):
        gates = {i: (i + 1) // 2 for i in range(1, 16)}
        bets = [bet('単勝', [5], 300), bet('複勝', [1], 200), bet('馬単', [4, 5], 100),
                bet('三連単', [5, 4, 1], 100), bet('ワイド', [1, 5], 100)]
        v = settlement.settle_bets(bets, OFFICIAL, gates)
        self.assertEqual(v['stake'], 800)
        self.assertEqual(v['payout'], 420 * 3 + 210 * 2 + 0 + 5610 + 580)
        self.assertEqual(v['profit'], v['payout'] - 800)
        self.assertAlmostEqual(v['roi'], v['payout'] / 800)

    def test_refund_for_scratched_horse(self):
        official = dict(OFFICIAL, scratched=[9])
        gates = {i: (i + 1) // 2 for i in range(1, 16)}  # 9番は5枠（10番と同枠）
        v = settlement.settle_bets([bet('単勝', [9], 500), bet('馬連', [4, 9], 200), bet('枠連', [3, 5], 100),
                                    bet('単勝', [5], 100)], official, gates)
        self.assertEqual(v['refund'], 700)  # 枠連は5枠に10番が残るので返還しない
        self.assertEqual(v['payout'], 420)
        self.assertAlmostEqual(v['roi'], 420 / 200)  # 返還分を除いた200円が実質投資
        official = dict(OFFICIAL, scratched=[9, 10])
        v = settlement.settle_bets([bet('枠連', [3, 5], 100)], official, gates)
        self.assertEqual(v['refund'], 100)
        gates[6] = 3  # 3枠が 5・6 の2頭
        v = settlement.settle_bets([bet('枠連', [3, 3], 100)], dict(OFFICIAL, scratched=[6]), gates)
        self.assertEqual(v['refund'], 100)  # 同枠は2頭残らなければ返還

    def test_dead_heat_pays_each_listed_ticket(self):
        official = dict(OFFICIAL, payouts=OFFICIAL['payouts'] | {'単勝': {'5': 300, '4': 250}})
        v = settlement.settle_bets([bet('単勝', [5], 100), bet('単勝', [4], 100)], official, {})
        self.assertEqual(v['payout'], 550)


class SettleRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rec, self.led = self.dir / 'r.json', self.dir / 'l.json'
        shutil.copy(HERE / 'records' / '20261004_京都大賞典.json', self.rec)
        shutil.copy(HERE / 'records' / 'ledger_20261003.json', self.led)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def decide(self):
        horses = json.loads(self.rec.read_text(encoding='utf-8'))['horses']
        win = {str(h['num']): h['pred_odds_1002'] for h in horses}
        fetch = lambda rid: {'odds': {'win': win, 'place': {k: [2.0, 3.0] for k in win}}, 'asof': 'x', 'scratched': [],
                             'fetched_at': {'win': '2026-10-04T14:27:35+09:00', 'place': '2026-10-04T14:27:35+09:00'}}
        raceday.run(self.rec, self.led, now=lambda: datetime.fromisoformat('2026-10-04T14:28:00+09:00'),
                    sleep=lambda s: None, fetch=fetch)

    def test_undecided_race_is_not_settled(self):
        with self.assertRaises(SystemExit):
            settlement.settle(self.rec, PAGE, 'fixture')

    def test_settle_does_not_touch_record_and_reports(self):
        self.decide()
        before = self.rec.read_text(encoding='utf-8')
        s = settlement.settle(self.rec, PAGE, 'fixture')
        self.assertEqual(self.rec.read_text(encoding='utf-8'), before)
        self.assertEqual(set(s['plans']), {'本番', '参考（単勝市場のみ）'})
        rec = json.loads(before)
        self.assertEqual(s['plans']['本番']['stake'], rec['decision']['stake'])
        out = self.dir / 'settlements'
        self.assertEqual(settlement.save(s, out)[1], 'saved')
        self.assertEqual(settlement.save(s, out)[1], 'unchanged')
        changed = dict(s, official=dict(s['official'], finish=[1, 2, 3]))
        with self.assertRaises(SystemExit):
            settlement.save(changed, out)
        text = settlement.report(out)
        self.assertIn('### 週末別', text)
        self.assertIn('2026/10/03週', text)
        self.assertIn('### 累計', text)
        self.assertNotIn('## 警告', text)
        rec['decision']['stake'] = 1
        self.rec.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')
        self.assertIn('精算後に変更', settlement.report(out))


if __name__ == '__main__':
    unittest.main()
