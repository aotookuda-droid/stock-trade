"""単勝の市場確率の補正（オッズ帯・べき乗）と、Harville 式の2・3着補正（Benter 型）を推定し、年で分けて検証する。

使い方: python3 -m keiba.research.calibration keiba/data/graded_2021_2026.json keiba/reports/calibration_results.md
（推定値は同名の .json にも保存。考察と判断材料は keiba/reports/calibration.md）

- 市場の勝率 q: 確定単勝オッズの逆数を、発売対象馬（取消・除外以外。競走中止・失格を含む）で正規化したもの
- 単勝補正: p ∝ q^β × κ^[単勝 LONGSHOT_ODDS 倍以上]。β<1 なら人気薄を持ち上げ、β>1 なら人気薄を割り引く（対数オッズへの単調な補正）。
  κ は100倍以上だけの追加の割引（engine.LONGSHOT_ODDS の除外を確率の補正で置き換えられるかを見るため）
- 2・3着補正（Benter 型）: P(2着=b | 1着=a) = p_b^λ2 / Σ_{j≠a} p_j^λ2、3着も λ3 で同様。λ=1 で Harville 式
- 推定は TRAIN_YEARS、評価は残りの年。指標は勝ち馬・2着・3着それぞれの条件付き対数損失（同着の着順は評価しない）と、
  馬連・ワイド・三連複の的中組に付けた確率（単勝由来 vs その券種の払戻から逆算した市場確率）

過去データは確定オッズで、運用で使う発走10分前のオッズではない。
"""
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path

from keiba import engine

TRAIN_YEARS = ('2021', '2022', '2023', '2024')
PAYOUT = {'馬連': 0.775, 'ワイド': 0.775, '三連複': 0.75}
HITS = {'馬連': 1, 'ワイド': 3, '三連複': 1}
BANDS = [(1, 3), (3, 5), (5, 10), (10, 20), (20, 50), (50, 100), (100, 200), (200, 10000)]
NOT_SOLD = ('取', '除')  # 取消・除外は返還（発売対象外）。中止・失格は発売対象で、着外として扱う
KAPPAS = [round(0.05 * i, 2) for i in range(1, 29)]  # 0.05〜1.4


def sold(r):
    """発売対象馬（確定オッズがあり、取消・除外でない馬）。"""
    return [h for h in r['horses'] if h['odds'] and h['rank'] not in NOT_SOLD]


def finish(r, pos):
    """pos 着の馬番。同着（または該当なし）なら None。"""
    xs = [h['num'] for h in sold(r) if h['rank'].isdigit() and int(h['rank']) == pos]
    return xs[0] if len(xs) == 1 else None


def usable(r):
    return len(sold(r)) >= 5 and any(h['rank'] == '1' for h in sold(r))


def market_win(r):
    return engine._normalize({h['num']: 1 / h['odds'] for h in sold(r)})


def win_probs(r, beta=1.0, kappa=1.0):
    q = market_win(r)
    odds = {h['num']: h['odds'] for h in sold(r)}
    return engine._normalize({k: v ** beta * (kappa if odds[k] >= engine.LONGSHOT_ODDS else 1.0) for k, v in q.items()})


def cond_prob(p, pick, removed, lam):
    """removed を除いた残りから pick が選ばれる確率（勝率の lam 乗に比例）。"""
    den = sum(v ** lam for k, v in p.items() if k not in removed)
    return p[pick] ** lam / den


def per_race(r, beta=1.0, kappa=1.0, lam2=1.0, lam3=1.0):
    """(勝ち馬, 2着, 3着) の条件付き対数損失。同着などで定義できない着順は None。"""
    p = win_probs(r, beta, kappa)
    a, b, c = finish(r, 1), finish(r, 2), finish(r, 3)
    if a is None:
        return None, None, None
    l1 = -math.log(p[a])
    if b is None:
        return l1, None, None
    l2 = -math.log(cond_prob(p, b, {a}, lam2))
    l3 = -math.log(cond_prob(p, c, {a, b}, lam3)) if c is not None else None
    return l1, l2, l3


