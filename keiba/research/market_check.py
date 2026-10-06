"""過去の重賞の確定オッズと着順・払戻だけで、エンジンが前提にしている市場確率の扱いを確かめる。

使い方: python3 -m keiba.research.market_check keiba/data/graded_2021_2026.json keiba/reports/market_check.md

1. 単勝オッズ帯ごとの実際の勝率と、全頭に単勝100円ずつ買った場合の回収率（人気薄の過大評価があるか）
2. 単勝オッズから Harville 式で出した3着以内確率と、実際の3着以内率（複勝・ワイドの確率の当たり具合）
3. 実際に的中した組について、Harville 式の確率と、払戻から逆算した市場の確率（払戻率÷配当倍率）の
   どちらがより高い確率を付けていたか（平均対数尤度）。組み合わせ馬券の確率を単勝から作るか、
   その券種のオッズから作るか、の判断材料

モデルの点数（ability など）を使っていないので、混合率・温度の妥当性はここでは分からない。
"""
import json
import math
import sys
from itertools import permutations
from pathlib import Path

from keiba import engine

PAYOUT = {'単勝': 0.80, '複勝': 0.80, '馬連': 0.775, 'ワイド': 0.775, '馬単': 0.75, '三連複': 0.75, '三連単': 0.725}
BANDS = [(1, 3), (3, 5), (5, 10), (10, 20), (20, 50), (50, 100), (100, 10000)]


NOT_SOLD = ('取', '除')  # 取消・除外は返還（発売対象外）。競走中止・失格は発売対象なので分母に残し、着外として扱う


def sold(r):
    return [h for h in r['horses'] if h['odds'] and h['rank'] not in NOT_SOLD]


def usable(r):
    return len(sold(r)) >= 5 and r['payouts'].get('単勝') and r['payouts'].get('三連単')


def market_win(r):
    return engine._normalize({h['num']: 1 / h['odds'] for h in sold(r)})


def win_bands(races):
    rows = []
    for lo, hi in BANDS:
        n = w = 0
        pay = 0.0
        imp = 0.0
        for r in races:
            p = market_win(r)
            for h in sold(r):
                if lo <= h['odds'] < hi:
                    n += 1
                    imp += p[h['num']]
                    if h['rank'] == '1':
                        w += 1
                        pay += h['odds'] * 100
        if n:
            rows.append((f'{lo}〜{hi if hi < 10000 else ""}倍', n, w, w / n, imp / n, pay / (n * 100)))
    return rows


def place_calibration(races):
    bins = [(0, .05), (.05, .1), (.1, .2), (.2, .3), (.3, .45), (.45, .6), (.6, 1.01)]
    acc = {b: [0, 0.0, 0] for b in bins}
    for r in races:
        p = market_win(r)
        tri = engine.harville(p)['trifecta']
        place = engine.ticket_probabilities(tri, 3)['place']
        top3 = {h['num'] for h in r['horses'] if h['rank'].isdigit() and int(h['rank']) <= 3}
        for n, q in place.items():
            for b in bins:
                if b[0] <= q < b[1]:
                    a = acc[b]
                    a[0] += 1
                    a[1] += q
                    a[2] += n in top3
    return [(f'{b[0] * 100:.0f}〜{min(b[1], 1) * 100:.0f}%', a[0], a[1] / a[0], a[2] / a[0]) for b, a in acc.items() if a[0]]


def _key(kind, s):
    nums = [int(x) for x in s.split('-')]
    if kind in ('馬連', '三連複', 'ワイド'):
        return tuple(sorted(nums))
    return tuple(nums)


