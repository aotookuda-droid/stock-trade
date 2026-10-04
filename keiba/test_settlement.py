import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from keiba import settlement

HERE = Path(__file__).parent
PAGE = (HERE / 'fixtures' / 'result_202605040111.html').read_text(encoding='utf-8')
OFFICIAL = settlement.parse_result(PAGE)
NO_WIDE = re.sub(r'<tr class="Wide">.*?</tr>', '', PAGE, flags=re.S)


def horse_row(page, num):
    """結果ページの着順表で馬番 num の行（開始位置, 終了位置）。"""
    for m in re.finditer(r'<tr[^>]*HorseList[^>]*>(.*?)</tr>', page, re.S):
        if re.search(r'<td class="Num Txt_C">\s*<div>%d</div>' % num, m.group(1)):
            return m.start(), m.end()
    raise AssertionError(num)


def edit_row(page, num, fn):
    a, b = horse_row(page, num)
    return page[:a] + fn(page[a:b]) + page[b:]


def set_rank(page, num, text):
    return edit_row(page, num, lambda r: re.sub(r'<div class="Rank">.*?</div>', f'<div class="Rank">{text}</div>', r, flags=re.S))


def bet(t, hs, x):
    return {'type': t, 'horses': hs, 'stake': x}


def record(bets, ref=None):
    """結果ページ（2026/10/03 東京11R）に対応する判断済みの記録。"""
    return {'race_id': '202605040111', 'race': '2026/10/03 東京11R グリーンチャンネルC(L) 芝1600',
            'start_at': '2026-10-03T15:45:00+09:00', 'field_size_at_sales_start': 15, 'pace': 'middle',
            'horses': [{'num': n, 'gate': g, 'name': f'馬{n}', 'ability': 70, 'notes': ''}
                       for n, g in sorted(OFFICIAL['gates'].items())],
            'result': {'bets': bets}, 'reference': {'win_market_only': {'bets': ref or []}},
            'decision': {'stake': sum(b['stake'] for b in bets), 'decided_at': '2026-10-03T14:45:00+09:00'}}


class ParseTest(unittest.TestCase):
    def test_parse_official_page(self):
        self.assertEqual(OFFICIAL['race_id'], '202605040111')
        self.assertEqual(OFFICIAL['finish'][:3], [5, 4, 1])
        self.assertEqual(OFFICIAL['gates'][5], 3)
        self.assertEqual(OFFICIAL['payouts']['複勝'], {'5': 150, '4': 120, '1': 210})
        self.assertEqual(OFFICIAL['payouts']['ワイド']['1-5'], 580)
        self.assertEqual(OFFICIAL['payouts']['馬単'], {'5>4': 1200})
        self.assertEqual(OFFICIAL['payouts']['三連単'], {'5>4>1': 5610})
        self.assertEqual(OFFICIAL['payouts']['枠連'], {'3-3': 520})


class SettleBetsTest(unittest.TestCase):
    def test_hits_misses_and_status(self):
        bets = [bet('単勝', [5], 300), bet('複勝', [1], 200), bet('馬単', [4, 5], 100),
                bet('三連単', [5, 4, 1], 100), bet('ワイド', [1, 5], 100)]
        v = settlement.settle_bets(bets, OFFICIAL, OFFICIAL['gates'])
        self.assertEqual(v['stake'], 800)
        self.assertEqual(v['payout'], 420 * 3 + 210 * 2 + 0 + 5610 + 580)
        self.assertEqual([r['status'] for r in v['tickets']], ['的中', '的中', '不的中', '的中', '的中'])
        self.assertAlmostEqual(v['roi'], v['payout'] / 800)

    def test_refund_for_scratched_horse(self):
        gates = dict(OFFICIAL['gates'])  # 9番は5枠（8番と同枠）
        v = settlement.settle_bets([bet('単勝', [9], 500), bet('馬連', [4, 9], 200), bet('枠連', [3, 5], 100),
                                    bet('単勝', [5], 100)], dict(OFFICIAL, scratched=[9]), gates)
        self.assertEqual(v['refund'], 700)  # 枠連は5枠に8番が残るので返還しない
        self.assertEqual([r['status'] for r in v['tickets']], ['返還', '返還', '不的中', '的中'])
        self.assertAlmostEqual(v['roi'], 420 / 200)  # 返還分を除いた200円が実質投資
        v = settlement.settle_bets([bet('枠連', [3, 5], 100)], dict(OFFICIAL, scratched=[8, 9]), gates)
        self.assertEqual(v['refund'], 100)
        v = settlement.settle_bets([bet('枠連', [3, 3], 100)], dict(OFFICIAL, scratched=[4]), gates)
        self.assertEqual(v['refund'], 100)  # 同枠は2頭残らなければ返還

    def test_dead_heat_pays_each_listed_ticket(self):
        official = dict(OFFICIAL, payouts=OFFICIAL['payouts'] | {'単勝': {'5': 300, '4': 250}})
        v = settlement.settle_bets([bet('単勝', [5], 100), bet('単勝', [4], 100)], official, {})
        self.assertEqual(v['payout'], 550)


class SettleRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rec = self.dir / 'r.json'
        self.out = self.dir / 'settlements'
        self.write(record([bet('単勝', [5], 300), bet('ワイド', [1, 5], 200)], ref=[bet('馬連', [4, 5], 100)]))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, rec):
        self.rec.write_text(json.dumps(rec, ensure_ascii=False), encoding='utf-8')

    def test_settles_matching_record_without_touching_it(self):
        before = self.rec.read_text(encoding='utf-8')
        s = settlement.settle(self.rec, PAGE, 'fixture')
        self.assertEqual(self.rec.read_text(encoding='utf-8'), before)
        self.assertEqual(s['plans']['本番']['payout'], 420 * 3 + 580 * 2)
        self.assertEqual(s['plans']['参考（単勝市場のみ）']['payout'], 550)

    def test_other_race_page_is_refused(self):
        kyoto = json.loads((HERE / 'records' / '20261004_京都大賞典.json').read_text(encoding='utf-8'))
        kyoto['decision'] = {'stake': 0, 'decided_at': 'x'}
        kyoto['result'] = {'bets': []}
        self.write(kyoto)
        with self.assertRaises(SystemExit):
            settlement.settle(self.rec, PAGE, 'fixture')

    def test_gate_mismatch_is_refused(self):
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        rec['horses'][0]['gate'] = 8
        self.write(rec)
        with self.assertRaises(SystemExit):
            settlement.settle(self.rec, PAGE, 'fixture')

    def test_missing_payout_table_is_incomplete_not_a_miss(self):
        with self.assertRaises(settlement.Incomplete):
            settlement.settle(self.rec, NO_WIDE, 'fixture')
        # 取り直して揃えば精算、揃わなければ何も保存しない
        pages = [NO_WIDE, PAGE]
        self.assertEqual(settlement.fetch(self.rec, get=lambda u: pages.pop(0), sleep=lambda s: None,
                                          directory=self.out), 'saved')
        saved = json.loads((self.out / '202605040111.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['plans']['本番']['payout'], 420 * 3 + 580 * 2)
        shutil.rmtree(self.out)
        self.assertEqual(settlement.fetch(self.rec, get=lambda u: NO_WIDE, sleep=lambda s: None,
                                          directory=self.out), 'incomplete')
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_missing_frame_only_is_incomplete(self):
        page = edit_row(PAGE, 7, lambda r: re.sub(r'<td class="Num Waku\d">', '<td class="Num">', r))
        with self.assertRaisesRegex(settlement.Incomplete, '7番の枠がない'):
            settlement.settle(self.rec, page, 'fixture')

    def test_missing_whole_row_is_incomplete(self):
        a, b = horse_row(PAGE, 12)
        with self.assertRaisesRegex(settlement.Incomplete, '12番の結果行がない'):
            settlement.settle(self.rec, PAGE[:a] + PAGE[b:], 'fixture')
        # 取り直して揃えば精算する（記録側の枠や状態で補わない）
        pages = [PAGE[:a] + PAGE[b:], PAGE]
        self.assertEqual(settlement.fetch(self.rec, get=lambda u: pages.pop(0), sleep=lambda s: None,
                                          directory=self.out), 'saved')

    def test_unreadable_status_is_incomplete(self):
        with self.assertRaisesRegex(settlement.Incomplete, '13番の出走状態'):
            settlement.settle(self.rec, set_rank(PAGE, 13, ''), 'fixture')

    def test_scratch_exclusion_stop_and_disqualification(self):
        page = set_rank(set_rank(set_rank(set_rank(PAGE, 7, '取消'), 8, '除外'), 9, '中止'), 10, '失格')
        official = settlement.parse_result(page)
        self.assertEqual({n: official['status'][n] for n in (7, 8, 9, 10, 5)},
                         {7: '取消', 8: '除外', 9: '中止', 10: '失格', 5: '完走'})
        self.assertEqual(official['scratched'], [7, 8])
        self.write(record([bet('単勝', [7], 100), bet('単勝', [8], 100), bet('単勝', [9], 100),
                           bet('単勝', [10], 100), bet('単勝', [5], 100)]))
        v = settlement.settle(self.rec, page, 'fixture')['plans']['本番']
        self.assertEqual([t['status'] for t in v['tickets']], ['返還', '返還', '不的中', '不的中', '的中'])
        self.assertEqual((v['refund'], v['payout']), (200, 420))

    def test_short_place_table_is_incomplete(self):
        self.write(record([bet('複勝', [1], 100)]))
        short = PAGE.replace('<span>150円<br />120円<br />210円</span>', '<span>150円<br />120円</span>')
        short = re.sub(r'(<tr class="Fukusho">.*?)<div><span>1</span></div>', r'\1', short, flags=re.S)
        with self.assertRaises(settlement.Incomplete):
            settlement.settle(self.rec, short, 'fixture')

    def test_undecided_race_is_not_settled(self):
        rec = json.loads(self.rec.read_text(encoding='utf-8'))
        del rec['decision']
        self.write(rec)
        with self.assertRaises(SystemExit):
            settlement.settle(self.rec, PAGE, 'fixture')

    def test_save_report_and_change_detection(self):
        s = settlement.settle(self.rec, PAGE, 'fixture')
        self.assertEqual(settlement.save(s, self.out)[1], 'saved')
        self.assertEqual(settlement.save(s, self.out)[1], 'unchanged')
        with self.assertRaises(SystemExit):
            settlement.save(dict(s, official=dict(s['official'], finish=[1, 2, 3])), self.out)
        text = settlement.report(self.out)
        for word in ('### レース別', '### 週末別', '2026/10/03週', '### 累計'):
            self.assertIn(word, text)
        self.assertNotIn('## 警告', text)
        self.assertNotIn('## 勝率の答え合わせ', text)
        # 分析・根拠・枠・展開のどれを変えても警告する
        for change in (lambda r: r['horses'][0].update(ability=99), lambda r: r['horses'][0].update(notes='後から追記'),
                       lambda r: r['horses'][0].update(gate=8), lambda r: r.update(pace='slow'),
                       lambda r: r.update(bias={'inner_outer': 'inner'})):
            rec = record([bet('単勝', [5], 300), bet('ワイド', [1, 5], 200)], ref=[bet('馬連', [4, 5], 100)])
            change(rec)
            self.write(rec)
            self.assertIn('精算後に変更', settlement.report(self.out))

    def test_probability_check(self):
        winner = str(OFFICIAL['finish'][0])
        rec = record([bet('単勝', [5], 300)])
        rec['probabilities'] = {'claude_model': {winner: 0.2}, 'market_win': {winner: 0.1},
                                'final': {winner: 0.15}, 'codex': {winner: 0.25}}
        self.write(rec)
        settlement.save(settlement.settle(self.rec, PAGE, 'fixture'), self.out)
        text = settlement.report(self.out)
        self.assertIn('## 勝率の答え合わせ', text)
        self.assertIn('| 20.0% | 25.0% | 10.0% | 15.0% |', text)
        self.assertIn('| 平均対数損失（全て記録あり1レース） | 1.609 | 1.386 | 2.303 | 1.897 |', text)
        self.assertNotIn('Codex以外が記録あり', text)
        # Codex の記録が無いレースは「未記録」で、共通レースから外す（平均は比べられる集合だけ）
        del rec['probabilities']['codex']
        self.write(rec)
        text = settlement.report(self.out)
        self.assertIn('| 20.0% | 未記録 | 10.0% | 15.0% |', text)
        self.assertIn('（全て記録あり0レース） | — | — | — | — |', text)
        self.assertIn('（Codex以外が記録あり1レース） | 1.609 | — | 2.303 | 1.897 |', text)
        # 勝ち馬に0を付けていた（記録はある）なら損失は無限大で、平均から外さない
        rec['probabilities']['codex'] = {winner: 0.0}
        self.write(rec)
        text = settlement.report(self.out)
        self.assertIn('| 20.0% | 0（損失∞） | 10.0% | 15.0% |', text)
        self.assertIn('（全て記録あり1レース） | 1.609 | ∞ | 2.303 | 1.897 |', text)

if __name__ == '__main__':
    unittest.main()