def mean_loss(races, i, **kw):
    xs = [x[i] for x in (per_race(r, **kw) for r in races) if x[i] is not None]
    return sum(xs) / len(xs)


def golden(f, lo, hi, tol=1e-4):
    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    while b - a > tol:
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def fit(races):
    """学習レースで β（κ=1）、β・κ、λ2・λ3（補正後の勝率に対して）を推定する。"""
    beta = golden(lambda x: mean_loss(races, 0, beta=x), 0.5, 1.5)
    best = None
    for k in KAPPAS:
        bk = golden(lambda x: mean_loss(races, 0, beta=x, kappa=k), 0.5, 1.5, tol=1e-3)
        loss = mean_loss(races, 0, beta=bk, kappa=k)
        if best is None or loss < best[0]:
            best = (loss, bk, k)
    kappa1 = golden(lambda x: mean_loss(races, 0, kappa=x), 0.05, 1.5)

    def lams(**kw):
        return (golden(lambda x: mean_loss(races, 1, lam2=x, **kw), 0.3, 1.5),
                golden(lambda x: mean_loss(races, 2, lam3=x, **kw), 0.3, 1.5))
    lam2, lam3 = lams(beta=beta)
    lam2_raw, lam3_raw = lams()
    lam2_k, lam3_k = lams(kappa=kappa1)
    return {'beta': beta, 'beta_k': best[1], 'kappa': best[2], 'kappa1': kappa1, 'lam2': lam2, 'lam3': lam3,
            'lam2_raw': lam2_raw, 'lam3_raw': lam3_raw, 'lam2_k': lam2_k, 'lam3_k': lam3_k}


def variants(est):
    return {
        '補正なし（単勝 Harville）': {},
        '単勝補正（β）のみ': {'beta': est['beta']},
        '単勝補正（β・κ）のみ': {'beta': est['beta_k'], 'kappa': est['kappa']},
        '2・3着補正（λ）のみ': {'lam2': est['lam2_raw'], 'lam3': est['lam3_raw']},
        'β ＋ λ': {'beta': est['beta'], 'lam2': est['lam2'], 'lam3': est['lam3']},
        'κ（β=1）＋ λ': {'kappa': est['kappa1'], 'lam2': est['lam2_k'], 'lam3': est['lam3_k']},
    }


def paired_ci(races, i, kw, base=None, n_boot=2000, seed=0):
    """補正あり − 補正なし の対数損失の差の平均と、レース単位のブートストラップ 95% 区間。"""
    diffs = []
    for r in races:
        x, y = per_race(r, **kw)[i], per_race(r, **(base or {}))[i]
        if x is not None and y is not None:
            diffs.append(x - y)
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n_boot))
    return sum(diffs) / len(diffs), means[int(.025 * n_boot)], means[int(.975 * n_boot)], len(diffs)


def benter_orders(p, lam2=1.0, lam3=1.0):
    """上位3頭の着順分布（Benter 型）。lam2=lam3=1 なら engine.harville と同じ。"""
    out = {}
    s2 = {k: v ** lam2 for k, v in p.items()}
    s3 = {k: v ** lam3 for k, v in p.items()}
    t2, t3 = sum(s2.values()), sum(s3.values())
    for a, pa in p.items():
        d2 = t2 - s2[a]
        for b in p:
            if b == a:
                continue
            pab = pa * s2[b] / d2
            d3 = t3 - s3[a] - s3[b]
            for c in p:
                if c != a and c != b:
                    out[(a, b, c)] = pab * s3[c] / d3
    return out


def _key(s):
    return tuple(sorted(int(x) for x in s.split('-')))


