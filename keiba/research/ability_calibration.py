"""能力値（ability）の物差しを過去の重賞で校正する。

使い方: python3 -m keiba.research.ability_calibration keiba/data/graded_2021_2026.json keiba/reports/ability_calibration.md

分析者の ability は主観の値で、過去の記録がほとんど無いので直接は校正できない。代わりに、近走のクラスと勝ち馬との
タイム差だけから機械的に作る「客観能力」を、エンジンと同じソフトマックス（温度 engine.SOFTMAX_TEMP）で勝率にし、
過去の重賞の勝ち馬を最もよく当てる点数の付け方を推定する。

- 能力値の大きさと温度は別々には決まらない（全馬の点数を2倍にして温度を2倍にしても勝率は同じ）。温度を8に固定して、
  点数の付け方（クラスごとの基準点・0.1秒差あたりの点数・直近の重み・休み明け）を推定する。
  これが「温度8のエンジンに入れる ability の物差し」になる
- 各走の点数 = クラスの基準点 − 秒差あたりの点数 × 勝ち馬とのタイム差（−0.5〜3.0秒に丸める）。
  対象はJRAの全レースと、海外・地方の格付けのある重賞（G・Jpn）。地方の一般戦は除く
- 客観能力 = 直近5走の最高点 と 直近3走の平均点 の重み付き和 − 休み明け（半年以上）の減点
- 2024年までで推定し、2025年以降の対数損失で、一様・市場（単勝）・客観能力・市場と客観能力の組み合わせを比べる
"""
import json
import math
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np

from keiba import engine
from keiba.research import history

TEST_FROM = '2025-01-01'
CLASSES = ('G1', 'G2', 'G3', 'L', 'OP', '3勝', '2勝', '1勝', '未勝利')
JRA = ('札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉')


def race_class(name, venue):
    """レース名・開催地からクラス。分からない・地方の一般戦は None。"""
    g = re.search(r'\((?:G|Jpn)(I{1,3}|[123])\)', name)
    if g:
        v = g.group(1)
        return 'G' + str(len(v) if v.startswith('I') else int(v))
    jra = any(v in venue for v in JRA)
    if not jra:
        return None
    if '(重賞)' in name:
        return 'G3'
    if '(L)' in name:
        return 'L'
    for key, cls in (('3勝', '3勝'), ('1600万', '3勝'), ('2勝', '2勝'), ('1000万', '2勝'), ('1勝', '1勝'),
                     ('500万', '1勝'), ('未勝利', '未勝利'), ('新馬', '未勝利')):
        if key in name:
            return cls
    if '(OP)' in name or 'オープン' in name or 'OP' in name:
        return 'OP'
    return None


def past_runs(horse_id, before, n=5):
    """before より前の直近 n 走 [(クラス, タイム差, 日付)]（新しい順）。クラスかタイム差が分からない走は飛ばす。"""
    raw = history._get(f'https://db.netkeiba.com/horse/ajax_horse_results.html?input=UTF-8&output=json&id={horse_id}',
                       f'horse_{horse_id}.json', 'utf-8')
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', json.loads(raw)['data'], re.S):
        c = [history._text(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)]
        if len(c) < 20:
            continue
        d = c[0].replace('/', '-')
        if d >= before or c[14].startswith('障'):
            continue
        cls = race_class(c[4], c[1])
        try:
            margin = float(c[19])
        except ValueError:
            continue
        if cls:
            out.append((cls, margin, d))
        if len(out) == n:
            break
    return out


def features(runs, race_date):
    """各走の (クラスの添字, 丸めたタイム差) と、前走からの日数。"""
    if not runs:
        return None
    days = (date.fromisoformat(race_date) - date.fromisoformat(runs[0][2])).days
    return {'runs': [(CLASSES.index(c), min(max(m, -0.5), 3.0)) for c, m, _ in runs], 'days': days}


def build(races):
    out = []
    for r in races:
        hs = [h for h in r['horses'] if h['rank'].isdigit() and h['horse_id'] and h['odds']]
        if len(hs) < 5 or not any(h['rank'] == '1' for h in hs):
            continue
        rows = []
        for h in hs:
            f = features(past_runs(h['horse_id'], r['date']), r['date'])
            rows.append({'num': h['num'], 'name': h['name'], 'win': h['rank'] == '1', 'odds': h['odds'], 'f': f})
        if all(x['f'] for x in rows):
            out.append({'race_id': r['race_id'], 'name': r['name'], 'date': r['date'], 'horses': rows})
    return out


# パラメータ: クラスの基準点の1段ごとの下がり幅（G1→G2, G2→G3, … の8つ。正に保つため対数で持つ）、
# 秒差あたりの点数、最高点の重み、休み明けの減点。基準点はクラスが下がるほど下がる（単調）
NAMES = [f'{c}の基準点（G1との差）' for c in CLASSES[1:]] + ['0.1秒差あたりの点数', '最高点の重み（残りは直近3走の平均）',
                                                        '半年以上の休み明けの減点']
START = np.array([math.log(3)] * 8 + [1.0, 0.5, 3.0], dtype=float)


def class_base(params):
    return np.concatenate([[0.0], -np.cumsum(np.exp(params[:8]))])


