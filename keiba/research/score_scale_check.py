"""本番の点数（主観の ability・加減点）の上位の点差と、温度8の物差しを判断時点の記録で確かめる（エンジンは変えない）。

使い方: python3 -m keiba.research.score_scale_check keiba/reports/score_scale_check.md

1. 精算済みレース: レコードの result.scores（判断時の総合点）と result 時点の単勝オッズ（発走約10分前）、
   settlements の着順で、温度ごとの勝ち馬の対数損失を比べる
2. 未確定レース（富士S・秋華賞など）: 点数はエンジンと同じ計算（バイアスはフラット）、オッズは取得時点の前売り
3. 全レース: 点数の広がりと、市場の勝率に一番合う温度（市場から見た温度）、客観能力（ability_calibration、温度8で
   推定済みの物差し）の広がりとの比較
4. 過去の重賞（客観能力）: 1位と2位の点差ごとに、1位の実際の勝率と温度8の予測・市場を比べる
"""
import json
import math
import re
import sys
from pathlib import Path

import numpy as np

from keiba import engine, live_card
from keiba.research import ability_calibration as ac

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / 'records'
SETTLEMENTS = ROOT / 'settlements'
TEMPS = (2, 3, 4, 5, 6, 8, 10, 12)
GAP_BINS = ((0, 1), (1, 3), (3, 6), (6, 10), (10, math.inf))


def softmax(x, t):
    x = np.asarray(x, dtype=float) / t
    e = np.exp(x - x.max())
    return e / e.sum()


def implied_temp(scores, market):
    """市場の勝率との交差エントロピー Σ m·(−log p) が最小になる温度（0.5〜50 の範囲で探す）。"""
    grid = np.exp(np.linspace(math.log(0.5), math.log(50), 400))
    loss = [-(market * np.log(softmax(scores, t))).sum() for t in grid]
    return float(grid[int(np.argmin(loss))])


def proxy_abilities(rec, nums):
    """客観能力（ability_calibration の点数、温度8で推定済み）。取れなければ None。"""
    try:
        raw = np.array(json.loads(live_card.ABILITY_PARAMS.read_text(encoding='utf-8'))['raw'])
        ids = live_card.horse_ids(rec['race_id'])
        d = re.search(r'(\d{4})/(\d{2})/(\d{2})', rec['race'])
        day = f'{d[1]}-{d[2]}-{d[3]}'
        out = {}
        for n in nums:
            f = ac.features(ac.past_runs(ids[n], day), day)
            if f is None:
                return None
            out[n] = float(ac.ability(raw, f))
        return out
    except Exception as e:
        print(f'客観能力を出せなかった（{rec["race"][:30]}）: {e}', file=sys.stderr)
        return None


def load_settled():
    finish = {}
    for f in SETTLEMENTS.glob('*.json'):
        s = json.loads(f.read_text(encoding='utf-8'))
        finish[s['race_id']] = s['official']['finish']
    out = []
    for f in sorted(RECORDS.glob('2026*.json')):
        rec = json.loads(f.read_text(encoding='utf-8'))
        if rec.get('race_id') not in finish or 'scores' not in (rec.get('result') or {}):
            continue
        scores = {int(k): v for k, v in rec['result']['scores'].items()}
        win = {int(k): engine._low(v) for k, v in rec['odds']['win'].items() if int(k) in scores}
        final = {int(k): v for k, v in rec['probabilities']['final'].items()}
        out.append({'rec': rec, 'scores': scores, 'win_odds': win, 'final': final, 'winner': finish[rec['race_id']][0],
                    'asof': rec['odds_meta'].get('official_at')})
    return out


