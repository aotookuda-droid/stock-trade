"""netkeiba の現在オッズで馬柱ページ（HTML）を作り直す。

使い方: python3 -m keiba.live_card 出力.html keiba/records/20261004_毎日王冠.json keiba/records/20261004_京都大賞典.json

レース記録（records/*.json）は書き換えない。オッズが取れないレースは前日予想オッズ（pred_odds_*）で計算する。
隣の codex-keiba/records に同じ race_id の分析があれば、Codex版のシナリオと勝率を参考表示する（読むだけ）。
"""
import json
import math
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
# notes に追記された調教・ルール照合の区切り（日付はレースごとに違う。例「（10/3 netkeiba調教[デイリー]・厩舎コメント確認）」）
TRAINING_TAG = re.compile(r'（(\d{1,2}/\d{1,2}) netkeiba調教\[デイリー\]・厩舎コメント確認）')
RULE_TAG = re.compile(r'。（(\d{1,2}/\d{1,2}) ルール照合で追加）')
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


INDEPENDENT_AUTHOR = 'Codex'


def _codex_meta(rec, file):
    """作成元の情報。author が Codex で、モデルIDが Claude 作成を示さないものだけを独立予測とする。
    作者の記録が無い分析（10/4 以前）は作成元不明として独立予測に数えない。"""
    model = rec.get('model', {})
    author, model_id = model.get('author'), model.get('id', '')
    independent = author == INDEPENDENT_AUTHOR and not model_id.lower().startswith('claude')
    prep = rec.get('probability_preparation') or {}
    return {'file': file, 'author': author or '不明', 'model_id': model_id or '不明',
            'model_status': model.get('status', ''), 'independent': independent,
            'kind': 'codex' if independent else 'codex_reference',
            'label': 'Codex（独立）' if independent else f"Codex形式の参考（作成: {author or '不明'}・独立予測ではない）",
            'prep_status': prep.get('status') or '記録なし',
            'frozen_at': (rec.get('prediction_meta') or {}).get('frozen_at')}


def _codex_problems(rec, scenarios, running, decision_at):
    """勝率を表示・採用できない理由（空なら採用できる）。"""
    problems = []
    prep = (rec.get('probability_preparation') or {}).get('status')
    if prep != 'ready':
        problems.append(f"勝率の完成状態が {prep or '記録なし'}（ready ではない）")
    if not scenarios:
        return problems + ['重みが正のシナリオがない']
    for sc in scenarios:
        w = sc.get('weight')
        if not isinstance(w, (int, float)) or not math.isfinite(w) or w <= 0:
            problems.append(f"シナリオ {sc.get('id')} の重みが不正: {w}")
        st = {int(k): v for k, v in sc.get('strengths', {}).items()}
        lack = sorted(n for n in running if n not in st)
        if lack:
            problems.append(f"シナリオ {sc.get('id')} が全頭を網羅していない（{lack}番なし）")
        bad = sorted(n for n, v in st.items() if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0)
        if bad:
            problems.append(f"シナリオ {sc.get('id')} の強度が正の有限値でない（{bad}番）")
    if decision_at:
        frozen = (rec.get('prediction_meta') or {}).get('frozen_at')
        if not frozen:
            problems.append('予測の確定時刻（prediction_meta.frozen_at）がない')
        elif datetime.fromisoformat(frozen) > decision_at:
            problems.append(f'判断時刻より後に確定（{frozen}）')
        known = [x.get('known_at') for x in scenarios] + [e.get('known_at') for h in rec.get('horses', [])
                                                          for e in h.get('evidence', [])]
        late = [k for k in known if k and datetime.fromisoformat(k) > decision_at]
        if late:
            problems.append(f'判断時刻より後の根拠を含む（{max(late)}）')
    return problems


