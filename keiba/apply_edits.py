"""馬柱ページ（ブラウザ）で編集した加減点を、レース記録（records/*.json）に反映する。

使い方: python3 -m keiba.apply_edits 編集.json keiba/records/20261011_アイルランドトロフィー.json [...]

編集.json は馬柱ページの db（コレクション edits）を ArtifactData で list した結果を保存したもの。
1件は {race_id, num, name, plus, minus, flag_adjust, note, base, saved_at}。base は編集を始めたときの記録の
加減点で、記録がその後に変わっていれば（別の更新と競合）反映せずに報告する。
フラグ名・割り引きの範囲は engine.flag_points と同じ検査を通ったものだけ反映し、notes に変更内容と理由を追記する。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from keiba import engine

FIELDS = ('plus', 'minus', 'flag_adjust')


def state(h):
    return {'plus': sorted(h.get('plus', [])), 'minus': sorted(h.get('minus', [])),
            'flag_adjust': {k: v for k, v in sorted((h.get('flag_adjust') or {}).items())}}


def load_edits(raw):
    """ArtifactData の出力の形に依らず、race_id と num を持つ辞書を拾う（{id, data} 包みも外す）。"""
    out = []

    def walk(x):
        if isinstance(x, dict):
            body = x.get('data') if isinstance(x.get('data'), dict) else x
            if 'race_id' in body and 'num' in body:
                out.append(body)
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(raw)
    return out


def describe(before, after):
    parts = []
    for side in ('plus', 'minus'):
        added = [k for k in after[side] if k not in before[side]]
        removed = [k for k in before[side] if k not in after[side]]
        parts += [f'{k}を追加' for k in added] + [f'{k}を削除' for k in removed]
    keys = sorted(set(before['flag_adjust']) | set(after['flag_adjust']))
    for k in keys:
        if k in after['plus'] + after['minus'] and before['flag_adjust'].get(k, 0) != after['flag_adjust'].get(k, 0):
            w = engine.PLUS_WEIGHTS.get(k, engine.MINUS_WEIGHTS.get(k))
            parts.append(f'{k} {w + before["flag_adjust"].get(k, 0):+d}→{w + after["flag_adjust"].get(k, 0):+d}')
    return '、'.join(parts)


def apply(edits, rec, today=None):
    """記録 rec に edits を反映し、(反映した馬, 報告の行) を返す。rec はその場で書き換える。"""
    stamp = (today or datetime.now()).strftime('%-m/%-d')
    applied, report = [], []
    by_num = {h['num']: h for h in rec['horses']}
    for e in sorted(edits, key=lambda e: e['num']):
        if e['race_id'] != rec['race_id']:
            continue
        h = by_num.get(int(e['num']))
        if h is None or h['name'] != e.get('name'):
            report.append(f"{e['num']}番 {e.get('name')}: 記録の馬と一致しないため反映しない")
            continue
        after = state(e)
        cur = state(h)
        if after == cur:
            continue
        if state(e.get('base') or {}) != cur:
            report.append(f"{h['num']}番 {h['name']}: 編集後に記録の加減点が変わっているため反映しない（ページを開き直して再編集）")
            continue
        try:
            engine.flag_points(dict(h, **after))
        except ValueError as err:
            report.append(f"{h['num']}番 {h['name']}: {err}")
            continue
        change = describe(cur, after)
        h['plus'], h['minus'] = after['plus'], after['minus']
        if after['flag_adjust']:
            h['flag_adjust'] = after['flag_adjust']
        else:
            h.pop('flag_adjust', None)
        note = (e.get('note') or '').strip()
        h['notes'] += f"（{stamp} ブラウザで加減点を調整）{change}。" + (f"理由: {note}。" if note else '')
        applied.append(h['num'])
        report.append(f"{h['num']}番 {h['name']}: {change}")
    return applied, report


def main(edits_path, *record_paths):
    edits = load_edits(json.loads(Path(edits_path).read_text(encoding='utf-8')))
    for path in record_paths:
        rec = json.loads(Path(path).read_text(encoding='utf-8'))
        if rec.get('decision'):
            print(f"{rec['race']}: 判断済みのため反映しない")
            continue
        applied, report = apply(edits, rec)
        if applied:
            Path(path).write_text(json.dumps(rec, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"{rec['race']}: {len(applied)}頭に反映" + ''.join('\n  ' + line for line in report))


if __name__ == '__main__':
    main(*sys.argv[1:])
