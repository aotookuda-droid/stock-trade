"""ペース確率（engine.PACE_PRIOR）と位置取り適性（engine.PACE_FIT）を過去の重賞で確かめる。

使い方: python3 -m keiba.research.pace_calibration keiba/data/graded_2021_2026.json keiba/reports/pace_calibration.md

1. 実際のペース: 各レースの前半3F − 上がり3F（db.netkeiba のレースページのペース欄）。大きいほど前半が遅い。
   芝/ダート × 距離帯ごとに、学習期間の3分位でスロー・ミドル・ハイに分ける（距離による差を除くため。
   どの距離帯もおおむね3分の1ずつになる相対的な定義）
2. ハナ候補: 各出走馬のそのレースより前の直近5走（障害・通過順のない海外などを除く）で、最初の通過順が1番手の回数が2回以上
   （engine.is_front_candidate と同じ基準）。比較用に旧基準（1〜2番手が2回以上）も数える
3. ハナ候補の頭数ごとの実際のペースの分布。学習期間（〜2024年）で出した確率を検証期間（2025年〜）の対数損失で、
   一様（各1/3）・現在の PACE_PRIOR と比べる
4. 位置取り適性: 事前の脚質（直近5走の最初の通過順の平均を頭数で割ったもの）× 実際のペースごとに、
   3着以内の数を市場（単勝オッズの Harville 式）の期待値と比べる。市場が能力を織り込んでいるので、比はペースによる上振れ・下振れを表す

レース前に分からない実際のペースで分けているので、4 は「ペースを当てられた場合の効果の大きさ」を示す。
馬の戦績は keiba.research.history のキャッシュを使う（無い馬は取得する）。
"""
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from keiba import engine
from keiba.research import history

PACES = ('slow', 'middle', 'high')
PACE_JA = {'slow': 'スロー', 'middle': 'ミドル', 'high': 'ハイ'}
STYLES = ('逃げ', '先行', '差し', '追込')
TEST_FROM = '2025-01-01'
# 取消・除外は出走していないので除く。競走中止・失格は出走した馬なので残す（勝ち・3着以内には数えない）
NOT_STARTED = ('取', '除')
# 学習期間のレースがこれ未満の距離帯は、検証期間を境界の推定に使わないよう集計から外す
MIN_TRAIN_BAND = 6


def distance_band(distance):
    if distance <= 1400:
        return '〜1400'
    if distance <= 1800:
        return '1500〜1800'
    if distance <= 2200:
        return '1900〜2200'
    return '2300〜'


def race_pace(race_id):
    """(前半3F, 上がり3F)。ペース欄が無ければ None。"""
    html = history.race_page(race_id)
    m = re.search(r'ペース.*?\((\d+\.\d)-(\d+\.\d)\)', html, re.S)
    return (float(m.group(1)), float(m.group(2))) if m else None


def first_positions(horse_id, before):
    """before（YYYY-MM-DD）より前の直近5走の [(最初の通過順, 頭数)]（新しい順）。"""
    raw = history._get(f'https://db.netkeiba.com/horse/ajax_horse_results.html?input=UTF-8&output=json&id={horse_id}',
                       f'horse_{horse_id}.json', 'utf-8')
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', json.loads(raw)['data'], re.S):
        c = [history._text(x) for x in re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)]
        if len(c) < 26 or c[0].replace('/', '-') >= before or c[14].startswith('障'):
            continue
        first = re.match(r'(\d+)', c[25])
        if first and c[6].isdigit():
            out.append((int(first.group(1)), int(c[6])))
        if len(out) == 5:
            break
    return out


def style_of(positions):
    if sum(1 for p, _ in positions if p == 1) >= 2:
        return '逃げ'
    rel = sum((p - 1) / max(n - 1, 1) for p, n in positions) / len(positions)
    return '先行' if rel <= 0.3 else '差し' if rel <= 0.6 else '追込'


def build(races):
    rows = []
    for r in races:
        pace = race_pace(r['race_id'])
        runners = [h for h in r['horses'] if h['rank'] not in NOT_STARTED and h['horse_id']]
        if not pace or len(runners) < 5:
            continue
        horses = []
        for h in runners:
            pos = first_positions(h['horse_id'], r['date'])
            horses.append({**h, 'positions': pos, 'style': style_of(pos) if pos else None})
        rows.append({'race_id': r['race_id'], 'name': r['name'], 'date': r['date'], 'surface': r['surface'],
                     'distance': r['distance'], 'band': f"{r['surface']}{distance_band(r['distance'])}",
                     'index': pace[0] - pace[1], 'horses': horses,
                     'leaders': sum(1 for h in horses if engine.is_front_candidate([p for p, _ in h['positions']])),
                     'old_leaders': sum(1 for h in horses if sum(1 for p, _ in h['positions'] if p <= 2) >= 2)})
    return rows