def exotic_likelihood(races):
    """的中した組について、Harville と市場（配当逆算）の平均対数確率。値が大きい方が実際の結果に高い確率を付けていた。"""
    out = {}
    for kind in ('馬連', '馬単', '三連複', '三連単'):
        lh = lm = 0.0
        n = 0
        by_band = {}
        for r in races:
            pays = r['payouts'].get(kind)
            if not pays or len(pays) != 1:  # 同着・不成立は除く
                continue
            p = market_win(r)
            probs = engine.ticket_probabilities(engine.harville(p)['trifecta'], 3)
            table = {'馬連': probs['umaren'], '馬単': probs['umatan'], '三連複': probs['trio'], '三連単': probs['trifecta']}[kind]
            (combo, pay), = pays.items()
            key = _key(kind, combo)
            ph = table.get(key)
            if not ph:
                continue
            pm = min(1.0, PAYOUT[kind] / (pay / 100))
            lh += math.log(ph)
            lm += math.log(pm)
            n += 1
            band = '1%以上' if ph >= .01 else '0.1〜1%' if ph >= .001 else '0.1%未満'
            b = by_band.setdefault(band, [0, 0.0])
            b[0] += 1
            b[1] += math.log(pm / ph)
        if n:
            out[kind] = {'n': n, 'harville': lh / n, 'market': lm / n,
                         'bands': {k: (v[0], math.exp(v[1] / v[0])) for k, v in by_band.items()}}
    return out


def report(races):
    lines = ['# 過去5年の重賞で見る市場確率の扱い', '',
             f'対象: JRA 平地重賞 {len(races)} レース（{min(r["date"] for r in races)}〜{max(r["date"] for r in races)}）。'
             '確定単勝オッズ・着順・払戻のみ使用。モデルの点数は使っていないので、混合率・温度の妥当性はここでは判断できない。', '',
             '## 1. 単勝オッズ帯ごとの勝率と回収率', '',
             '| オッズ帯 | 頭数 | 勝利 | 実際の勝率 | 市場の勝率（平均） | 単勝回収率 |', '|---|---:|---:|---:|---:|---:|']
    for label, n, w, wr, imp, roi in win_bands(races):
        lines.append(f'| {label} | {n} | {w} | {wr * 100:.1f}% | {imp * 100:.1f}% | {roi * 100:.0f}% |')
    lines += ['', '実際の勝率が市場の勝率を下回る帯は、市場より実力が低い（買われすぎ）。', '',
              '## 2. Harville 式（単勝オッズから）の3着以内確率と実際', '',
              '| 推定3着以内確率 | 頭数 | 推定の平均 | 実際の3着以内率 |', '|---|---:|---:|---:|']
    for label, n, est, act in place_calibration(races):
        lines.append(f'| {label} | {n} | {est * 100:.1f}% | {act * 100:.1f}% |')
    lines += ['', '## 3. 的中した組への確率：Harville 式（単勝から） vs 市場（その券種の配当から逆算）', '',
              '| 券種 | レース数 | Harville 平均対数確率 | 市場 平均対数確率 | 良かった方 |', '|---|---:|---:|---:|---|']
    ex = exotic_likelihood(races)
    for kind, v in ex.items():
        better = '市場' if v['market'] > v['harville'] else 'Harville'
        lines.append(f"| {kind} | {v['n']} | {v['harville']:.3f} | {v['market']:.3f} | {better} |")
    lines += ['', '的中組の Harville 確率の帯ごとの「市場確率 ÷ Harville 確率」（幾何平均。1より小さいと、市場は Harville より低い確率＝高い配当を付けていた）', '',
              '| 券種 | 帯 | レース数 | 市場÷Harville |', '|---|---|---:|---:|']
    for kind, v in ex.items():
        for band in ('1%以上', '0.1〜1%', '0.1%未満'):
            if band in v['bands']:
                n, ratio = v['bands'][band]
                lines.append(f'| {kind} | {band} | {n} | {ratio:.2f} |')
    return '\n'.join(lines)


def main(data, out=None):
    races = [r for r in json.loads(Path(data).read_text(encoding='utf-8')) if usable(r)]
    text = report(races)
    if out:
        Path(out).write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main(*sys.argv[1:])