def load_pending(paths):
    out = []
    for p in paths:
        rec = json.loads(Path(p).read_text(encoding='utf-8'))
        scratched = set(rec.get('scratched', []))
        horses = [h for h in rec['horses'] if h['num'] not in scratched]
        pace = rec.get('pace') or engine.predict_pace(horses)
        bias = rec.get('bias') or {}
        io, fb = bias.get('inner_outer', 'flat'), bias.get('front_back', 'flat')
        scores = {h['num']: engine.total_score(h, pace, io, fb) for h in horses}
        got = live_card.fetch_odds(rec['race_id'])
        if got['odds'] and all(str(n) in got['odds']['win'] for n in scores):
            win, asof = {n: engine._low(got['odds']['win'][str(n)]) for n in scores}, f'前売り {got["asof"]}'
        else:
            key = sorted(k for k in horses[0] if k.startswith('pred_odds_'))[-1]
            win, asof = {h['num']: h[key] for h in horses}, f'予想オッズ {key}'
        out.append({'rec': rec, 'scores': scores, 'win_odds': win, 'final': None, 'winner': None, 'asof': asof})
    return out


def gap_for(s, order, target):
    """温度8で点数1位の勝率を target にするのに要る、2位との点差。"""
    others = np.delete(s, order[0])
    x = engine.SOFTMAX_TEMP * math.log(target / (1 - target) * np.exp(others / engine.SOFTMAX_TEMP).sum())
    return float(x - others.max())


def top5(r, nums, s, order, p8, pa):
    """点数上位5頭の内訳（ability・展開・加減点）と客観能力の勝率。"""
    rec = r['rec']
    horses = {h['num']: h for h in rec['horses']}
    pace = rec.get('pace') or engine.predict_pace([h for h in rec['horses'] if h['num'] in r['scores']])
    pp = softmax(pa, 8) if pa is not None else None
    out = []
    for i in order[:5]:
        h = horses[nums[i]]
        out.append({'num': nums[i], 'name': h['name'], 'score': float(s[i]), 'ability': h['ability'],
                    'pace': engine.pace_fit(h['style'], pace), 'flags': engine.flag_points(h),
                    'flag_names': h.get('plus', []) + h.get('minus', []), 'p8': float(p8[i]),
                    'mkt': 1 / r['win_odds'][nums[i]] / sum(1 / v for v in r['win_odds'].values()),
                    'proxy_p': float(pp[i]) if pp is not None else None})
    return out


def race_row(r):
    nums = sorted(r['scores'])
    s = np.array([r['scores'][n] for n in nums])
    m = np.array([1 / r['win_odds'][n] for n in nums])
    m = m / m.sum()
    order = np.argsort(-s, kind='stable')
    top = nums[order[0]]
    p8 = softmax(s, engine.SOFTMAX_TEMP)
    proxy = proxy_abilities(r['rec'], nums)
    pa = np.array([proxy[n] for n in nums]) if proxy else None
    name = {h['num']: h['name'] for h in r['rec']['horses']}
    fav = nums[int(np.argmax(m))]
    row = {
        'race': re.sub(r'\s.*', '', r['rec']['race'].split(' ', 2)[-1]) if ' ' in r['rec']['race'] else r['rec']['race'],
        'title': r['rec']['race'], 'asof': r['asof'], 'n': len(nums),
        'top': top, 'top_name': name[top], 'gap12': float(s[order[0]] - s[order[1]]),
        'gap14': float(s[order[0]] - s[order[3]]), 'sd': float(s.std()),
        'top_p8': float(p8[order[0]]), 'top_mkt': float(m[order[0]]),
        'fav': fav, 'fav_name': name[fav], 'fav_mkt': float(m.max()), 'fav_p8': float(p8[nums.index(fav)]),
        'fav_score_rank': int(np.where(order == nums.index(fav))[0][0]) + 1,
        'implied_t': implied_temp(s, m),
        'proxy_sd': float(pa.std()) if pa is not None else None,
        'proxy_gap12': float(np.sort(pa)[-1] - np.sort(pa)[-2]) if pa is not None else None,
        'proxy_implied_t': implied_temp(pa, m) if pa is not None else None,
        'corr': float(np.corrcoef(s, pa)[0, 1]) if pa is not None else None,
        'top5': top5(r, nums, s, order, p8, pa),
        'gap_for_mkt': gap_for(s, order, m[order[0]]),
    }
    if r['winner'] is not None:
        w = nums.index(r['winner'])
        row['winner'] = r['winner']
        row['winner_name'] = name[r['winner']]
        row['winner_score_rank'] = int(np.where(order == w)[0][0]) + 1
        row['loss'] = {f'T{t}': -math.log(softmax(s, t)[w]) for t in TEMPS}
        row['loss']['市場'] = -math.log(m[w])
        row['loss']['本番の混合（記録値）'] = -math.log(r['final'][r['winner']])
        row['loss']['一様'] = math.log(len(nums))
        if pa is not None:
            row['loss']['客観能力 T8'] = -math.log(softmax(pa, 8)[w])
    return row


