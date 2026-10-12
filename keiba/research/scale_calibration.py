"""モデル勝率の物差しを市場にそろえる方式（案3）と、乖離ゲートの根拠を過去の重賞で確かめる。

使い方: python3 -m keiba.research.scale_calibration keiba/data/graded_2021_2026.json keiba/reports/scale_calibration.md

本番の点数（主観の ability・加減点）は過去分がないので、ability_calibration の客観能力（学習期間で推定済みの点数）を
代用モデルにする。市場は確定単勝オッズ（発走10分前ではない）。比べる方式は次の3つで、混合は本番と同じ
モデル^0.25 × 市場^0.75 の正規化。
- 温度: softmax(点数 / T)。本番は T=8
- 広がり合わせ: レースごとに、モデルの対数勝率の標準偏差が市場の k 倍になるよう点数を伸縮する
- 順位写像: モデルの順位どおりに市場の勝率を並べ替える（大きさは市場そのもの、順位だけモデル）
方式とパラメータは学習期間（〜2024年）の混合後の対数損失で選び、2025年〜で評価する。
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

from keiba import engine
from keiba.research import ability_calibration as ac

TEST_FROM = ac.TEST_FROM
W = engine.MODEL_WEIGHT
TEMPS = (4, 5, 6, 7, 8, 9, 10, 12)
SPREADS = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2)
RATIO_BINS = ((3.0, math.inf, '3倍以上'), (2.0, 3.0, '2〜3倍'), (1.5, 2.0, '1.5〜2倍'), (1 / 1.5, 1.5, '1/1.5〜1.5倍'),
              (0.0, 1 / 1.5, '1/1.5倍未満'))


def softmax(x):
    e = np.exp(x - x.max())
    return e / e.sum()


def by_temp(t):
    return lambda a, m: softmax(a / t)


def by_spread(k):
    def f(a, m):
        sa = a.std()
        return softmax(a * (k * np.log(m).std() / sa)) if sa > 0 else m
    return f


def by_rank(a, m):
    out = np.empty_like(m)
    out[np.argsort(-a, kind='stable')] = np.sort(m)[::-1]
    return out


def blend(p, m, w=W):
    return softmax(w * np.log(p) + (1 - w) * np.log(m))


def prepare(data, params):
    out = []
    for r in data:
        o = np.array([h['odds'] for h in r['horses']], dtype=float)
        out.append({'test': r['date'] >= TEST_FROM, 'a': np.array([ac.ability(params, h['f']) for h in r['horses']]),
                    'odds': o, 'm': (1 / o) / (1 / o).sum(), 'win': np.array([h['win'] for h in r['horses']])})
    return out


def logloss(races, f, mixed):
    loss = 0.0
    for r in races:
        p = f(r['a'], r['m'])
        p = blend(p, r['m']) if mixed else p
        loss -= math.log(max(p[r['win'].argmax()], 1e-12))
    return loss / len(races)


def win_bets(races, f, pick):
    """pick(モデル勝率, 市場, 混合後, オッズ) で選んだ馬の単勝を100円ずつ買った場合（確定オッズ）。"""
    n = wins = expected = ret = 0.0
    for r in races:
        p = f(r['a'], r['m'])
        sel = pick(p, r['m'], blend(p, r['m']), r['odds'])
        n += sel.sum()
        wins += r['win'][sel].sum()
        expected += r['m'][sel].sum()
        ret += (r['odds'] * r['win'])[sel].sum()
    return {'n': int(n), 'wins': int(wins), 'market_wins': round(float(expected), 1),
            'roi': round(float(ret / n), 3) if n else None}


def main(src, dst):
    races = [r for r in json.loads(Path(src).read_text(encoding='utf-8')) if r['surface'] in ('芝', 'ダ')]
    params = np.array(json.loads(Path(dst).with_name('ability_calibration.json').read_text(encoding='utf-8'))['raw'])
    data = prepare(ac.build(races), params)
    train = [r for r in data if not r['test']]
    test = [r for r in data if r['test']]
    methods = [(f'温度 {t}', by_temp(t)) for t in TEMPS] + [(f'広がり合わせ k={k}', by_spread(k)) for k in SPREADS] + \
              [('順位写像', by_rank)]
    rows = [{'name': n, 'train_mixed': logloss(train, f, True), 'test_alone': logloss(test, f, False),
             'test_mixed': logloss(test, f, True)} for n, f in methods]
    best = min(rows, key=lambda x: x['train_mixed'])
    market = logloss(test, lambda a, m: m, False)
    current = by_temp(engine.SOFTMAX_TEMP)
    ratio = [{'label': label, **{part: win_bets(rs, current, lambda p, m, b, o, lo=lo, hi=hi: (p / m >= lo) & (p / m < hi))
                                 for part, rs in (('train', train), ('test', test))}} for lo, hi, label in RATIO_BINS]
    ev = {n: {part: win_bets(rs, f, lambda p, m, b, o: b * o >= engine.EV_THRESHOLD)
              for part, rs in (('train', train), ('test', test))} for n, f in methods if n in (f'温度 {engine.SOFTMAX_TEMP}', best['name'], '順位写像')}
    out = ['# モデル勝率の物差しと乖離ゲートの検証（過去の重賞）', '',
           f'対象 {len(data)} レース（学習 {len(train)}: 〜2024年、評価 {len(test)}: 2025年〜）。代用モデルは ability_calibration の'
           '客観能力（本番の主観 ability・加減点ではない）。市場は確定単勝オッズ。混合は本番と同じ モデル^0.25 × 市場^0.75。', '',
           '## 1. 物差しの合わせ方（対数損失、小さいほど良い）', '',
           f'評価年の市場だけ: **{market:.4f}**。方式は学習年の混合後で選ぶ（選ばれたのは「{best["name"]}」）。', '',
           '| 方式 | 学習年・混合後 | 評価年・モデル単独 | 評価年・混合後 |', '|---|---:|---:|---:|']
    out += [f'| {r["name"]} | {r["train_mixed"]:.4f} | {r["test_alone"]:.4f} | {r["test_mixed"]:.4f} |' for r in rows]
    out += ['', '## 2. モデル勝率 ÷ 市場勝率 の帯ごとの実際（温度8、単勝100円ずつ・確定オッズ）', '',
            '「市場の想定勝ち数」は市場勝率の合計。乖離が本物の情報なら、倍率が高い帯ほど実際の勝ち数が想定を上回るはず。', '',
            '| 倍率 | 学習年 頭数 | 勝ち | 市場の想定 | 回収率 | 評価年 頭数 | 勝ち | 市場の想定 | 回収率 |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in ratio:
        a, b = r['train'], r['test']
        out.append(f'| {r["label"]} | {a["n"]} | {a["wins"]} | {a["market_wins"]} | {a["roi"]} | {b["n"]} | {b["wins"]} | '
                   f'{b["market_wins"]} | {b["roi"]} |')
    out += ['', f'## 3. 混合後の期待値が{engine.EV_THRESHOLD}以上の単勝を買った場合（確定オッズ）', '',
            '| 方式 | 学習年 点数 | 勝ち | 市場の想定 | 回収率 | 評価年 点数 | 勝ち | 市場の想定 | 回収率 |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for n, v in ev.items():
        a, b = v['train'], v['test']
        out.append(f'| {n} | {a["n"]} | {a["wins"]} | {a["market_wins"]} | {a["roi"]} | {b["n"]} | {b["wins"]} | '
                   f'{b["market_wins"]} | {b["roi"]} |')
    Path(dst).write_text('\n'.join(out) + '\n', encoding='utf-8')
    Path(dst).with_suffix('.json').write_text(json.dumps(
        {'market_test': market, 'methods': rows, 'selected': best['name'], 'ratio_bins': ratio, 'ev_bets': ev},
        ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('\n'.join(out))


if __name__ == '__main__':
    main(*sys.argv[1:3])