def exotic_compare(races, kws):
    """券種ごと・モデルごとに、的中組への平均対数確率と、市場（払戻逆算）÷モデル の幾何平均。"""
    out = {}
    for name, kw in kws.items():
        for r in races:
            p = win_probs(r, kw.get('beta', 1.0), kw.get('kappa', 1.0))
            probs = engine.ticket_probabilities(benter_orders(p, kw.get('lam2', 1.0), kw.get('lam3', 1.0)), 3)
            for kind, table in (('馬連', probs['umaren']), ('ワイド', probs['wide']), ('三連複', probs['trio'])):
                pays = r['payouts'].get(kind) or {}
                if len(pays) != HITS[kind]:  # 同着・不成立は除く
                    continue
                for combo, pay in pays.items():
                    pm = min(1.0, PAYOUT[kind] / (pay / 100))
                    ph = table.get(_key(combo))
                    if not ph:
                        continue
                    long_ = any(h['odds'] >= engine.LONGSHOT_ODDS for h in sold(r) if h['num'] in _key(combo))
                    for grp in ('全体', '100倍以上を含む組' if long_ else '100倍未満のみの組'):
                        o = out.setdefault((kind, grp), {}).setdefault(name, [0, 0.0, 0.0])
                        o[0] += 1
                        o[1] += math.log(ph)
                        o[2] += math.log(pm)
    return out


def band_table(races, kw=None):
    """オッズ帯ごと: 頭数・勝利・実勝率・市場の勝率・補正後の勝率・単勝回収率。"""
    rows = []
    for lo, hi in BANDS:
        n = w = 0
        imp = cal = pay = 0.0
        for r in races:
            q = market_win(r)
            p = win_probs(r, **(kw or {}))
            for h in sold(r):
                if lo <= h['odds'] < hi:
                    n += 1
                    imp += q[h['num']]
                    cal += p[h['num']]
                    if h['rank'] == '1':
                        w += 1
                        pay += h['odds']
        if n:
            rows.append((f'{lo}〜{hi if hi < 10000 else ""}倍', n, w, w / n, imp / n, cal / n, pay / n))
    return rows


def longshot_counts(races, kw):
    """100倍以上の馬の (頭数, 予測勝利数, 実際の勝利数, 予測3着以内数, 実際の3着以内数)。3着以内は同着も含めて数える。"""
    n = w = t = 0
    ew = et = 0.0
    for r in races:
        long_ = {h['num'] for h in sold(r) if h['odds'] >= engine.LONGSHOT_ODDS}
        if not long_:
            continue
        p = win_probs(r, kw.get('beta', 1.0), kw.get('kappa', 1.0))
        place = engine.ticket_probabilities(benter_orders(p, kw.get('lam2', 1.0), kw.get('lam3', 1.0)), 3)['place']
        for h in sold(r):
            if h['num'] in long_:
                n += 1
                ew += p[h['num']]
                et += place[h['num']]
                w += h['rank'] == '1'
                t += h['rank'].isdigit() and int(h['rank']) <= 3
    return n, ew, w, et, t


def split(races):
    train = [r for r in races if r['date'][:4] in TRAIN_YEARS]
    test = [r for r in races if r['date'][:4] not in TRAIN_YEARS]
    return train, test


def counts(races):
    c = Counter(h['rank'] for r in races for h in r['horses'] if not h['rank'].isdigit())
    return c