def ability(params, f):
    base = class_base(params)
    per = [base[c] - params[8] * 10 * m for c, m in f['runs']]
    w = min(max(params[9], 0.0), 1.0)
    return w * max(per) + (1 - w) * sum(per[:3]) / len(per[:3]) - (params[10] if f['days'] >= 180 else 0.0)


def race_logloss(params, race, temp=engine.SOFTMAX_TEMP):
    s = np.array([ability(params, h['f']) for h in race['horses']]) / temp
    s -= s.max()
    p = np.exp(s) / np.exp(s).sum()
    return -math.log(max(p[[h['win'] for h in race['horses']].index(True)], 1e-12))


def mean_loss(params, races):
    return sum(race_logloss(params, r) for r in races) / len(races)


def nelder_mead(f, x0, step=1.0, iters=3000, tol=1e-7):
    n = len(x0)
    pts = [x0] + [x0 + step * np.eye(n)[i] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = np.argsort(vals)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        c = sum(pts[:-1]) / n
        xr = c + (c - pts[-1])
        fr = f(xr)
        if fr < vals[0]:
            xe = c + 2 * (c - pts[-1])
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = c + 0.5 * (pts[-1] - c)
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [pts[0] + 0.5 * (p - pts[0]) for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = int(np.argmin(vals))
    return pts[i], vals[i]


def market_logloss(races):
    loss = 0.0
    for r in races:
        p = engine._normalize({h['num']: 1 / h['odds'] for h in r['horses']})
        loss -= math.log(p[next(h['num'] for h in r['horses'] if h['win'])])
    return loss / len(races)


def combined(params, races, a, b):
    """勝率 ∝ 市場^a × exp(b × 客観能力 / 温度)。"""
    loss = 0.0
    for r in races:
        mk = engine._normalize({h['num']: 1 / h['odds'] for h in r['horses']})
        s = np.array([a * math.log(mk[h['num']]) + b * ability(params, h['f']) / engine.SOFTMAX_TEMP for h in r['horses']])
        s -= s.max()
        p = np.exp(s) / np.exp(s).sum()
        loss -= math.log(p[[h['win'] for h in r['horses']].index(True)])
    return loss / len(races)


def fit_combined(params, races):
    best = (None, math.inf)
    for a in np.arange(0.6, 1.41, 0.05):
        for b in np.arange(-0.2, 1.01, 0.05):
            v = combined(params, races, a, b)
            if v < best[1]:
                best = ((round(float(a), 2), round(float(b), 2)), v)
    return best


def main(src, dst):
    races = [r for r in json.loads(Path(src).read_text(encoding='utf-8')) if r['surface'] in ('芝', 'ダ')]
    data = build(races)
    train = [r for r in data if r['date'] < TEST_FROM]
    test = [r for r in data if r['date'] >= TEST_FROM]
    params, train_loss = nelder_mead(lambda x: mean_loss(x, train), START.copy())
    (a, b), comb_train = fit_combined(params, train)
    uniform = sum(math.log(len(r['horses'])) for r in test) / len(test)
    rows = [('一様（頭数分の1）', uniform), ('市場（単勝）', market_logloss(test)),
            ('客観能力（温度8）', mean_loss(params, test)),
            (f'市場^{a} × 客観能力^{b}（学習期間で推定）', combined(params, test, a, b))]
    # 温度を変えた場合（客観能力の点数はそのまま）。1.0 倍が温度8
    temps = [(t, sum(race_logloss(params, r, t) for r in test) / len(test)) for t in (4, 6, 8, 10, 12, 16)]
    out = ['# 能力値の物差しの校正（過去の重賞）', '',
           f'対象 {len(data)} レース（学習 {len(train)}: 〜2024年、検証 {len(test)}: 2025年〜）。'
           '近走のクラスと勝ち馬とのタイム差だけで作った客観能力を、温度8のソフトマックスで勝率にして推定した。', '',
           '## 温度8のエンジンと整合する点数の付け方（学習期間で推定）', '', '| 項目 | 値 |', '|---|---:|',
           '| G1の基準点 | 0（基準） |']
    shown = list(class_base(params)[1:]) + list(params[8:])
    out += [f'| {n} | {v:+.1f} |' if i < 8 else f'| {n} | {v:.2f} |' for i, (n, v) in enumerate(zip(NAMES, shown))]
    out += ['', f'学習期間の対数損失 {train_loss:.3f}。', '',
            '## 検証期間（2025年〜）の対数損失（勝ち馬に付けた勝率の −log、小さいほど良い）', '',
            '| 勝率の出どころ | 対数損失 |', '|---|---:|'] + [f'| {n} | {v:.3f} |' for n, v in rows]
    out += ['', '## 温度を変えた場合（客観能力の点数はそのまま、検証期間）', '', '| 温度 | 対数損失 |', '|---:|---:|']
    out += [f'| {t} | {v:.3f} |' for t, v in temps]
    Path(dst).write_text('\n'.join(out) + '\n', encoding='utf-8')
    Path(dst).with_suffix('.json').write_text(json.dumps(
        {'params': dict(zip(NAMES, map(float, shown))), 'raw': list(map(float, params)), 'combined': {'market': a, 'ability': b},
         'test': dict(rows), 'temps': dict(temps)}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('\n'.join(out))
    return params, data


if __name__ == '__main__':
    main(*sys.argv[1:3])
