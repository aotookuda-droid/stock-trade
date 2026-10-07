"""前走のレベルと近走の4角位置を、過去の重賞で市場と比べて確かめ、今年の出走馬に当てはめる。

使い方:
  python3 -m keiba.research.form_check keiba/records/20261011_アイルランドトロフィー.json keiba/reports/form_アイルランドトロフィー.md \
      [202505040411,202405040411,...]（同レースの過去 race_id をカンマ区切り。省くと trends.race_ids(レース名) の過去10年）

- 4角位置: 各走の最後の通過順 ÷ 頭数（0 に近いほど前）。判断日より前の直近5走（障害・通過順なしを除く）の平均を
  「近走4角」とし、前（〜0.25）・中前（〜0.5）・中後（〜0.75）・後（0.75〜）に分ける
- 前走レベル: 前走に出ていた他の馬のうち、判断日までに JRA 重賞を勝っていた頭数（keiba/data の重賞データ 2016/10〜で数える）。
  前走の時点の実績と、その後の勝利の両方を含む。0頭・1頭・2〜3頭・4頭以上に分ける。前走が重賞データに無いレース
  （L・OP・条件戦）の場合は、前走の結果ページを取得して同じく数える
- 照合: 2021/10〜の JRA 平地芝重賞（全馬の戦績がキャッシュ済みの期間）で、区分ごとに1着・3着以内の実数を
  単勝オッズから作った市場の見込み（勝率は 1/オッズ を正規化、3着以内は単勝市場の λ（engine.PLACE_DISCOUNT_WIN_MARKET）で補正した Benter 型）と比べる（実数 ÷ 見込み）。
  市場の見込みを超える区分は「市場が過小評価」、下回る区分は「過大評価」の候補。区分を見てから選んだ比較は
  多重比較になるので、z（(実数−見込み)/√Σp(1−p)）が ±2 を超えない差は誤差の範囲として扱う
- 同じレースの過去10回（2016〜）は、戦績が無い馬だけ取得して同じ区分で集計する（サンプルが少ないので参考）

市場は近走の成績を織り込んでいるので、ここでの区分は ability に足す材料ではなく、評価の見直しのきっかけとして使う。
"""
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from keiba import engine
from keiba.research import history
from keiba.research.calibration import benter_orders

DATA = Path(__file__).resolve().parents[1] / 'data'
GRADED = [DATA / 'graded_2016_2021.json', DATA / 'graded_2021_2026.json']
BACKTEST_FROM = '2021-10-02'
LEVEL_LABELS = ('0〜2頭', '3〜5頭', '6〜8頭', '9頭以上')
AFTER_LABELS = ('2割未満', '2〜4割', '4割以上', 'その後の出走3未満')
OTHER = '前走が重賞データ外'
C4_LABELS = ('前', '中前', '中後', '後')
JRA = ('札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉')


@lru_cache(maxsize=None)
def runs(horse_id):
    """全戦績 [{date, race_id, name, venue, field, rank, course, passage, margin}]（新しい順）。"""
    raw = history._get(f'https://db.netkeiba.com/horse/ajax_horse_results.html?input=UTF-8&output=json&id={horse_id}',
                       f'horse_{horse_id}.json', 'utf-8')
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', json.loads(raw)['data'], re.S):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        c = [history._text(x) for x in cells]
        if len(c) < 26:
            continue
        rid = re.search(r'/race/(\w+)/', cells[4])
        out.append({'date': c[0].replace('/', '-'), 'race_id': rid.group(1) if rid else None, 'name': c[4],
                    'venue': c[1], 'field': int(c[6]) if c[6].isdigit() else None, 'rank': c[11],
                    'course': c[14], 'passage': c[25], 'margin': c[19]})
    return tuple(out)


def fresh_runs(horse_id, max_age_days=2):
    """キャッシュが古ければ取り直した戦績（今年の出走馬・前走上位馬のその後を見る用）。"""
    path = history.CACHE / f'horse_{horse_id}.json'
    if path.exists() and time.time() - path.stat().st_mtime > max_age_days * 86400:
        path.unlink()
        runs.cache_clear()
    return runs(horse_id)


def corner4(run):
    m = re.findall(r'\d+', run['passage'] or '')
    if not m or not run['field'] or run['course'].startswith('障'):
        return None
    return min(int(m[-1]) / run['field'], 1.0)


def recent_c4(hist, before, n=5):
    vals = [(r, corner4(r)) for r in hist if r['date'] < before]
    vals = [(r, v) for r, v in vals if v is not None][:n]
    return vals


def c4_label(v):
    return None if v is None else C4_LABELS[0 if v <= 0.25 else 1 if v <= 0.5 else 2 if v <= 0.75 else 3]


def level_label(n):
    return None if n is None else LEVEL_LABELS[0 if n <= 2 else 1 if n <= 5 else 2 if n <= 8 else 3]