def report(races):
    train, test = split(races)
    est = fit(train)
    est_test = fit(test)  # 参考（評価年で推定し直した値。安定性を見るだけで、評価には使わない）
    vs = variants(est)
    c = counts(races)
    L = ['# 単勝の市場確率の補正と Harville 式の2・3着補正（年分割の検証）', '',
         f'対象: JRA 平地重賞 {len(races)} レース（{min(r["date"] for r in races)}〜{max(r["date"] for r in races)}）。'
         f'推定（学習）: {TRAIN_YEARS[0]}〜{TRAIN_YEARS[-1]}年 {len(train)} レース、評価: {min(r["date"] for r in test)[:4]}年〜 {len(test)} レース。',
         '確定単勝オッズ・着順・払戻のみ使用（モデルの点数は使わない）。', '',
         f'分母: 発売対象馬（確定オッズあり）。競走中止 {c.get("中", 0)} 頭・失格 {c.get("失", 0)} 頭は分母に残して着外、'
         f'取消 {c.get("取", 0)} 頭・除外 {c.get("除", 0)} 頭は返還なので外す（market_check.py も同じ扱いに直した）。'
         '勝ち馬に同着があるレースは全指標、2着同着は2・3着、3着同着は3着の評価から外す。', '',
         '## 1. 推定値（学習年）', '',
         '| 補正 | 値 | 意味 | 参考: 評価年で推定し直した値 |', '|---|---:|---|---:|',
         f"| β（単勝、κ=1） | {est['beta']:.3f} | p ∝ q^β。1より大きいと人気薄を割り引く | {est_test['beta']:.3f} |",
         f"| β（κ と同時） | {est['beta_k']:.3f} | | {est_test['beta_k']:.3f} |",
         f"| κ（{engine.LONGSHOT_ODDS}倍以上の追加の倍率） | {est['kappa']:.2f} | 1より小さいと100倍以上をさらに割り引く（格子 0.05 刻み） | {est_test['kappa']:.2f} |",
         f"| λ2（2着、β 補正後の勝率に対して） | {est['lam2']:.3f} | 1より小さいと2着は勝率ほど人気に偏らない | {est_test['lam2']:.3f} |",
         f"| λ3（3着、同上） | {est['lam3']:.3f} | | {est_test['lam3']:.3f} |",
         f"| λ2（補正なしの勝率に対して） | {est['lam2_raw']:.3f} | | {est_test['lam2_raw']:.3f} |",
         f"| λ3（補正なしの勝率に対して） | {est['lam3_raw']:.3f} | | {est_test['lam3_raw']:.3f} |",
         f"| κ（β=1 に固定） | {est['kappa1']:.3f} | 100倍以上だけを割り引く1変数の補正 | {est_test['kappa1']:.3f} |",
         f"| λ2・λ3（κ 補正後の勝率に対して） | {est['lam2_k']:.3f}・{est['lam3_k']:.3f} | | {est_test['lam2_k']:.3f}・{est_test['lam3_k']:.3f} |", '',
         '## 2. オッズ帯ごとの勝率（市場 vs 実際 vs 補正後 β）', '']
    for title, rs in (('学習年', train), ('評価年', test)):
        L += [f'### {title}', '', '| オッズ帯 | 頭数 | 勝利 | 実際の勝率 | 市場の勝率 | 補正後（β） | 実際÷市場 | 単勝回収率 |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
        for label, n, w, wr, imp, cal, roi in band_table(rs, {'beta': est['beta']}):
            L.append(f'| {label} | {n} | {w} | {wr * 100:.2f}% | {imp * 100:.2f}% | {cal * 100:.2f}% | {wr / imp:.2f} | {roi * 100:.0f}% |')
        L.append('')
    L += ['## 3. 評価年の対数損失（小さいほど良い）', '',
          '各着順の条件付き対数損失（2着は1着を、3着は1・2着を既知として）。合計は三連単の着順の対数損失に等しい。', '',
          '| 方式 | 勝ち馬 | 2着 | 3着 | 合計 |', '|---|---:|---:|---:|---:|']
    for name, kw in vs.items():
        ls = [mean_loss(test, i, **kw) for i in range(3)]
        L.append(f'| {name} | {ls[0]:.4f} | {ls[1]:.4f} | {ls[2]:.4f} | {sum(ls):.4f} |')
    L += ['', '補正なしとの差（補正あり − 補正なし、負なら改善）と、レース単位のブートストラップ 95% 区間:', '',
          '| 方式 | 着順 | 差 | 95% 区間 | レース数 |', '|---|---|---:|---|---:|']
    for name, kw in list(vs.items())[1:]:
        for i, pos in enumerate(('勝ち馬', '2着', '3着')):
            if i == 0 and not ('beta' in kw or 'kappa' in kw):
                continue
            m, lo, hi, n = paired_ci(test, i, kw)
            if abs(m) < 1e-12 and lo == hi:
                continue
            L.append(f'| {name} | {pos} | {m:+.4f} | [{lo:+.4f}, {hi:+.4f}] | {n} |')
    L += ['', '学習年での同じ差（推定に使ったデータなので、改善するのは当然。評価年と大きくずれないかを見る）:', '',
          '| 方式 | 勝ち馬 | 2着 | 3着 |', '|---|---:|---:|---:|']
    for name, kw in list(vs.items())[1:]:
        d = [mean_loss(train, i, **kw) - mean_loss(train, i) for i in range(3)]
        L.append(f'| {name} | {d[0]:+.4f} | {d[1]:+.4f} | {d[2]:+.4f} |')
    L += ['', '## 4. 評価年: 的中組への確率（単勝由来 vs その券種の払戻から逆算した市場確率）', '',
          '平均対数確率（大きいほど実際の結果に高い確率を付けていた）。市場確率は 払戻率÷配当倍率（ワイド・馬連 77.5%、三連複 75%）で、'
          'ワイドは1レース3組。「市場÷単勝由来」は幾何平均で、1より大きいと券種の市場の方が高い確率（低い配当）を付けていた。', '',
          '組の分類（100倍以上を含むか）は結果で分けた部分集合なので、件数が少なく偏りがある。参考程度。', '',
          '| 券種 | 組 | 件数 | 補正なし | λ のみ | κ ＋ λ | 券種の市場 | 市場÷補正なし | 市場÷λ のみ | 市場÷(κ＋λ) |',
          '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    ex = exotic_compare(test, {'raw': {}, 'lam': vs['2・3着補正（λ）のみ'], 'cal': vs['κ（β=1）＋ λ']})
    for kind in ('馬連', 'ワイド', '三連複'):
        for grp in ('全体', '100倍未満のみの組', '100倍以上を含む組'):
            v = ex.get((kind, grp))
            if not v:
                continue
            n, lr, lm = v['raw']
            _, ll, _ = v['lam']
            _, lc, _ = v['cal']
            L.append(f'| {kind} | {grp} | {n} | {lr / n:.3f} | {ll / n:.3f} | {lc / n:.3f} | {lm / n:.3f} | '
                     f'{math.exp((lm - lr) / n):.2f} | {math.exp((lm - ll) / n):.2f} | {math.exp((lm - lc) / n):.2f} |')
    q100 = {'補正なし': {}, '単勝補正（β）のみ': vs['単勝補正（β）のみ'], '単勝補正（β・κ）のみ': vs['単勝補正（β・κ）のみ'],
            '2・3着補正（λ）のみ': vs['2・3着補正（λ）のみ'], 'κ（β=1）＋ λ': vs['κ（β=1）＋ λ']}
    L += ['', '## 5. 100倍除外との関係（判断材料）', '',
          f'単勝{engine.LONGSHOT_ODDS}倍以上だった馬について、方式ごとの予測（勝率・3着以内確率の合計）と実際の数。'
          '3着以内はワイド・三連複・複勝の的中に効く。', '']
    for title, rs in (('学習年', train), ('評価年', test)):
        L += [f'### {title}', '', '| 方式 | 頭数 | 予測勝利数 | 実際の勝利数 | 予測3着以内数 | 実際の3着以内数 |', '|---|---:|---:|---:|---:|---:|']
        for name, kw in q100.items():
            L.append('| {} | {} | {:.1f} | {} | {:.1f} | {} |'.format(name, *longshot_counts(rs, kw)))
        L.append('')
    return '\n'.join(L), est


def main(data, out=None):
    races = [r for r in json.loads(Path(data).read_text(encoding='utf-8')) if usable(r)]
    text, est = report(races)
    if out:
        Path(out).write_text(text + '\n', encoding='utf-8')
        Path(out).with_suffix('.json').write_text(json.dumps(est, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main(*sys.argv[1:])
