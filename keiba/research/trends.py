"""同名重賞の過去10年の傾向（区分ごとの出走数・勝利・3着以内・複勝率）と、今年の出走馬の当てはまり。

使い方: python3 -m keiba.research.trends 毎日王冠 2016 2025 keiba/records/20261004_毎日王冠.json keiba/reports/trends_毎日王冠.md

検索語は netkeiba のレース名検索に渡す（スワンSは「スワンS」）。改称したレースは LINEAGE に系譜を書く。
出力先（5番目）を省くと画面に出すだけ。馬柱（live_card.load_trends）は trends_<レコードのレース名>.json を読むので、
出力ファイル名はレコードの race のレース名に合わせる。今年の馬の区分は馬番と start_at が入った（枠順確定後の）レコードで作る。

複勝率 = 3着以内の頭数 ÷ 出走頭数。サンプルが少ない区分（出走20頭未満）は参考程度。
人気・オッズの情報は市場が既に織り込んでいるので、傾向に合う馬に点数を足すかは別途判断する（ここでは表示だけ）。
"""
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from keiba.research import history

MIN_N = 20
RAN = ('中', '失')  # 着順欄が数字でなくても出走した馬


def _ordinal(d):
    return date.fromisoformat(d).toordinal()


def style(passage, field):
    """最初のコーナー通過順から脚質（逃げ・先行・差し・追込）。"""
    m = re.match(r'(\d+)', passage or '')
    if not m:
        return None
    pos = int(m.group(1))
    if pos == 1:
        return '逃げ'
    if pos <= max(2, round(field / 3)):
        return '先行'
    if pos <= round(field * 2 / 3):
        return '差し'
    return '追込'


def prev_race(hist, race_date):
    before = [r for r in hist if r['date'] < race_date]
    return before[0] if before else None


def race_label(name):
    return re.sub(r'\((G[I]+|GI|GII|GIII|L|OP|Jpn[I]+|[123]勝クラス|.勝クラス)\)', '', name).strip()


def buckets(h, field, race_date, hist):
    age = int(re.sub(r'\D', '', h['sexage']) or 0)
    prev = prev_race(hist, race_date) if hist is not None else None
    out = {
        '枠': f"{(h['waku'] + 1) // 2 * 2 - 1}-{(h['waku'] + 1) // 2 * 2}枠",
        '人気': ('1番人気' if h['pop'] == 1 else '2-3番人気' if h['pop'] <= 3 else '4-6番人気' if h['pop'] <= 6
               else '7-9番人気' if h['pop'] <= 9 else '10番人気以下') if h['pop'] else None,
        '年齢': f'{age}歳' if age < 7 else '7歳以上',
        '脚質': style(h['passage'], field),
    }
    if prev:
        weeks = (_ordinal(race_date) - _ordinal(prev['date'])) // 7
        out['前走間隔'] = '中5週以内' if weeks <= 6 else '中6〜9週' if weeks <= 10 else '中10〜17週' if weeks <= 18 else '中18週以上'
        rank = prev['rank']
        out['前走着順'] = ('前走1着' if rank == '1' else '前走2-3着' if rank in ('2', '3') else '前走4-5着' if rank in ('4', '5')
                       else '前走6-9着' if rank in ('6', '7', '8', '9') else '前走10着以下・その他')
        out['前走レース'] = race_label(prev['race'])
    return out


# 改称したレースの系譜（検索語, その語で拾った race_id のうち同じレースとして使う条件）。
# アイルランドトロフィーは2016年まで府中牝馬S（10月の4回東京）、2017〜2024年はアイルランドトロフィー府中牝馬S、
# 2025年からアイルランドT。2016年の「アイルランドT」は別の OP（芝2000）、2025年からの府中牝馬Sは6月の GⅢ なので除く
LINEAGE = {
    'アイルランドトロフィー': [('アイルランドトロフィー', lambda rid: rid[4:8] == '0504' and rid[:4] >= '2017'),
                       ('府中牝馬', lambda rid: rid[4:8] == '0504' and rid[:4] <= '2016')],
}


def race_ids(word):
    if word not in LINEAGE:
        return history.past_race_ids(word)
    out = []
    for w, keep in LINEAGE[word]:
        out += [rid for rid in history.past_race_ids(w) if keep(rid) and rid not in out]
    return sorted(out, reverse=True)


def collect(word, first_year, last_year):
    races = []
    for rid in race_ids(word):
        year = int(rid[:4])
        if first_year <= year <= last_year:
            r = history.parse_race(history.race_page(rid), rid)
            for h in r['horses']:
                h['hist'] = history.horse_history(h['horse_id']) if h['horse_id'] else None
            races.append(r)
    return sorted(races, key=lambda r: r['date'])


def table(races):
    stats = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    for r in races:
        for h in r['horses']:
            # 競走中止・失格は出走馬（3着外）として数え、取消・除外だけ除く
            if not h['rank'].isdigit() and h['rank'] not in RAN:
                continue
            rank = int(h['rank']) if h['rank'].isdigit() else 99
            for k, v in buckets(h, r['field'], r['date'], h['hist']).items():
                if v is None:
                    continue
                s = stats[k][v]
                s[0] += 1
                s[1] += rank == 1
                s[2] += rank <= 3
    return stats


