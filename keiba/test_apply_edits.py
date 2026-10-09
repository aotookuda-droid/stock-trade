import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from keiba import apply_edits as A


def record():
    return {'race_id': 'R1', 'horses': [
        {'num': 13, 'name': 'ワタシマツワ', 'ability': 56, 'style': '先行', 'gate_zone': '外',
         'minus': ['turf_to_outer'], 'plus': ['breaking_out'], 'flag_adjust': {'breaking_out': -1}, 'notes': 'メモ。'}]}


def edit(**kw):
    e = {'race_id': 'R1', 'num': 13, 'name': 'ワタシマツワ', 'plus': ['breaking_out'], 'minus': [],
         'flag_adjust': {'breaking_out': -2}, 'note': '理由',
         'base': {'plus': ['breaking_out'], 'minus': ['turf_to_outer'], 'flag_adjust': {'breaking_out': -1}}}
    e.update(kw)
    return e


class ApplyEditsTest(unittest.TestCase):
    def test_applies_and_notes_change(self):
        rec = record()
        applied, _ = A.apply([edit()], rec, datetime(2026, 10, 9))
        h = rec['horses'][0]
        self.assertEqual(applied, [13])
        self.assertEqual((h['minus'], h['flag_adjust']), ([], {'breaking_out': -2}))
        self.assertIn('（10/9 ブラウザで加減点を調整）turf_to_outerを削除、breaking_out +3→+2。理由: 理由。', h['notes'])
        self.assertEqual(A.apply([edit()], rec)[0], [])  # 2回目は何もしない

    def test_conflict_when_record_changed_after_edit(self):
        rec = record()
        rec['horses'][0]['plus'].append('second_start')
        applied, report = A.apply([edit()], rec)
        self.assertEqual(applied, [])
        self.assertIn('記録の加減点が変わっている', report[0])

    def test_invalid_discount_is_rejected(self):
        rec = record()
        applied, report = A.apply([edit(flag_adjust={'breaking_out': 2})], rec)
        self.assertEqual(applied, [])
        self.assertIn('割り引きだけ', report[0])
        self.assertEqual(rec['horses'][0]['flag_adjust'], {'breaking_out': -1})

    def test_dropping_adjust_removes_field_and_unwraps_listing(self):
        rec = record()
        edits = A.load_edits({'docs': [{'id': 'R1-13', 'data': edit(flag_adjust={})}]})
        A.apply(edits, rec)
        self.assertNotIn('flag_adjust', rec['horses'][0])

    def test_reads_out_dir_listing(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / 'edits'
            sub.mkdir()
            (sub / 'R1-13.json').write_text(json.dumps(edit(), ensure_ascii=False), encoding='utf-8')
            self.assertEqual([e['num'] for e in A.read_edits(d)], [13])
            self.assertEqual(A.read_edits(Path(d) / 'none'), [])


if __name__ == '__main__':
    unittest.main()
