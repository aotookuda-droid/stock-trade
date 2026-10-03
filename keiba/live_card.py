"""netkeiba の現在オッズで馬柱ページ（HTML）を作り直す。

使い方: python3 -m keiba.live_card 出力.html keiba/records/20261004_毎日王冠.json keiba/records/20261004_京都大賞典.json

レース記録（records/*.json）は書き換えない。オッズが取れないレースは前日予想オッズ（pred_odds_*）で計算する。
隣の codex-keiba/records に同じ race_id の分析があれば、Codex版のシナリオと勝率を参考表示する（読むだけ）。
"""
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from keiba import engine

API = 'https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={}&type={}&action=update'
TEMPLATE = Path(__file__).with_name('card_template.html')
CODEX_RECORDS = Path(__file__).resolve().parents[2] / 'codex-keiba' / 'records'
LINKS = [('出馬表', 'shutuba'), ('調教', 'oikiri'), ('厩舎コメント', 'comment'), ('オッズ', 'odds/index')]
TRAINING_TAG = '（10/3 netkeiba調教[デイリー]・厩舎コメント確認）'
# API の type → (engine のキー, 組の区切り, 下限だけでなく上限も持つか)
TYPES = {1: ('win', None, False), 2: ('place', None, True), 3: ('wakuren', '-', False), 4: ('umaren', '-', False),
         5: ('wide', '-', True), 6: ('umatan', '>', False), 7: ('sanrenpuku', '-', False), 8: ('sanrentan', '>', False)}