def entries(race_id):
    """今年の出馬表（馬柱ページ）から {馬番: (horse_id, 性齢)}。"""
    html = history._get(f'https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}',
                        f'shutuba_past_{race_id}.html', 'utf-8', fresh=True)
    out = {}
    for row in re.findall(r'<tr class="HorseList[^"]*"[^>]*>(.*?)</tr>', html, re.S):
        num = re.search(r'<td class="Waku\d*"[^>]*>.*?</td>\s*<td[^>]*>\s*(\d+)\s*</td>', row, re.S)
        hid = re.search(r'class="Horse02">\s*<a href="https://db\.netkeiba\.com/horse/(\w+)"', row)
        sex = re.search(r'([牡牝セ]\d+)', history._text(re.sub(r'<td class="Past.*', '', row, flags=re.S)))
        if num and hid:
            out[int(num.group(1))] = (hid.group(1), sex.group(1) if sex else '')
    return out


def current_buckets(rec, odds_rank):
    """今年の出走馬の区分。人気は渡された順位（オッズ順）、前走は netkeiba の全戦績から。"""
    date_ = rec['start_at'][:10]
    out = {}
    ent = entries(rec['race_id'])
    for h in rec['horses']:
        hid, sexage = ent.get(h['num'], (None, ''))
        hist = history.horse_history(hid) if hid else None
        fake = {'waku': h['gate'], 'pop': odds_rank.get(h['num']), 'sexage': sexage, 'passage': ''}
        b = buckets(fake, len(rec['horses']), date_, hist)
        b['脚質'] = {'逃げ': '逃げ', '先行': '先行', '差し': '差し', '追込': '追込'}.get(h['style'])
        out[h['num']] = b
    return out


def report(word, races, stats, cur=None, names=None):
    lines = [f'## {word} 過去{len(races)}回（{races[0]["date"][:4]}〜{races[-1]["date"][:4]}）', '']
    venues = sorted({f"{r['place']}{r['surface']}{r['distance']}" for r in races})
    lines.append('開催: ' + '、'.join(venues) + '。複勝率=3着以内÷出走。出走20頭未満の区分は参考程度（*）。')
    lines.append('')
    lines.append('| 年 | 1着 | 2着 | 3着 |')
    lines.append('|---|---|---|---|')
    for r in races:
        top = sorted((h for h in r['horses'] if h['rank'].isdigit()), key=lambda h: int(h['rank']))[:3]
        lines.append(f"| {r['date'][:4]}（{r['place']}） | " + ' | '.join(
            f"{h['name']}（{h['pop']}人・{h['waku']}枠・{h['sexage']}）" for h in top) + ' |')
    for key in ('人気', '枠', '年齢', '脚質', '前走間隔', '前走着順', '前走レース'):
        rows = stats.get(key, {})
        items = sorted(rows.items(), key=lambda kv: (-kv[1][2], -kv[1][0]))
        if key == '前走レース':
            items = [kv for kv in items if kv[1][2] > 0 or kv[1][0] >= 5]
        lines += ['', f'### {key}', '', '| 区分 | 出走 | 1着 | 3着内 | 複勝率 | 今年の該当馬 |', '|---|---:|---:|---:|---:|---|']
        for v, (n, w, t) in items:
            mark = '' if n >= MIN_N else '*'
            who = ''
            if cur:
                who = '、'.join(f'{num}{names[num]}' for num, b in sorted(cur.items()) if b.get(key) == v)
            lines.append(f'| {v}{mark} | {n} | {w} | {t} | {t / n * 100:.0f}% | {who} |')
    return '\n'.join(lines)


def main(word, first, last, record_path=None, out=None):
    races = collect(word, int(first), int(last))
    stats = table(races)
    cur = names = None
    if record_path:
        rec = json.loads(Path(record_path).read_text(encoding='utf-8'))
        from keiba import live_card
        got = live_card.fetch_odds(rec['race_id'])
        win = {int(k): v for k, v in (got['odds'] or {}).get('win', {}).items()}
        if not win:
            win = {h['num']: h[sorted(k for k in h if k.startswith('pred_odds_'))[-1]] for h in rec['horses']}
        odds_rank = {n: i + 1 for i, n in enumerate(sorted(win, key=win.get))}
        cur = current_buckets(rec, odds_rank)
        names = {h['num']: h['name'] for h in rec['horses']}
    text = report(word, races, stats, cur, names)
    if out:
        Path(out).write_text(text + '\n', encoding='utf-8')
        # 馬柱用: 区分ごとの成績と、今年の出走馬の区分（人気は馬柱側でその時点のオッズ順から決める）
        Path(out).with_suffix('.json').write_text(json.dumps({
            'race': word, 'years': f"{races[0]['date'][:4]}〜{races[-1]['date'][:4]}", 'n_races': len(races),
            'venues': sorted({f"{r['place']}{r['surface']}{r['distance']}" for r in races}),
            'stats': {k: {v: s for v, s in rows.items()} for k, rows in stats.items()},
            'horses': {str(n): {k: v for k, v in b.items() if k != '人気'} for n, b in (cur or {}).items()},
        }, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(text)
    return stats, cur


if __name__ == '__main__':
    main(*sys.argv[1:])