def load_codex(race_id, scratched=(), decision_at=None, running=None):
    """Codex版の最新分析。分析が無ければ None。

    戻り値は作成元の情報（_codex_meta）に、win（{馬番: 勝率}。採用できなければ None）、reason（採用しない理由）、
    notes・scenarios（表示用）を加えたもの。勝率はシナリオごとに取消馬を除いて強度を正規化し、シナリオの重みで平均する
    （取消後の勝率を混合後に一律で再正規化するのとは一般に一致しない）。取消で隊列が変わる場合は Codex に判断前の再分析を依頼する。
    running（出走予定の馬番）を渡すと全頭の網羅を確かめる。decision_at を渡すと確定時刻・根拠の時刻を確かめる。
    """
    try:
        index = json.loads((CODEX_RECORDS / 'index.json').read_text(encoding='utf-8'))
        rec = json.loads((CODEX_RECORDS / index[race_id]).read_text(encoding='utf-8'))
    except (OSError, KeyError, ValueError):
        return None
    scratched = {int(n) for n in scratched}
    scenarios = [s for s in rec.get('scenarios', []) if isinstance(s.get('weight'), (int, float)) and s['weight'] > 0]
    nums = running if running is not None else sorted({int(k) for s in scenarios for k in s.get('strengths', {})})
    nums = [n for n in nums if n not in scratched]
    meta = _codex_meta(rec, f"codex-keiba/records/{index[race_id]}")
    problems = _codex_problems(rec, scenarios, nums, decision_at)
    win = None
    if not problems:
        total_w = sum(s['weight'] for s in scenarios)
        win = {n: 0.0 for n in nums}
        for s in scenarios:
            st = {int(k): v for k, v in s['strengths'].items() if int(k) in win}
            tot = sum(st.values())
            for n, v in st.items():
                win[n] += s['weight'] / total_w * v / tot
    notes = {h['num']: h['evidence'][-1]['note'] for h in rec.get('horses', []) if h.get('evidence')}
    known = max((s.get('known_at', '') for s in scenarios), default='')
    gone = sorted(scratched & {int(k) for s in scenarios for k in s.get('strengths', {})})
    return {**meta, 'status': rec.get('model', {}).get('status', ''), 'win': win,
            'reason': '・'.join(problems) or None,
            'scratch_note': (f"取消 {gone}番をシナリオごとに除いて再計算した近似。隊列が変わる場合は Codex に再分析を依頼する"
                             if gone and win else None),
            'known_at': known[5:16].replace('T', ' ').replace('-', '/'), 'notes': notes,
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


ABILITY_PARAMS = Path(__file__).resolve().parent / 'reports' / 'ability_calibration.json'
SHUTUBA = 'https://race.netkeiba.com/race/shutuba.html?race_id={}'


def horse_ids(race_id):
    """{馬番: netkeiba の馬ID}。出馬表（キャッシュがあればそれ）から読む。"""
    from keiba.research import history
    cached = history.CACHE / f'shutuba_past_{race_id}.html'
    page = cached.read_text(encoding='utf-8') if cached.exists() else \
        history._get(SHUTUBA.format(race_id), f'shutuba_{race_id}.html', 'euc-jp')
    ids = {}
    for tr in re.findall(r'<tr class="HorseList.*?</tr>', page, re.S):
        # 馬番の欄（出馬表は Umaban、馬柱は Waku。枠番の Waku1〜8 とは別）
        n = re.search(r'<td class="(?:Waku|Umaban\d*(?: [^"]*)?)">\s*(\d+)\s*</td>', tr)
        m = re.search(r'db\.netkeiba\.com/horse/(\d{10})', tr)
        if n and m:
            ids[int(n.group(1))] = m.group(1)
    return ids


def reference_abilities(rec, running):
    """近走成績ベースの能力参照値（keiba.research.ability_calibration の客観能力）のレース内順位と勝率、
    ability だけの順位と勝率。不利・対戦相手のその後・個体の適性は未補正で、ability とは点数の原点が違うので
    生の点差では比べない。取得できなければ None。"""
    try:
        import numpy as np
        from keiba.research import ability_calibration as A
        raw = np.array(json.loads(ABILITY_PARAMS.read_text(encoding='utf-8'))['raw'])
        ids = horse_ids(rec['race_id'])
        day = re.search(r'(\d{4})/(\d{2})/(\d{2})', rec['race'])
        day = f'{day[1]}-{day[2]}-{day[3]}'
        ref = {}
        for h in running:
            f = A.features(A.past_runs(ids[h['num']], day), day)
            if f is None:
                return None
            ref[h['num']] = float(A.ability(raw, f))
    except Exception as e:  # 参照値は補助表示。取れなくても馬柱は出す
        print(f'能力参照値を出せなかった: {e}', file=sys.stderr)
        return None
    ab = {h['num']: h['ability'] for h in running}
    rank = lambda d: {n: i + 1 for i, n in enumerate(sorted(d, key=lambda k: -d[k]))}
    rp, ap = engine.model_probabilities(ref), engine.model_probabilities(ab)
    rr, ar = rank(ref), rank(ab)
    return {n: {'ref_p': round(rp[n], 4), 'ref_rank': rr[n], 'ab_p': round(ap[n], 4), 'ab_rank': ar[n]} for n in ref}


def _start_iso(race):
    m = re.search(r'(\d{4})/(\d{2})/(\d{2}).*?(\d{1,2}):(\d{2})発走', race)
    return f"{m[1]}-{m[2]}-{m[3]}T{int(m[4]):02d}:{m[5]}:00+09:00" if m else None


def _jockey(notes):
    m = re.findall(r'騎手([^。、（(]+)', notes)
    return m[-1] if m else ''


def _split(text, tag, seen):
    """tag の前後に分け、tag の日付（無ければ seen のまま）も返す。"""
    m = tag.search(text)
    if not m:
        return text, seen, ''
    return text[:m.start()], m.group(1), text[m.end():]


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
    start = _start_iso(rec['race'])
    decision_at = datetime.fromisoformat(start) - engine.DECISION_BEFORE if start else None
    codex = load_codex(rec['race_id'], scratched, decision_at, [h['num'] for h in running]) if rec.get('race_id') else None
    others = {codex['kind']: codex['win']} if codex and codex['win'] else None
    res = engine.analyze(dict(rec, odds=odds, scratched=sorted(scratched)), others=others)
    mkt = engine._normalize({h['num']: 1 / win[h['num']] for h in running})
    refs = reference_abilities(rec, running) if rec.get('race_id') else None
    name_m = re.search(r'\d+R (.+?)\(', rec['race'])
    trend_meta, trends = load_trends(name_m.group(1) if name_m else '', win)
    horses = []
    train_date = rule_date = None
    for h in rec['horses']:
        n = h['num']
        is_scratched = n in scratched
        notes, train_date, train = _split(h['notes'], TRAINING_TAG, train_date)
        train, rule_date, rule = _split(train, RULE_TAG, rule_date)
        grade = re.search(r'評価([A-D])|\(([A-D])', train)
        horses.append({
            'num': n, 'gate': h['gate'], 'name': h['name'], 'jockey': _jockey(notes),
            'style': h['style'], 'zone': h['gate_zone'], 'ability': h['ability'],
            'minus': h['minus'], 'plus': h['plus'], 'adjust': h.get('flag_adjust') or {},
            'new': [k for k in h['plus'] + h['minus'] if '→' + k in train + rule], 'rule': rule,
            'notes': notes.rstrip('。'), 'train': train, 'grade': (grade.group(1) or grade.group(2)) if grade else '',
            'odds': win.get(n), 'pred': h.get(pred_key), 'scratched': is_scratched,
            'codex': round(codex['win'][n], 4) if codex and codex['win'] and n in codex['win'] else None,
            'codex_note': codex['notes'].get(n, '') if codex else '',
            'trend': trends.get(n),
            'ref': refs.get(n) if refs else None,
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
            'train_date': train_date, 'rule_date': rule_date,
            'start_at': start,
            'links': [{'label': label, 'url': f'https://race.netkeiba.com/{"" if page.startswith("odds") else "race/"}'
                       f'{page}.html?race_id={rec["race_id"]}'} for label, page in LINKS] if rec.get('race_id') else []}


def page_title(races, today=None):
    """ページの見出し（例「10/11 重賞 前日馬柱」）。日付はレース名の先頭から取り、レース当日以降に作ったときは「前日」を外す。"""
    dates = sorted({m.group(1) for r in races if (m := re.match(r'(\d{4}/\d{2}/\d{2})', r['race']))})
    label = '・'.join(re.sub(r'(^|/)0', r'\1', d[5:]) for d in dates)
    today = (today or datetime.now()).strftime('%Y/%m/%d')
    return f"{label} 重賞 {'前日馬柱' if dates and today < dates[0] else '馬柱'}".strip()


def main(out, *paths):
    races = [build_race(p, f'r{i}') for i, p in enumerate(paths)]
    asofs = sorted(r['asof'] for r in races if r['asof'])
    if asofs:
        label = f"netkeiba オッズ {asofs[-1][5:16].replace('-', '/')} 時点"
    else:
        label = f"前日予想オッズ（{races[0]['pred_key']}）"
    stale = [r['short'] + ('（単勝オッズ欠損: ' + ','.join(map(str, r['missing'])) + '番）' if r['missing'] else '')
             for r in races if not r['live']]
    trained = [f"{r['short']}は{r['train_date']}" for r in races if r['train_date']]
    header = (f"{label}で計算（{datetime.now():%m/%d %H:%M} 更新）。トラックバイアスは未確定（フラット扱い）。"
              + (f"調教・厩舎コメントは{'・'.join(trained)}反映済み（★付きがその日の追加フラグ）。" if trained
                 else '調教・厩舎コメントは未反映（枠順確定段階）。')
              + (f"{'・'.join(stale)}はオッズ未取得のため予想オッズで参考計算。" if stale and (asofs or any(r['missing'] for r in races)) else '')
              + '買い目は参考試算。最終提案は発走10分前に実取得オッズで keiba.raceday が出す。')
    starts = sorted(r['start_at'] for r in races if r['start_at'])
    data = {'races': races, 'plus': engine.PLUS_WEIGHTS, 'minus': engine.MINUS_WEIGHTS,
            'header': header, 'odds_label': label,
            'asof_iso': asofs[-1].replace(' ', 'T') + '+09:00' if asofs else None,
            'end_iso': starts[-1] if starts else None}
    html = (TEMPLATE.read_text(encoding='utf-8').replace('__TITLE__', page_title(races))
            .replace('/*DATA*/null', json.dumps(data, ensure_ascii=False)))
    Path(out).write_text(html, encoding='utf-8')
    print(header)
    for r in races:
        print(r['short'], [(b['type'], b['horses'], b['stake'], round(b['ev'], 2)) for b in r['bets']] or '見送り')


if __name__ == '__main__':
    main(*sys.argv[1:])