def history_gaps(src):
    """過去の重賞（客観能力）で、1位と2位の点差ごとの1位の勝率。"""
    races = [r for r in json.loads(Path(src).read_text(encoding='utf-8')) if r['surface'] in ('芝', 'ダ')]
    params = np.array(json.loads((ROOT / 'reports' / 'ability_calibration.json').read_text(encoding='utf-8'))['raw'])
    rows = {b: {'n': 0, 'wins': 0, 'p8': 0.0, 'mkt': 0.0, 'gaps': []} for b in GAP_BINS}
    sds = []
    for r in ac.build(races):
        a = np.array([ac.ability(params, h['f']) for h in r['horses']])
        o = np.array([h['odds'] for h in r['horses']], dtype=float)
        m = (1 / o) / (1 / o).sum()
        order = np.argsort(-a, kind='stable')
        gap = a[order[0]] - a[order[1]]
        sds.append(a.std())
        for b in GAP_BINS:
            if b[0] <= gap < b[1]:
                x = rows[b]
                x['n'] += 1
                x['wins'] += int(r['horses'][order[0]]['win'])
                x['p8'] += softmax(a, 8)[order[0]]
                x['mkt'] += m[order[0]]
                x['gaps'].append(gap)
    return rows, float(np.median(sds)), len(sds)


def pct(v):
    return '—' if v is None else f'{v:.1%}'


def fmt(v, d=3):
    return '—' if v is None else f'{v:.{d}f}'


