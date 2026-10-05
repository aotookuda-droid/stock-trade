"""位置取りの点数（engine.PACE_FIT）を縮めるべきかを、レース前に作れるペース確率で検証する。

使い方: python3 -m keiba.research.pacefit_check keiba/data/graded_2021_2026.json keiba/reports/pacefit_check.md

- ペース確率: 学習期間（〜2024年）だけで推定した、ハナ候補の頭数ごとの実際のペースの割合（pace_calibration と同じ方法）
- 各馬の位置取り点 = Σ ペース確率 × PACE_FIT[ペース][脚質] × 倍率。脚質は直近5走の通過順から機械的に決める
  （本番は分析者が決める）。倍率は 0・0.25・0.5・0.75・1（探索の候補で、採用値ではない）
- 土台は2つ: 客観能力（ability_calibration の学習期間の推定）に足す場合と、単勝市場に掛ける場合（勝率 ∝ 市場 × exp(点/温度)）
- 学習期間で倍率を選び、検証期間（2025年〜）で 勝率の対数損失・Brier・上位3頭の着順の対数損失（Harville）を比べる
- 参考として、実際のペースを事後に知っていた場合（神の目）も並べる。これはレース前には使えない

市場は確定単勝オッズで、発走10分前のオッズではない。
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

from keiba import engine
from keiba.research import ability_calibration as A
from keiba.research import pace_calibration as P

MULTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def build(src):
    races = [r for r in json.loads(Path(src).read_text(encoding='utf-8')) if r['surface'] in ('芝', 'ダ')]
    rows = P.build(races)
    _, _, rows = P.label_paces(rows)
    raw = np.array(json.loads((Path(__file__).parents[1] / 'reports' / 'ability_calibration.json')
                              .read_text(encoding='utf-8'))['raw'])
    out = []
    for r in rows:
        hs = [h for h in r['horses'] if h['odds']]
        if len(hs) < 5 or not any(h['rank'] == '1' for h in hs):
            continue
        for h in hs:
            f = A.features(A.past_runs(h['horse_id'], r['date']), r['date'])
            h['obj'] = A.ability(raw, f) if f else None
        if any(h['obj'] is None for h in hs):
            continue
        out.append({**r, 'horses': hs})
    return out


def probs(scores):
    s = np.array(list(scores.values())) / engine.SOFTMAX_TEMP
    s -= s.max()
    e = np.exp(s)
    return dict(zip(scores, e / e.sum()))


def evaluate(races, prior, mult, base, oracle=False):
    """(勝率の対数損失, Brier, 上位3頭の着順の対数損失) の平均。指標を定義できないレース（None）は、その指標だけ平均から除く。"""
    per = per_race(races, prior, mult, base, oracle)
    out = []
    for i in range(3):
        xs = [x[i] for x in per if x[i] is not None]
        out.append(sum(xs) / len(xs))
    return tuple(out)


def unique_top(race):
    """{着順: 馬番}。1着が1頭だけなら1着を、1〜3着がそれぞれ1頭だけなら1〜3着を返す（同着の着順は入れない）。"""
    ranks = [h['rank'] for h in race['horses']]
    return {k: next(h['num'] for h in race['horses'] if h['rank'] == str(k))
            for k in (1, 2, 3) if ranks.count(str(k)) == 1}


def counts(races):
    """(勝率・Brier を評価できるレース数, 着順を評価できるレース数)。同着で一意に決まらないレースは除く。"""
    tops = [unique_top(r) for r in races]
    return sum(1 for t in tops if 1 in t), sum(1 for t in tops if len(t) == 3)


def per_race(races, prior, mult, base, oracle=False):
    """レースごとの (勝率の対数損失, Brier, 着順の対数損失)。1着が同着のレースは勝率・Brier を、
    1〜3着のどれかが同着のレースは着順を None にする（0 を入れて平均しない。倍率によらず同じレース集合になる）。"""
    out = []
    for r in races:
        pace = {r['pace']: 1.0} if oracle else prior[P.bucket(r['leaders'])]
        mk = engine._normalize({h['num']: 1 / h['odds'] for h in r['horses']})
        sc = {}
        for h in r['horses']:
            pf = sum(w * engine.PACE_FIT[k][h['style']] for k, w in pace.items()) if h['style'] else 0.0
            b = h['obj'] if base == 'ability' else engine.SOFTMAX_TEMP * math.log(mk[h['num']])
            sc[h['num']] = b + mult * pf
        p = probs(sc)
        top = unique_top(r)
        ll = br = order = None
        if 1 in top:
            ll = -math.log(p[top[1]])
            br = sum((v - (n == top[1])) ** 2 for n, v in p.items())
        if len(top) == 3:
            rest, lp = dict(p), 0.0
            for k in (1, 2, 3):
                lp += math.log(rest[top[k]] / sum(rest.values()))
                del rest[top[k]]
            order = -lp
        out.append((ll, br, order))
    return out


def paired(races, prior, base, m1, m0, i):
    d = [a[i] - b[i] for a, b in zip(per_race(races, prior, m1, base), per_race(races, prior, m0, base))
         if a[i] is not None]
    m = sum(d) / len(d)
    return m, (sum((v - m) ** 2 for v in d) / (len(d) - 1) / len(d)) ** 0.5


def main(src, dst):
    races = build(src)
    train = [r for r in races if r['date'] < P.TEST_FROM]
    test = [r for r in races if r['date'] >= P.TEST_FROM]
    prior, _ = P.fit_prior(train)
    out = ['# 位置取りの点数（PACE_FIT）の縮小の検証', '',
           f'対象 {len(races)} レース（学習 {len(train)}: 〜2024年、検証 {len(test)}: 2025年〜）。'
           'ペース確率は学習期間だけで推定したハナ候補の頭数ごとの割合。脚質は直近5走の通過順から機械的に決めた（本番は分析者が決める）。'
           '市場は確定単勝オッズ。倍率は現行の PACE_FIT に掛ける値で、探索の候補。'
           '同着で着順が一意に決まらないレースは、その指標だけから除いた（倍率の比較は同じレース集合）: '
           '勝率・Brier は学習{}・検証{}レース、着順は学習{}・検証{}レース。'.format(
               counts(train)[0], counts(test)[0], counts(train)[1], counts(test)[1]), '']
    for base, label in (('ability', '土台：客観能力（温度8）'), ('market', '土台：単勝市場')):
        tr = {m: evaluate(train, prior, m, base) for m in MULTS}
        best = min(MULTS, key=lambda m: tr[m][0])
        out += [f'## {label}', '', f'学習期間で勝率の対数損失が最小の倍率: **{best}**', '',
                '| 倍率 | 学習 勝率LL | 検証 勝率LL | 検証 Brier | 検証 着順LL | 検証 勝率LL（神の目） |',
                '|---:|---:|---:|---:|---:|---:|']
        for m in MULTS:
            te = evaluate(test, prior, m, base)
            orc = evaluate(test, prior, m, base, oracle=True)
            out.append(f'| {m} | {tr[m][0]:.4f} | {te[0]:.4f} | {te[1]:.4f} | {te[2]:.4f} | {orc[0]:.4f} |')
        out += ['', '検証期間のレースごとの差（倍率1 − 倍率0、平均 ± 標準誤差、負なら倍率1が良い）: 勝率LL {:+.4f} ± {:.4f}、'
                '着順LL {:+.4f} ± {:.4f}'.format(*paired(test, prior, base, 1.0, 0.0, 0), *paired(test, prior, base, 1.0, 0.0, 2)), '']
    text = '\n'.join(out) + '\n'
    Path(dst).write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main(*sys.argv[1:3])