def _get(race_id, t):
    req = urllib.request.Request(API.format(race_id, t), headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_odds(race_id):
    """全券種のオッズを engine の形式で返す。(odds, 発表時刻) 。単勝が取れなければ (None, None)。"""
    odds, asof = {}, None
    for t, (key, sep, has_range) in TYPES.items():
        data = _get(race_id, t).get('data') or {}
        table = (data.get('odds') or {}).get(str(t)) or {}
        if t == 2:  # 複勝は type=1 の応答に含まれない
            table = (data.get('odds') or {}).get('2') or table
        out = {}
        for combo, v in table.items():
            try:
                low = float(v[0])
            except (ValueError, IndexError):
                continue  # 取消・未発売
            nums = [str(int(combo[i:i + 2])) for i in range(0, len(combo), 2)]
            k = nums[0] if sep is None else sep.join(nums)
            out[k] = [low, float(v[1])] if has_range and v[1] else low
        if out:
            odds[key] = out
            asof = asof or data.get('official_datetime')
    if not odds.get('win'):
        return None, None
    return odds, asof


def load_codex(race_id):
    """Codex版の最新分析から {scenarios, win, notes, ...} を返す。無ければ None。"""
    try:
        index = json.loads((CODEX_RECORDS / 'index.json').read_text(encoding='utf-8'))
        rec = json.loads((CODEX_RECORDS / index[race_id]).read_text(encoding='utf-8'))
    except (OSError, KeyError, ValueError):
        return None
    scenarios = [s for s in rec.get('scenarios', []) if s.get('weight', 0) > 0 and s.get('strengths')]
    if not scenarios:
        return None
    total_w = sum(s['weight'] for s in scenarios)
    win = {}
    for s in scenarios:
        tot = sum(s['strengths'].values())
        for k, v in s['strengths'].items():
            win[int(k)] = win.get(int(k), 0.0) + s['weight'] / total_w * v / tot
    notes = {h['num']: h['evidence'][-1]['note'] for h in rec.get('horses', []) if h.get('evidence')}
    known = max((s.get('known_at', '') for s in scenarios), default='')
    return {'file': f"codex-keiba/records/{index[race_id]}", 'status': rec.get('model', {}).get('status', ''),
            'known_at': known[5:16].replace('T', ' ').replace('-', '/'), 'win': win, 'notes': notes,
            'scenarios': [{k: s.get(k) for k in ('id', 'weight', 'pace', 'inner_outer', 'front_back', 'rationale')}
                          for s in scenarios]}


def _start_iso(race):
    m = re.search(r'(\d{4})/(\d{2})/(\d{2}).*?(\d{1,2}):(\d{2})発走', race)
    return f"{m[1]}-{m[2]}-{m[3]}T{int(m[4]):02d}:{m[5]}:00+09:00" if m else None


def _jockey(notes):
    m = re.findall(r'騎手([^。、（(]+)', notes)
    return m[-1] if m else ''


def build_race(path, key):
    rec = json.loads(Path(path).read_text(encoding='utf-8'))
    pred_key = sorted(k for k in rec['horses'][0] if k.startswith('pred_odds_'))[-1]
    odds, asof = fetch_odds(rec['race_id']) if rec.get('race_id') else (None, None)
    live = odds is not None
    if not live:
        odds = {'win': {str(h['num']): h[pred_key] for h in rec['horses']}}
    win = {int(k): v for k, v in odds['win'].items()}
    # 単勝オッズのない馬（取消・除外）は計算から外す
    running = [h for h in rec['horses'] if h['num'] in win]
    res = engine.analyze(dict(rec, horses=running, odds=odds))
    mkt = engine._normalize({h['num']: 1 / win[h['num']] for h in running})
    codex = load_codex(rec['race_id']) if rec.get('race_id') else None
    horses = []
    for h in rec['horses']:
        n = h['num']
        scratched = n not in win
        notes, _, train = h['notes'].partition(TRAINING_TAG)
        grade = re.search(r'評価([A-D])|\(([A-D])', train)
        horses.append({
            'num': n, 'gate': h['gate'], 'name': h['name'], 'jockey': _jockey(h['notes']),
            'style': h['style'], 'zone': h['gate_zone'], 'ability': h['ability'],
            'minus': h['minus'], 'plus': h['plus'],
            'new': [k for k in h['plus'] + h['minus'] if '→' + k in train],
            'notes': notes.rstrip('。'), 'train': train, 'grade': (grade.group(1) or grade.group(2)) if grade else '',
            'odds': win.get(n), 'pred': h.get(pred_key), 'scratched': scratched,
            'codex': round(codex['win'][n], 4) if codex and n in codex['win'] else None,
            'codex_note': codex['notes'].get(n, '') if codex else '',
            'front': engine.is_front_candidate(h.get('early_positions', [])), 'early': h.get('early_positions', []),
            'pacefit': engine.PACE_FIT[res['pace']][h['style']],
            'score': res['scores'].get(n, 0), 'p': res['win_prob'].get(n, 0.0), 'mkt': round(mkt.get(n, 0.0), 4),
        })
    bets = [{k: b[k] for k in ('type', 'horses', 'prob', 'odds', 'ev', 'kelly', 'stake')} for b in res['bets']]
    name = re.search(r'\d+R (.+?)\(', rec['race'])
    return {'key': key, 'short': name.group(1) if name else key, 'race': rec['race'], 'status': rec.get('status', ''),
            'pace': res['pace'], 'pace_note': rec.get('pace_note', ''), 'horses': horses,
            'bets': bets, 'stake': res['total_stake'], 'live': live, 'asof': asof, 'pred_key': pred_key,
            'codex': {k: v for k, v in codex.items() if k not in ('win', 'notes')} if codex else None,
            'start_at': _start_iso(rec['race']),
            'links': [{'label': label, 'url': f'https://race.netkeiba.com/{"" if page.startswith("odds") else "race/"}'
                       f'{page}.html?race_id={rec["race_id"]}'} for label, page in LINKS] if rec.get('race_id') else []}


def main(out, *paths):
    races = [build_race(p, f'r{i}') for i, p in enumerate(paths)]
    asofs = sorted(r['asof'] for r in races if r['asof'])
    if asofs:
        label = f"netkeiba オッズ {asofs[-1][5:16].replace('-', '/')} 時点"
    else:
        label = f"前日予想オッズ（{races[0]['pred_key']}）"
    stale = [r['short'] for r in races if not r['live']]
    header = (f"{label}で計算（{datetime.now():%m/%d %H:%M} 更新）。トラックバイアスは未確定（フラット扱い）。"
              "調教・厩舎コメントは10/3反映済み（★付きが10/3追加フラグ）。"
              + (f"{'・'.join(stale)}はオッズ未取得のため予想オッズ。" if asofs and stale else ''))
    starts = sorted(r['start_at'] for r in races if r['start_at'])
    data = {'races': races, 'plus': engine.PLUS_WEIGHTS, 'minus': engine.MINUS_WEIGHTS,
            'header': header, 'odds_label': label,
            'asof_iso': asofs[-1].replace(' ', 'T') + '+09:00' if asofs else None,
            'end_iso': starts[-1] if starts else None}
    html = TEMPLATE.read_text(encoding='utf-8').replace('/*DATA*/null', json.dumps(data, ensure_ascii=False))
    Path(out).write_text(html, encoding='utf-8')
    print(header)
    for r in races:
        print(r['short'], [(b['type'], b['horses'], b['stake'], round(b['ev'], 2)) for b in r['bets']] or '見送り')


if __name__ == '__main__':
    main(*sys.argv[1:])
