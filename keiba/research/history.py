"""過去レースのデータ収集（netkeiba の公開ページ）。分析は trends.py / market_check.py で行う。

- db.netkeiba.com/race/<race_id>/ : 着順・枠・馬番・性齢・斤量・通過・単勝オッズ・人気・払戻
- db.netkeiba.com/race/list.html?word=<レース名> : 同名レースの過去の race_id
- race.netkeiba.com/top/race_list_sub.html?kaisai_date=YYYYMMDD : その日の JRA レース一覧（重賞アイコン付き）
- db.netkeiba.com/horse/ajax_horse_results.html : 馬の全戦績

取得したページは keiba/data/cache/ に保存して再利用する（git 管理外）。1件ごとに PAUSE 秒あける。
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / 'data' / 'cache'
PAUSE = 0.6
UA = {'User-Agent': 'Mozilla/5.0'}


def _get(url, name, encoding):
    path = CACHE / name
    if path.exists():
        return path.read_text(encoding='utf-8')
    CACHE.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        text = r.read().decode(encoding, 'replace')
    path.write_text(text, encoding='utf-8')
    time.sleep(PAUSE)
    return text


def _text(s):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', s)).replace('&nbsp;', ' ').strip()


def race_page(race_id):
    return _get(f'https://db.netkeiba.com/race/{race_id}/', f'race_{race_id}.html', 'euc-jp')


def parse_race(html, race_id):
    """db のレースページから {race_id, name, date, surface, distance, place, field, horses, payouts}。"""
    intro = re.search(r'<dl class="racedata[^"]*">(.*?)</dl>', html, re.S)
    name = _text(re.search(r'<h1>(.*?)</h1>', intro.group(1), re.S).group(1)) if intro else ''
    course = re.search(r'(芝|ダ|障)[^\d]*(\d{3,4})m', _text(intro.group(1))) if intro else None
    smalltxt = re.search(r'<p class="smalltxt">(.*?)</p>', html, re.S)
    small = _text(smalltxt.group(1)) if smalltxt else ''
    d = re.search(r'(\d{4})年(\d{2})月(\d{2})日', small)
    place = re.search(r'\d+回(\S+?)\d+日目', small)
    table = html[html.find('race_table_01'):]
    table = table[:table.find('</table>')]
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S)
    head = [re.sub(r'\s', '', _text(c)) for c in re.findall(r'<th[^>]*>(.*?)</th>', rows[0], re.S)] if rows else []
    col = {h: i for i, h in enumerate(head)}
    horses = []
    for r in rows[1:]:
        raw = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        c = [_text(x) for x in raw]
        if len(c) < len(head):
            continue
        hid = re.search(r'/horse/(\w+)/', raw[col['馬名']])
        odds = c[col['単勝']].replace(',', '')
        horses.append({
            'rank': c[col['着順']], 'waku': int(c[col['枠番']]), 'num': int(c[col['馬番']]),
            'name': c[col['馬名']], 'horse_id': hid.group(1) if hid else None,
            'sexage': c[col['性齢']], 'kg': float(c[col['斤量']]) if c[col['斤量']] else None,
            'jockey': c[col['騎手']], 'passage': c[col['通過']], 'last3f': c[col['上り']],
            'odds': float(odds) if re.fullmatch(r'[\d.]+', odds) else None,
            'pop': int(c[col['人気']]) if c[col['人気']].isdigit() else None,
            'body': c[col['馬体重']],
        })
    payouts = {}
    pay = html[html.find('pay_table_01'):]
    pay = pay[:pay.rfind('</table>', 0, pay.find('馬場情報') if '馬場情報' in pay else len(pay)) + 8]
    for th, combos, pays in re.findall(r'<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>', pay, re.S):
        kind = _text(th)
        ks = [_text(x) for x in re.split(r'<br\s*/?>', combos) if _text(x)]
        ps = [int(_text(x).replace(',', '')) for x in re.split(r'<br\s*/?>', pays) if _text(x).replace(',', '').isdigit()]
        if ks and len(ks) == len(ps):
            payouts[kind] = dict(zip(ks, ps))
    return {'race_id': race_id, 'name': name,
            'date': f'{d[1]}-{d[2]}-{d[3]}' if d else None, 'place': place.group(1) if place else None,
            'surface': course.group(1) if course else None, 'distance': int(course.group(2)) if course else None,
            'field': len(horses), 'horses': horses, 'payouts': payouts}


def past_race_ids(word):
    """同名レースの race_id（新しい順）。"""
    q = urllib.parse.quote(word.encode('euc-jp'))
    html = _get(f'https://db.netkeiba.com/race/list.html?word={q}', f'list_{q.replace("%", "")}.html', 'euc-jp')
    ids = []
    for rid in re.findall(r'/race/(\d{12})/', html):
        if rid not in ids:
            ids.append(rid)
    return ids


def graded_race_ids(start, end):
    """start〜end の土日月で行われた JRA 平地重賞（GI・GII・GIII）の race_id。"""
    out = []
    d = start
    while d <= end:
        if d.weekday() in (0, 5, 6):
            html = _get(f'https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={d:%Y%m%d}',
                        f'list_{d:%Y%m%d}.html', 'utf-8')
            for rid, body in re.findall(r'race_id=(\d{12})[^"]*"[^>]*>(.*?)</a>', html, re.S):
                grades = re.findall(r'Icon_GradeType(\d+)', body)
                text = _text(body)
                if grades and grades[0] in ('1', '2', '3') and re.search(r'(芝|ダ)\d', text) and rid not in out:
                    out.append(rid)
        d += timedelta(days=1)
    return out


def horse_history(horse_id):
    """馬の全戦績 [{date, race_id, race, distance_text, rank, kg, jockey}]（新しい順）。"""
    raw = _get(f'https://db.netkeiba.com/horse/ajax_horse_results.html?input=UTF-8&output=json&id={horse_id}',
               f'horse_{horse_id}.json', 'utf-8')
    data = json.loads(raw)['data']
    out = []
    for r in re.findall(r'<tr[^>]*>(.*?)</tr>', data, re.S):
        raw_cells = re.findall(r'<td[^>]*>(.*?)</td>', r, re.S)
        c = [_text(x) for x in raw_cells]
        if len(c) < 15:
            continue
        rid = re.search(r'/race/(\w+)/', raw_cells[4])
        out.append({'date': c[0].replace('/', '-'), 'race_id': rid.group(1) if rid else None, 'race': c[4],
                    'course': c[14], 'rank': c[11], 'kg': c[13], 'jockey': c[12]})
    return out


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['graded']:
        start, end = (date.fromisoformat(x) for x in sys.argv[2:4])
        ids = graded_race_ids(start, end)
        print(len(ids))
        races = [parse_race(race_page(rid), rid) for rid in ids]
        Path(sys.argv[4]).write_text(json.dumps(races, ensure_ascii=False) + '\n', encoding='utf-8')
