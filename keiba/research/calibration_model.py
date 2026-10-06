"""2・3着の補正 λ（Benter 型）を、モデル側の勝率と、エンジンと同じ形の混合分布に当てはめて検証する。

使い方: python3 -m keiba.research.calibration_model keiba/data/graded_2021_2026.json keiba/reports/calibration_model.md

- モデル側の勝率: ability_calibration の客観能力（学習期間で推定した点数の付け方）を温度 engine.SOFTMAX_TEMP の
  ソフトマックスにしたもの（本番の ability は分析者の主観で、ここでは機械的な代用品）
- 市場側の勝率: 確定単勝オッズ（calibration.py と同じ分母）。過去データに三連単の全オッズはないので、
  エンジンの代替経路（単勝の Harville）に当たる
- 混合分布: engine.order_distribution と同じく、上位3頭の着順ごとに モデル^w × 市場^(1−w) を正規化（w は MODEL_WEIGHT と参考値）
- λ は学習年（calibration.TEST_FROM より前）だけで、モデル側・市場側それぞれの単独の分布で推定し、混合分布には推定し直さずに当てはめる
- 指標（評価年）: 三連単の着順の対数損失、三連複の組の対数損失、勝ち馬の対数損失（いずれも同着のレースは除く）

確定オッズでの検証で、発走10分前のオッズではない。
"""
import json
import math
import random
import sys
from pathlib import Path

import numpy as np

from keiba import engine
from keiba.research import ability_calibration as A
from keiba.research import calibration as C

WEIGHTS = (engine.MODEL_WEIGHT, 0.5, 0.0)


def build(src):
    raw = np.array(json.loads((Path(__file__).parents[1] / 'reports' / 'ability_calibration.json')
                              .read_text(encoding='utf-8'))['raw'])
    out = []
    for r in json.loads(Path(src).read_text(encoding='utf-8')):
        if r['surface'] not in ('芝', 'ダ') or not C.usable(r):
            continue
        hs = C.sold(r)
        if any(not h['horse_id'] for h in hs):
            continue
        obj = {}
        for h in hs:
            f = A.features(A.past_runs(h['horse_id'], r['date']), r['date'])
            if not f:
                break
            obj[h['num']] = A.ability(raw, f)
        else:
            out.append({**r, 'obj': obj})
    return out


def model_win(r):
    top = max(r['obj'].values())
    return engine._normalize({k: math.exp((s - top) / engine.SOFTMAX_TEMP) for k, s in r['obj'].items()})


def top3(r):
    xs = [C.finish(r, k) for k in (1, 2, 3)]
    return tuple(xs) if None not in xs else None


def cond_loss(races, probs, i, lam):
    """probs(r) の勝率に λ を掛けた条件付き対数損失（i=1: 2着、i=2: 3着）の平均。"""
    tot = n = 0
    for r in races:
        t = top3(r)
        if not t:
            continue
        p = probs(r)
        tot -= math.log(C.cond_prob(p, t[i], set(t[:i]), lam))
        n += 1
    return tot / n


def fit_lams(races, probs):
    return tuple(C.golden(lambda x: cond_loss(races, probs, i, x), 0.3, 1.5) for i in (1, 2))


def pooled(r, w, lm, lq, kappa=1.0):
    m = C.benter_orders(model_win(r), *lm)
    q = C.benter_orders(C.win_probs(r, kappa=kappa), *lq)
    if w <= 0:
        return q
    return engine._normalize({o: math.exp(w * math.log(m[o]) + (1 - w) * math.log(q[o])) for o in m})


def scores(races, w, lm, lq, kappa=1.0):
    """(三連単の対数損失, 三連複の対数損失, 勝ち馬の対数損失, 各レースの三連単の損失)。"""
    tri = trio = win = 0.0
    per = []
    for r in races:
        t = top3(r)
        if not t:
            continue
        o = pooled(r, w, lm, lq, kappa)
        lt = -math.log(o[t])
        tri += lt
        per.append(lt)
        s = set(t)
        trio -= math.log(sum(v for k, v in o.items() if set(k) == s))
        win -= math.log(sum(v for k, v in o.items() if k[0] == t[0]))
    n = len(per)
    return tri / n, trio / n, win / n, per


def report(races):
    train, test = C.split(races)
    lm = fit_lams(train, model_win)
    lm_test = fit_lams(test, model_win)
    est = json.loads((Path(__file__).parents[1] / 'reports' / 'calibration_results.json').read_text(encoding='utf-8'))
    lq = (est['lam2_raw'], est['lam3_raw'])
    lqk = (est['lam2_k'], est['lam3_k'])
    one = (1.0, 1.0)
    L = ['# 2・3着補正 λ をモデル側とエンジン型の混合分布に当てはめた検証', '',
         f'対象: 客観能力が全馬で作れた重賞 {len(races)} レース（学習 {len(train)}、評価 {len(test)}。同着のレースは指標から除く）。'
         'モデル側は ability_calibration の客観能力（温度8）、市場側は確定単勝オッズの Harville（エンジンの代替経路に当たる。'
         '本番は三連単オッズがそろえばそちらを使う）。', '',
         '## λ の推定値（学習年）', '', '| 勝率の出どころ | λ2 | λ3 | 参考: 評価年で推定し直した値 |', '|---|---:|---:|---:|',
         f'| モデル（客観能力） | {lm[0]:.3f} | {lm[1]:.3f} | {lm_test[0]:.3f}・{lm_test[1]:.3f} |',
         f"| 市場（単勝、calibration_results.json） | {lq[0]:.3f} | {lq[1]:.3f} | — |", '',
         '## 評価年の対数損失（小さいほど良い）', '',
         '「λ なし」はエンジンの現在の形（両側とも Harville）。差は「λ なし」との差で、区間はレース単位のブートストラップ 95%。', '']
    variants = [('λ なし', one, one, 1.0), ('モデル側だけ λ', lm, one, 1.0), ('市場側だけ λ', one, lq, 1.0),
                ('市場側 λ ＋ κ', one, lqk, est['kappa1']), ('両側 λ', lm, lq, 1.0), ('両側 λ ＋ 市場側 κ', lm, lqk, est['kappa1'])]
    for w in WEIGHTS:
        L += [f'### モデルの重み w = {w}' + ('（現行 MODEL_WEIGHT）' if w == engine.MODEL_WEIGHT else '（参考）'), '',
              '| 方式 | 三連単 | 三連複 | 勝ち馬 | 三連単の差 | 95% 区間 |', '|---|---:|---:|---:|---:|---|']
        base = None
        for name, a, b, k in variants:
            if w <= 0 and a != one:  # 市場のみではモデル側の λ は効かない
                continue
            tri, trio, win, per = scores(test, w, a, b, k)
            if base is None:
                base = per
                L.append(f'| {name} | {tri:.4f} | {trio:.4f} | {win:.4f} | — | |')
                continue
            m, lo, hi = boot([x - y for x, y in zip(per, base)])
            L.append(f'| {name} | {tri:.4f} | {trio:.4f} | {win:.4f} | {m:+.4f} | [{lo:+.4f}, {hi:+.4f}] |')
        L.append('')
    return '\n'.join(L), {'lam_model': lm, 'lam_model_test': lm_test}


def boot(d, n_boot=2000, seed=0):
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n_boot))
    return sum(d) / len(d), means[int(.025 * n_boot)], means[int(.975 * n_boot)]


def main(src, out=None):
    text, est = report(build(src))
    if out:
        Path(out).write_text(text + '\n', encoding='utf-8')
        Path(out).with_suffix('.json').write_text(json.dumps(est, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main(*sys.argv[1:])