def after_label(n, top3):
    if n is None:
        return None
    if n < 3:
        return AFTER_LABELS[3]
    r = top3 / n
    return AFTER_LABELS[0 if r < 0.2 else 1 if r < 0.4 else 2]


class Graded:
    """重賞データ（race_id → 出走馬、horse_id → 重賞勝ちの日付）。"""

    def __init__(self, extra=()):
        self.races = {}
        for p in GRADED:
            for r in json.loads(p.read_text(encoding='utf-8')):
                self.races[r['race_id']] = r
        for r in extra:
            self.races[r['race_id']] = r
        self.wins = defaultdict(list)
        for r in self.races.values():
            for h in r['horses']:
                if h['rank'] == '1' and h['horse_id']:
                    self.wins[h['horse_id']].append(r['date'])

    def runners(self, race_id, fetch=False):
        if race_id in self.races:
            return self.races[race_id]['horses']
        if fetch and race_id and race_id[4:6].isdigit() and int(race_id[4:6]) <= 10:
            return history.parse_race(history.race_page(race_id), race_id)['horses']
        return None

    def level(self, race_id, me, before, fetch=False):
        """前走の他の出走馬のうち before までに重賞を勝っていた馬 [(馬名, 勝利数)]。分からなければ None。"""
        hs = self.runners(race_id, fetch)
        if hs is None:
            return None
        out = []
        for h in hs:
            if h['horse_id'] and h['horse_id'] != me and h['rank'] not in ('取', '除'):
                w = [d for d in self.wins[h['horse_id']] if d < before]
                if w:
                    out.append((h['name'], len(w)))
        return out


def features(g, horse_id, before, fetch=False):
    hist = fresh_runs(horse_id) if fetch else runs(horse_id)
    prev = next((r for r in hist if r['date'] < before), None)
    c4 = recent_c4(hist, before)
    mean = sum(v for _, v in c4) / len(c4) if len(c4) >= 3 else None
    lv = g.level(prev['race_id'], horse_id, before, fetch) if prev else None
    graded_prev = bool(prev and prev['race_id'] in g.races)
    n = top3 = None
    hs = g.runners(prev['race_id'], fetch) if prev and (graded_prev or fetch) else None
    if hs:
        # 前走の上位5頭（自身を除く）の、前走後〜判断日前の全出走（クラス問わず）での3着内
        top = sorted((x for x in hs if x['rank'].isdigit() and x['horse_id'] and x['horse_id'] != horse_id),
                     key=lambda x: int(x['rank']))[:5]
        n = top3 = 0
        for x in top:
            for r in (fresh_runs if fetch else runs)(x['horse_id']):
                if prev['date'] < r['date'] < before and r['rank'] not in ('取', '除', ''):
                    n += 1
                    top3 += r['rank'] in ('1', '2', '3')
    return {'prev': prev, 'graded_prev': graded_prev, 'c4': c4, 'c4_mean': mean, 'c4_label': c4_label(mean),
            'level': lv, 'level_label': level_label(None if lv is None else len(lv)),
            'after_n': n, 'after_top3': top3, 'after_label': after_label(n, top3)}


def market(r):
    hs = [h for h in r['horses'] if h['rank'] not in ('取', '除') and h['odds']]
    p = engine._normalize({h['num']: 1 / h['odds'] for h in hs})
    # 3着以内は Harville 式だと人気薄を過小評価する（keiba/reports/calibration.md）ので、単勝市場の λ で補正する
    place = defaultdict(float)
    for order, q in benter_orders(p, *engine.PLACE_DISCOUNT_WIN_MARKET).items():
        for k in order:
            place[k] += q
    return p, place


def tally(races, g, cached_only=True):
    """区分ごとの [出走, 1着, 3着内, 勝率見込み和, 3着内見込み和, 3着内の分散和]。"""
    stats = {k: defaultdict(lambda: [0, 0, 0, 0.0, 0.0, 0.0]) for k in ('近走4角', '前走レベル', '前走上位馬のその後')}
    for r in races:
        hs = [h for h in r['horses'] if h['rank'] not in ('取', '除') and h['odds'] and h['horse_id']]
        if len(hs) < 5 or sum(h['rank'] == '1' for h in hs) != 1:
            continue
        pw, pp = market(r)
        for h in hs:
            if cached_only and not (history.CACHE / f"horse_{h['horse_id']}.json").exists():
                continue
            f = features(g, h['horse_id'], r['date'])
            rank = int(h['rank']) if h['rank'].isdigit() else 99
            graded = f['graded_prev']
            for key, lab in (('近走4角', f['c4_label']), ('前走レベル', f['level_label'] if graded else OTHER),
                             ('前走上位馬のその後', f['after_label'] if graded else OTHER)):
                if lab is None:
                    continue
                s = stats[key][lab]
                s[0] += 1
                s[1] += rank == 1
                s[2] += rank <= 3
                s[3] += pw[h['num']]
                s[4] += pp[h['num']]
                s[5] += pp[h['num']] * (1 - pp[h['num']])
    return stats