def label_paces(rows):
    """学習期間だけの3分位で、距離帯ごとにスロー（前半が遅い＝index大）・ミドル・ハイに分ける。
    学習期間のレースが MIN_TRAIN_BAND 未満の距離帯は境界を作れないので、その距離帯のレースを外す（戻り値の dropped）。"""
    cuts, dropped = {}, {}
    for band in {r['band'] for r in rows}:
        xs = sorted(r['index'] for r in rows if r['band'] == band and r['date'] < TEST_FROM)
        if len(xs) < MIN_TRAIN_BAND:
            dropped[band] = sum(1 for r in rows if r['band'] == band)
            continue
        cuts[band] = (xs[len(xs) // 3], xs[2 * len(xs) // 3])
    kept = []
    for r in rows:
        if r['band'] in cuts:
            lo, hi = cuts[r['band']]
            r['pace'] = 'high' if r['index'] < lo else 'slow' if r['index'] >= hi else 'middle'
            kept.append(r)
    return cuts, dropped, kept


def bucket(n):
    return min(n, 3)


def fit_prior(rows, key='leaders', smooth=1.0):
    counts = defaultdict(Counter)
    for r in rows:
        counts[bucket(r[key])][r['pace']] += 1
    return {k: {p: (counts[k][p] + smooth) / (sum(counts[k].values()) + 3 * smooth) for p in PACES} for k in range(4)}, counts


def log_loss(rows, prior, key='leaders'):
    return sum(-math.log(prior[bucket(r[key])][r['pace']]) for r in rows) / len(rows)


def paired(rows, a, b, key_a='leaders', key_b='leaders'):
    """レースごとの損失差（a − b）の平均と標準誤差。"""
    d = [math.log(b[bucket(r[key_b])][r['pace']]) - math.log(a[bucket(r[key_a])][r['pace']]) for r in rows]
    m = sum(d) / len(d)
    se = (sum((x - m) ** 2 for x in d) / (len(d) - 1) / len(d)) ** 0.5
    return m, se


def style_effect(rows):
    """{(脚質, ペース): [頭数, 3着以内, 市場の期待3着以内]}。"""
    acc = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        hs = [h for h in r['horses'] if h['odds']]
        if len(hs) < 5:
            continue
        place = engine.harville(engine._normalize({h['num']: 1 / h['odds'] for h in hs}))['place']
        for h in hs:
            if not h['style']:
                continue
            a = acc[(h['style'], r['pace'])]
            a[0] += 1
            a[1] += h['rank'].isdigit() and int(h['rank']) <= 3
            a[2] += place[h['num']]
    return acc


def report(rows, cuts, dropped):
    train = [r for r in rows if r['date'] < TEST_FROM]
    test = [r for r in rows if r['date'] >= TEST_FROM]
    fitted, counts = fit_prior(train)
    fitted_old, counts_old = fit_prior(train, 'old_leaders')
    uniform = {k: {p: 1 / 3 for p in PACES} for k in range(4)}
    full, counts_all = fit_prior(rows)
    out = ['# ペース確率と位置取り適性の検証（過去の重賞）', '',
           f'対象 {len(rows)} レース（学習 {len(train)}: 〜2024年、検証 {len(test)}: 2025年〜）。'
           '実際のペースは前半3F − 上がり3F を芝/ダート×距離帯ごとに学習期間だけの3分位で3つに分けたもの。'
           '取消・除外は除き、競走中止・失格は出走馬として残した。'
           + (f"学習期間のレースが{MIN_TRAIN_BAND}未満の距離帯（{'、'.join(f'{b} {n}レース' for b, n in sorted(dropped.items()))}）は"
              '検証期間を境界に使わないよう外した。' if dropped else ''), '',
           '前半3F − 上がり3F は中盤の緩みや長い仕掛けを1つの値に縮めた代理指標で、10/4 京都大賞典のような展開を十分には表さない。', '',
           '## 距離帯ごとの境界（前半3F − 上がり3F、秒）', '', '| 距離帯 | レース数 | ハイ（これ未満） | スロー（これ以上） |', '|---|---:|---:|---:|']
    for band in sorted(cuts):
        out.append(f"| {band} | {sum(1 for r in rows if r['band'] == band)} | {cuts[band][0]:+.1f} | {cuts[band][1]:+.1f} |")
    out += ['', '## ハナ候補の頭数と実際のペース（全期間）', '',
            '| ハナ候補 | レース数 | スロー | ミドル | ハイ |', '|---|---:|---:|---:|---:|']
    for k in range(4):
        c = counts_all[k]
        n = sum(c.values())
        if n:
            out.append(f"| {k if k < 3 else '3+'}頭 | {n} | " + ' | '.join(f'{c[p] / n:.0%}' for p in PACES) + ' |')
    out += ['', '旧基準（1〜2番手が2回以上）で数えた場合（全期間）:', '',
            '| 旧・逃げ候補 | レース数 | スロー | ミドル | ハイ |', '|---|---:|---:|---:|---:|']
    _, old_all = fit_prior(rows, 'old_leaders')
    for k in range(4):
        c = old_all[k]
        n = sum(c.values())
        if n:
            out.append(f"| {k if k < 3 else '3+'}頭 | {n} | " + ' | '.join(f'{c[p] / n:.0%}' for p in PACES) + ' |')
    out += ['', '## 検証期間の対数損失（小さいほど良い。一様は log 3 = 1.099）', '',
            '| 確率の出どころ | 対数損失 |', '|---|---:|',
            f'| 一様（各1/3） | {log_loss(test, uniform):.3f} |',
            f'| 現在の PACE_PRIOR（全期間で推定。検証期間を含むので参考） | {log_loss(test, engine.PACE_PRIOR):.3f} |',
            f'| 学習期間で推定（ハナ候補） | {log_loss(test, fitted):.3f} |',
            f"| 学習期間で推定（旧基準） | {log_loss(test, fitted_old, 'old_leaders'):.3f} |",
            '', 'レースごとの損失差（平均 ± 標準誤差、負なら左が良い）:', '',
            '| 比較 | 差 |', '|---|---:|',
            '| 学習期間で推定 − 一様 | {:+.3f} ± {:.3f} |'.format(*paired(test, fitted, uniform)),
            "| 学習期間で推定 − 旧基準で推定 | {:+.3f} ± {:.3f} |".format(*paired(test, fitted, fitted_old, 'leaders', 'old_leaders')),
            '', '本番の PACE_PRIOR は、この検証の後に全期間で推定し直した値（下の全期間の表）。全期間の値そのものは未使用期間で検証していない。',
            '', '学習期間で推定した確率（ハナ候補、各セルに1件ずつ足して平滑化）:', '',
            '| ハナ候補 | 学習レース数 | スロー | ミドル | ハイ |', '|---|---:|---:|---:|---:|']
    for k in range(4):
        out.append(f"| {k if k < 3 else '3+'}頭 | {sum(counts[k].values())} | "
                   + ' | '.join(f'{fitted[k][p]:.0%}' for p in PACES) + ' |')
    acc = style_effect(rows)
    out += ['', '## 脚質 × 実際のペース: 3着以内の数 ÷ 市場の期待（全期間）', '',
            '1.00 より大きければ市場の見積もりより3着以内に来た。脚質はレース前の直近5走の通過順から機械的に決めたもの。'
            '市場は確定単勝オッズの Harville 式（発走10分前のオッズではない）。実際のペースを事後に知って分けた集計で、'
            '比は勝率の補正倍率でも脚質の因果効果でもない。', '',
            '| 脚質 | ' + ' | '.join(PACE_JA[p] for p in PACES) + ' | 全体 |', '|---|---:|---:|---:|---:|']
    for s in STYLES:
        cells = []
        for p in PACES:
            n, hit, exp = acc[(s, p)]
            cells.append(f'{hit / exp:.2f}（{n}頭）' if exp else '-')
        n = sum(acc[(s, p)][0] for p in PACES)
        hit = sum(acc[(s, p)][1] for p in PACES)
        exp = sum(acc[(s, p)][2] for p in PACES)
        out.append(f'| {s} | ' + ' | '.join(cells) + f' | {hit / exp:.2f}（{n}頭） |')
    return '\n'.join(out) + '\n'


def main(src, dst):
    races = [r for r in json.loads(Path(src).read_text(encoding='utf-8')) if r['surface'] in ('芝', 'ダ')]
    rows = build(races)
    cuts, dropped, rows = label_paces(rows)
    text = report(rows, cuts, dropped)
    Path(dst).write_text(text, encoding='utf-8')
    Path(dst).with_suffix('.json').write_text(json.dumps(
        [{k: r[k] for k in ('race_id', 'name', 'date', 'band', 'index', 'pace', 'leaders', 'old_leaders')} for r in rows],
        ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main(*sys.argv[1:3])