def main(dst, *pending):
    settled = [race_row(r) for r in load_settled()]
    pend = [race_row(r) for r in load_pending(pending)] if pending else []
    hist, hist_sd, hist_n = history_gaps(ROOT / 'data' / 'graded_2021_2026.json')
    out = ['# 点数の上位差と温度8の物差しの検証（判断時点の記録）', '',
           f'エンジンは変えていない（温度 {engine.SOFTMAX_TEMP}・混合率 {engine.MODEL_WEIGHT}）。'
           f'精算済み {len(settled)} レースは判断時の総合点（result.scores）と判断時の単勝オッズ、未確定 {len(pend)} レースは'
           'エンジンと同じ計算の点数（バイアスはフラット）と取得時点の前売りオッズ。市場の勝率は単勝オッズの逆数を正規化したもの'
           '（本番の計算用市場は三連単由来なので少しずれる）。', '',
           '**レース数が少ないので、ここでの対数損失の差で温度や点数の付け方を決めることはできない。** 見るのは「どこがずれているか」の形。', '']

    out += ['## 1. 点数の広がりと、市場から見た温度', '',
            '「市場から見た温度」は、点数を softmax(点数/T) にしたとき市場の単勝勝率に一番近くなる T。'
            '8より小さければ、市場は点数の差を本番より大きく見ている（点数が詰まりすぎ）。'
            '客観能力は過去の重賞で温度8に合わせて推定した点数なので、同じレースでの広がりを比べる物差しになる'
            f'（過去 {hist_n} レースの客観能力の標準偏差の中央値 {hist_sd:.1f}）。', '',
            '| レース | 頭数 | 1位−2位 | 1位−4位 | 点数の標準偏差 | 客観能力の標準偏差 | 市場から見た温度（点数） | 同（客観能力） | 点数と客観能力の相関 |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in settled + pend:
        out.append(f'| {r["race"]} | {r["n"]} | {r["gap12"]:.1f} | {r["gap14"]:.1f} | {r["sd"]:.1f} | {fmt(r["proxy_sd"], 1)} | '
                   f'{r["implied_t"]:.1f} | {fmt(r["proxy_implied_t"], 1)} | {fmt(r["corr"], 2)} |')

    out += ['', '## 2. 点数1位と1番人気（温度8の勝率と市場）', '',
            '| レース | オッズ時点 | 点数1位 | 温度8 | 市場 | 1番人気 | 点数の順位 | 温度8 | 市場 |', '|---|---|---|---:|---:|---|---:|---:|---:|']
    for r in settled + pend:
        out.append(f'| {r["race"]} | {r["asof"]} | {r["top"]} {r["top_name"]} | {r["top_p8"]:.1%} | {r["top_mkt"]:.1%} | '
                   f'{r["fav"]} {r["fav_name"]} | {r["fav_score_rank"]} | {r["fav_p8"]:.1%} | {r["fav_mkt"]:.1%} |')

    out += ['', '温度8で点差 g は勝率の比 e^(g/8) になる: ' +
            '・'.join(f'{g}点差 → {math.exp(g / 8):.2f}倍' for g in (1, 3, 5, 10, 15)) + '。', '']

    if settled:
        keys = list(settled[0]['loss'])
        out += ['## 3. 精算済みレースの勝ち馬の対数損失（−log p、小さいほど良い）', '',
                '| レース | 勝ち馬 | 点数の順位 | ' + ' | '.join(keys) + ' |', '|---|---|---:|' + '---:|' * len(keys)]
        for r in settled:
            out.append(f'| {r["race"]} | {r["winner"]} {r["winner_name"]} | {r["winner_score_rank"]} | ' +
                       ' | '.join(fmt(r['loss'].get(k)) for k in keys) + ' |')
        means = {k: np.mean([r['loss'][k] for r in settled if k in r['loss']]) for k in keys}
        out.append(f'| **平均** | | | ' + ' | '.join(f'**{means[k]:.3f}**' for k in keys) + ' |')
        out.append('')

    out += ['## 4. 点数上位5頭の内訳（展開は予想ペースの確率で平均、バイアスはフラット）', '',
            '「市場並みに要る点差」は、温度8のまま点数1位の勝率を市場の単勝勝率にそろえるのに要る2位との点差。', '']
    for r in settled + pend:
        out += [f'**{r["race"]}**（1位−2位 {r["gap12"]:.1f} 点、市場並みに要る点差 {r["gap_for_mkt"]:.1f} 点）', '',
                '| 馬 | 総合点 | ability | 展開 | 加減点（フラグ） | 温度8 | 客観能力 T8 | 市場 |', '|---|---:|---:|---:|---|---:|---:|---:|']
        for x in r['top5']:
            flags = '、'.join(x['flag_names']) or 'なし'
            out.append(f'| {x["num"]} {x["name"]} | {x["score"]:.1f} | {x["ability"]} | {x["pace"]:+.1f} | {x["flags"]:+d}（{flags}） | '
                       f'{x["p8"]:.1%} | {pct(x["proxy_p"])} | {x["mkt"]:.1%} |')
        out.append('')

    out += ['## 5. 過去の重賞（客観能力・確定オッズ）: 1位と2位の点差ごとの1位の勝率', '',
            '客観能力は温度8を前提に推定した点数。点差が大きい帯で実際の勝率が温度8の予測を上回るなら、'
            '温度8は点差を小さく見積もっている。', '',
            '| 1位−2位 | レース数 | 1位の実際の勝率 | 温度8の予測 | 市場 |', '|---|---:|---:|---:|---:|']
    for (lo, hi), x in hist.items():
        if x['n']:
            label = f'{lo}点以上' if hi == math.inf else f'{lo}〜{hi}点'
            out.append(f'| {label} | {x["n"]} | {x["wins"] / x["n"]:.1%} | {x["p8"] / x["n"]:.1%} | {x["mkt"] / x["n"]:.1%} |')
    out.append('')
    Path(dst).write_text('\n'.join(out) + '\n', encoding='utf-8')
    Path(dst).with_suffix('.json').write_text(json.dumps(
        {'settled': settled, 'pending': pend,
         'history_gaps': [{'lo': lo, 'hi': None if hi == math.inf else hi, 'n': x['n'], 'wins': x['wins'],
                           'p8': x['p8'], 'mkt': x['mkt']} for (lo, hi), x in hist.items()],
         'history_proxy_sd_median': hist_sd}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('\n'.join(out))


if __name__ == '__main__':
    main(*sys.argv[1:])