def turf_graded(g, place=None, dist=None):
    out = []
    for r in g.races.values():
        if r['date'] < BACKTEST_FROM or r['surface'] != '芝' or r['place'] not in JRA:
            continue
        if place and r['place'] not in place:
            continue
        if dist and not dist[0] <= r['distance'] <= dist[1]:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r['date'])


def table(stats, key, order):
    rows = ['| 区分 | 出走 | 1着 | 市場の見込み | 実数÷見込み | 3着内 | 市場の見込み | 実数÷見込み | z（3着内） |',
            '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for lab in order:
        s = stats[key].get(lab)
        if not s or not s[0]:
            continue
        z = (s[2] - s[4]) / math.sqrt(s[5]) if s[5] else 0
        rows.append(f'| {lab} | {s[0]} | {s[1]} | {s[3]:.1f} | {s[1] / s[3]:.2f} | {s[2]} | {s[4]:.1f} | '
                    f'{s[2] / s[4]:.2f} | {z:+.1f} |')
    return rows


def lookup(stats, key, lab):
    s = stats[key].get(lab)
    if not s or not s[0]:
        return '—'
    return f"{s[2] / s[4]:.2f}（{s[2]}/{s[4]:.1f}、n={s[0]}）"


def recent_graded(start, end):
    return [history.parse_race(history.race_page(rid), rid) for rid in history.graded_race_ids(start, end)]


def main(record_path, out, past_ids=''):
    rec = json.loads(Path(record_path).read_text(encoding='utf-8'))
    m = re.match(r'(\d{4})/(\d{2})/(\d{2}) (\S+?)\d+R (.+?)\(', rec['race'])
    race_date = f'{m[1]}-{m[2]}-{m[3]}'
    place, name = m[4], m[5]
    dist = int(re.search(r'(\d{4})', rec['race'].split(')')[1]).group(1))
    last = max(r['date'] for p in GRADED for r in json.loads(p.read_text(encoding='utf-8')))
    # 結果が出ているレースだけ（未施行のページをキャッシュに残さない）
    end = min(date.fromisoformat(race_date), date.today()) - timedelta(days=1)
    extra = recent_graded(date.fromisoformat(last) + timedelta(days=1), end)
    g = Graded(extra)

    all_turf = turf_graded(g)
    near = turf_graded(g, (place,) if place != '京都' else ('京都', '阪神'), (dist - 200, dist + 200))
    s_all = tally(all_turf, g)
    s_near = tally(near, g)
    if not past_ids:
        from keiba.research import trends
        year = int(race_date[:4])
        past_ids = ','.join(rid for rid in trends.race_ids(name) if year - 10 <= int(rid[:4]) < year)
    past = [g.races.get(rid) or history.parse_race(history.race_page(rid), rid) for rid in past_ids.split(',') if rid]
    s_past = tally(past, g, cached_only=False)

    # 今年の出走馬
    shutuba = history._get(f'https://race.netkeiba.com/race/shutuba_past.html?race_id={rec["race_id"]}',
                           f'shutuba_past_{rec["race_id"]}.html', 'utf-8')
    ids = {}
    for hid, nm in re.findall(r'class="Horse02">\s*<a href="[^"]*/horse/(\w+)"[^>]*>\s*(?:<span[^>]*></span>)?([^<]+)</a>',
                              shutuba):
        ids[nm.strip()] = hid
    cur = []
    for h in rec['horses']:
        f = features(g, ids[h['name']], race_date, fetch=True)
        prev = f['prev']
        after = []
        if prev and prev['race_id']:
            hs = g.runners(prev['race_id'], fetch=True) or []
            top = sorted((x for x in hs if x['rank'].isdigit() and x['horse_id'] != ids[h['name']]),
                         key=lambda x: int(x['rank']))[:3]
            for x in top:
                later = [r for r in fresh_runs(x['horse_id']) if prev['date'] < r['date'] < race_date]
                after.append((x['name'], x['rank'], [f"{r['name']}{r['rank']}着" for r in reversed(later)]))
        cur.append({'name': h['name'], 'ability': h['ability'], 'style': h['style'], 'f': f, 'after': after})

    cond = f'{place}{"・阪神" if place == "京都" else ""}芝{dist - 200}〜{dist + 200}m'
    lines = [f'## {name}: 前走レベルと近走4角位置の照合', '',
             f'判断日 {race_date}。照合は {BACKTEST_FROM}〜の JRA 芝重賞（{len(all_turf)}レース）、うち {cond}（{len(near)}レース）、'
             f'同レースの過去{len(past)}回（2021年以前の馬は戦績を取得して集計）。実数÷見込みは市場（単勝オッズ）の見込みに対する実績。', '',
             '- 近走4角: 直近5走（3走以上ある馬だけ）の「4角の位置÷頭数」の平均。前〜0.25・中前〜0.5・中後〜0.75・後',
             '- 前走レベル: 前走の他の出走馬のうち、判断日までに JRA 重賞を勝っていた頭数（重賞データ 2016/10〜）',
             '- 前走上位馬のその後: 前走の上位5頭（自身を除く）の、前走後〜判断日前の全出走（クラス問わず）での3着内率',
             f'- 照合の区分は前走が重賞データ（2016/10〜の JRA 重賞）にある馬だけ。それ以外は「{OTHER}」にまとめる。'
             '今年の馬で前走が重賞以外の場合は、前走の結果ページから同じ値を出して参考に表示する（照合は「前走が重賞データ外」の行）', '',
             '市場の見込みは、勝率=1/オッズの正規化、3着内=単勝市場の λ で補正した Benter 型（Harville 式は人気薄の3着内を過小評価する）。'
             '人気薄ほど単勝の実績が市場を下回る傾向（keiba/reports/market_check.md）は補正していないので、人気薄の多い区分（後方の馬など）の1着の比は低めに出る。'
             '市場は近走の成績を織り込んでいる。z が ±2 を超えない差は誤差の範囲（区分を見てから比べた多重比較でもある）。', '']
    for title, st in (('芝重賞全体', s_all), (cond, s_near), (f'{name} 過去{len(past)}回（参考）', s_past)):
        lines += [f'### {title}', '', '近走4角', ''] + table(st, '近走4角', C4_LABELS)
        lines += ['', '前走レベル', ''] + table(st, '前走レベル', LEVEL_LABELS + (OTHER,))
        lines += ['', '前走上位馬のその後', ''] + table(st, '前走上位馬のその後', AFTER_LABELS + (OTHER,)) + ['']

    def both(key, lab):
        return '／'.join(lookup(st, key, lab) for st in (s_all, s_near)) if lab else '—'

    lines += ['### 今年の登録馬', '',
              '区分の値は「3着内の実数÷市場の見込み（芝重賞全体／' + cond + '）」。', '',
              '| 馬 | ability | 脚質 | 近走4角（新しい順） | 平均・区分 | 区分の3着内 | 前走 | 前走レベル（重賞勝ち馬） | 区分の3着内 | '
              '上位馬のその後 | 区分の3着内 |',
              '|---|---:|---|---|---|---|---|---|---|---|---|']
    for c in cur:
        f = c['f']
        c4 = ' '.join(f"{re.findall(r'[0-9]+', r['passage'])[-1]}/{r['field']}" for r, _ in f['c4'])
        mean = f"{f['c4_mean']:.2f} {f['c4_label']}" if f['c4_mean'] is not None else '—'
        lv = f['level']
        lvtxt = '—' if lv is None else f"{len(lv)}頭" + (f"（{'、'.join(n for n, _ in lv[:6])}{'…' if len(lv) > 6 else ''}）" if lv else '')
        prev = f['prev']
        ptxt = f"{prev['date'][5:]} {prev['name']} {prev['rank']}着" if prev else '—'
        graded = f['graded_prev']
        aft = '—' if f['after_n'] is None else f"{f['after_top3']}/{f['after_n']}"
        lines.append(f"| {c['name']} | {c['ability']} | {c['style']} | {c4} | {mean} | {both('近走4角', f['c4_label'])} | {ptxt} | "
                     f"{lvtxt} | {both('前走レベル', f['level_label'] if graded else OTHER)} | {aft} | "
                     f"{both('前走上位馬のその後', f['after_label'] if graded else OTHER)} |")
    lines += ['', '#### 前走上位馬のその後（明細）', '']
    for c in cur:
        lines.append(f"- {c['name']}（{c['f']['prev']['name'] if c['f']['prev'] else '—'}）: " +
                     ' ／ '.join(f"{n}({r}着)→{'・'.join(a) if a else '未出走'}" for n, r, a in c['after']))
    Path(out).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    Path(out).with_suffix('.json').write_text(json.dumps({
        'race': rec['race'], 'stats': {t: {k: dict(v) for k, v in st.items()} for t, st in
                                       (('all', s_all), ('near', s_near), ('past', s_past))},
        'horses': [{'name': c['name'], 'c4_mean': c['f']['c4_mean'], 'c4_label': c['f']['c4_label'],
                    'prev': c['f']['prev'], 'level': c['f']['level'], 'level_label': c['f']['level_label'],
                    'after_n': c['f']['after_n'], 'after_top3': c['f']['after_top3'], 'after_label': c['f']['after_label'],
                    'after': c['after']} for c in cur]}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main(*sys.argv[1:])
