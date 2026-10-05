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
TRENDS = Path(__file__).with_name('reports')
TREND_MIN_N = 10  # ▲▼に数える区分の最低出走数
LINKS = [('出馬表', 'shutuba'), ('調教', 'oikiri'), ('厩舎コメント', 'comment'), ('オッズ', 'odds/index')]
TRAINING_TAG = '（10/3 netkeiba調教[デイリー]・厩舎コメント確認）'
RULE_TAG = '。（10/3 ルール照合で追加）'
# API の type → (engine のキー, 組の区切り, 下限だけでなく上限も持つか)
TYPES = {1: ('win', None, False), 2: ('place', None, True), 3: ('wakuren', '-', False), 4: ('umaren', '-', False),
         5: ('wide', '-', True), 6: ('umatan', '>', False), 7: ('sanrenpuku', '-', False), 8: ('sanrentan', '>', False)}


def _get(race_id, t):
    req = urllib.request.Request(API.format(race_id, t), headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_odds(race_id):
    """全券種のオッズを engine の形式で取得する。

    戻り値 {'odds', 'asof'（JRA発表時刻）, 'fetched_at'（券種ごとの取得時刻）, 'scratched'（取消・除外と明示された馬番）}。
    単勝が1頭も取れなければ odds は None。
    """
    odds, asof, fetched_at, scratched = {}, None, {}, set()
    for t, (key, sep, has_range) in TYPES.items():
        data = _get(race_id, t).get('data') or {}
        got = datetime.now().astimezone().isoformat(timespec='seconds')
        table = (data.get('odds') or {}).get(str(t)) or {}
        if t == 2:  # 複勝は type=1 の応答に含まれない
            table = (data.get('odds') or {}).get('2') or table
        out = {}
        for combo, v in table.items():
            try:
                low = float(v[0])
            except (ValueError, IndexError):
                if t == 1 and v and any(w in str(v[0]) for w in ('取消', '除外')):
                    scratched.add(int(combo))
                continue  # 取消・未発売
            nums = [str(int(combo[i:i + 2])) for i in range(0, len(combo), 2)]
            k = nums[0] if sep is None else sep.join(nums)
            out[k] = [low, float(v[1])] if has_range and v[1] else low
        if out:
            odds[key] = out
            fetched_at[key] = got
            asof = asof or data.get('official_datetime')
    return {'odds': odds if odds.get('win') else None, 'asof': asof, 'fetched_at': fetched_at,
            'scratched': sorted(scratched)}


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


def load_trends(short, win):
    """同レース過去10年の傾向（keiba.research.trends が書いた JSON）。人気はその時点の単勝オッズ順。"""
    path = TRENDS / f'trends_{short}.json'
    if not path.exists():
        return None, {}
    t = json.loads(path.read_text(encoding='utf-8'))
    total = sum(v[0] for v in t['stats']['人気'].values())
    base = sum(v[2] for v in t['stats']['人気'].values()) / total if total else 0
    rank = {n: i + 1 for i, n in enumerate(sorted(win, key=win.get))}
    per = {}
    for num, cats in t['horses'].items():
        n = int(num)
        cats = dict(cats)
        if n in rank:
            p = rank[n]
            cats['人気'] = ('1番人気' if p == 1 else '2-3番人気' if p <= 3 else '4-6番人気' if p <= 6
                          else '7-9番人気' if p <= 9 else '10番人気以下')
        items, up, down = [], 0, 0
        for key in ('人気', '前走レース', '前走着順', '前走間隔', '枠', '年齢', '脚質'):
            v = cats.get(key)
            s = t['stats'].get(key, {}).get(v) if v else None
            if not s:
                continue
            rate = s[2] / s[0]
            items.append({'key': key, 'value': v, 'n': s[0], 'win': s[1], 'top3': s[2], 'rate': round(rate, 3)})
            if s[0] >= TREND_MIN_N and base:
                up += rate >= base * 1.5
                down += rate <= base * 0.5
        per[n] = {'items': items, 'up': up, 'down': down}
    meta = {'years': t['years'], 'n_races': t['n_races'], 'venues': t['venues'], 'base': round(base, 3)}
    return meta, per


def _start_iso(race):
    m = re.search(r'(\d{4})/(\d{2})/(\d{2}).*?(\d{1,2}):(\d{2})発走', race)
    return f"{m[1]}-{m[2]}-{m[3]}T{int(m[4]):02d}:{m[5]}:00+09:00" if m else None


def _jockey(notes):
    m = re.findall(r'騎手([^。、（(]+)', notes)
    return m[-1] if m else ''


def build_race(path, key):
    rec = json.loads(Path(path).read_text(encoding='utf-8'))
    pred_key = sorted(k for k in rec['horses'][0] if k.startswith('pred_odds_'))[-1]
    got = fetch_odds(rec['race_id']) if rec.get('race_id') else {'odds': None, 'asof': None, 'scratched': []}
    odds, asof = got['odds'], got['asof']
    # 取消は記録の scratched か、netkeiba が取消・除外と明示した馬だけ。単勝が無いだけの馬は取得失敗として扱う
    scratched = set(rec.get('scratched', [])) | set(got['scratched'])
    missing = [h['num'] for h in rec['horses']
               if odds and h['num'] not in scratched and str(h['num']) not in odds['win']]
    live = odds is not None and not missing
    if not live:
        odds, asof = {'win': {str(h['num']): h[pred_key] for h in rec['horses'] if h['num'] not in scratched}}, None
    win = {int(k): v for k, v in odds['win'].items()}
    running = [h for h in rec['horses'] if h['num'] not in scratched]
    codex = load_codex(rec['race_id']) if rec.get('race_id') else None
    others = {'Codex': engine._normalize({n: p for n, p in codex['win'].items() if n not in scratched})} if codex else None
    res = engine.analyze(dict(rec, odds=odds, scratched=sorted(scratched)), others=others)
    mkt = engine._normalize({h['num']: 1 / win[h['num']] for h in running})
    name_m = re.search(r'\d+R (.+?)\(', rec['race'])
    trend_meta, trends = load_trends(name_m.group(1) if name_m else '', win)
    horses = []
    for h in rec['horses']:
        n = h['num']
        is_scratched = n in scratched
        notes, _, train = h['notes'].partition(TRAINING_TAG)
        train, _, rule = train.partition(RULE_TAG)
        grade = re.search(r'評価([A-D])|\(([A-D])', train)
        horses.append({
            'num': n, 'gate': h['gate'], 'name': h['name'], 'jockey': _jockey(notes),
            'style': h['style'], 'zone': h['gate_zone'], 'ability': h['ability'],
            'minus': h['minus'], 'plus': h['plus'],
            'new': [k for k in h['plus'] + h['minus'] if '→' + k in train + rule], 'rule': rule,
            'notes': notes.rstrip('。'), 'train': train, 'grade': (grade.group(1) or grade.group(2)) if grade else '',
            'odds': win.get(n), 'pred': h.get(pred_key), 'scratched': is_scratched,
            'codex': round(codex['win'][n], 4) if codex and n in codex['win'] else None,
            'codex_note': codex['notes'].get(n, '') if codex else '',
            'trend': trends.get(n),
            'front': engine.is_front_candidate(h.get('early_positions', [])), 'early': h.get('early_positions', []),
            'pacefit': round(engine.pace_fit(h['style'], res['pace_probs']), 1),
            'score': res['scores'].get(n, 0), 'p': round(res['win_prob'].get(n, 0.0), 4), 'mkt': round(mkt.get(n, 0.0), 4),
        })
    bets = [{k: b[k] for k in ('type', 'horses', 'prob', 'odds', 'ev', 'kelly', 'stake')} for b in res['bets']]
    name = re.search(r'\d+R (.+?)\(', rec['race'])
    return {'key': key, 'short': name.group(1) if name else key, 'race': rec['race'], 'status': rec.get('status', ''),
            'pace': res['pace'], 'pace_probs': res['pace_probs'], 'warnings': engine.format_warnings(res), 'pace_note': rec.get('pace_note', ''), 'horses': horses,
            'bets': bets, 'stake': res['total_stake'], 'live': live, 'asof': asof, 'pred_key': pred_key,
            'missing': missing, 'skip': engine.SKIP_REASONS.get(res['skip_reason'], ''), 'market': engine.market_label(res),
            'codex': {k: v for k, v in codex.items() if k not in ('win', 'notes')} if codex else None,
            'trend_meta': trend_meta,
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
    stale = [r['short'] + ('（単勝オッズ欠損: ' + ','.join(map(str, r['missing'])) + '番）' if r['missing'] else '')
             for r in races if not r['live']]
    header = (f"{label}で計算（{datetime.now():%m/%d %H:%M} 更新）。トラックバイアスは未確定（フラット扱い）。"
              "調教・厩舎コメントは10/3反映済み（★付きが10/3追加フラグ）。"
              + (f"{'・'.join(stale)}はオッズ未取得のため予想オッズで参考計算。" if stale and (asofs or any(r['missing'] for r in races)) else '')
              + '買い目は参考試算。最終提案は発走10分前に実取得オッズで keiba.raceday が出す。')
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
