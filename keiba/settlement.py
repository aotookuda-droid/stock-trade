"""公式払戻による仮想精算。本番提案（result）と参考試算（reference）をそれぞれ精算して集計する。

使い方:
  python3 -m keiba.settlement fetch keiba/records/20261004_京都大賞典.json   # 公式払戻を取得して精算を保存
  python3 -m keiba.settlement report [出力.md]                               # レース別・週末別・累計を表示

- レース記録（判断時点の分析・オッズ・買い目）は読むだけで書き換えない。精算は keiba/settlements/<race_id>.json に保存する。
- 精算時に記録の判断部分のハッシュを保存し、集計時に記録が後から変わっていないか確認する。
- 取消・除外の馬を含む買い目は返還（枠連は、その枠の出走馬が足りなくなった場合だけ返還）。
- 回収率 = 払戻 ÷（提案額 − 返還）。購入・投票は行わない。
"""
import hashlib
import html
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from keiba import engine

RESULT_URL = 'https://race.netkeiba.com/race/result.html?race_id={}'
SETTLEMENT_DIR = Path(__file__).with_name('settlements')
RECORD_DIR = Path(__file__).with_name('records')
# 結果ページの行クラス → (券種, 組の区切り。None は1頭、'-' は順不同、'>' は着順どおり)
ROWS = {'Tansho': ('単勝', None), 'Fukusho': ('複勝', None), 'Wakuren': ('枠連', '-'), 'Umaren': ('馬連', '-'),
        'Wide': ('ワイド', '-'), 'Umatan': ('馬単', '>'), 'Fuku3': ('三連複', '-'), 'Tan3': ('三連単', '>')}
PLANS = {'本番': lambda r: r['result']['bets'],
         '参考（単勝市場のみ）': lambda r: r['reference']['win_market_only']['bets']}


def _text(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()


def _key(nums, sep):
    nums = [int(n) for n in nums]
    if sep == '-':
        nums = sorted(nums)
    return str(nums[0]) if sep is None else sep.join(map(str, nums))


def parse_result(page):
    """結果ページから {'payouts': {券種: {組: 100円あたり払戻}}, 'finish': [馬番...], 'scratched': [馬番...]}。"""
    payouts = {}
    for cls, body in re.findall(r'<tr class="(\w+)">(.*?)</tr>', page, re.S):
        if cls not in ROWS:
            continue
        kind, sep = ROWS[cls]
        result = re.search(r'<td class="Result">(.*?)</td>', body, re.S).group(1)
        pays = [int(_text(p).replace(',', '').replace('円', ''))
                for p in re.search(r'<td class="Payout">(.*?)</td>', body, re.S).group(1).split('<br />')
                if _text(p)]
        if sep is None:
            tickets = [[n] for n in re.findall(r'<span>(\d+)</span>', result)]
        else:
            tickets = [re.findall(r'<span>(\d+)</span>', ul) for ul in re.findall(r'<ul>(.*?)</ul>', result, re.S)]
        if len(tickets) != len(pays):
            raise ValueError(f'{kind} の組と払戻の数が合わない: {tickets} / {pays}')
        payouts[kind] = {_key(t, sep): p for t, p in zip(tickets, pays)}
    finish, scratched = [], []
    for row in re.findall(r'<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>(.*?)</tr>', page, re.S):
        rank = _text(re.search(r'<td class="Result_Num">(.*?)</td>', row, re.S).group(1))
        num = int(_text(re.search(r'<td class="Num Txt_C">(.*?)</td>', row, re.S).group(1)))
        if rank in ('取消', '除外'):
            scratched.append(num)
        elif rank.isdigit():
            finish.append((int(rank), num))
    if not payouts.get('単勝'):
        raise ValueError('単勝の払戻が見つからない（未確定か、ページ形式が変わった）')
    return {'payouts': payouts, 'finish': [n for _, n in sorted(finish)], 'scratched': sorted(scratched)}


def settle_bets(bets, official, gates):
    """買い目を精算する。戻り値 {'stake','refund','payout','profit','roi','tickets'}。"""
    scratched = set(official['scratched'])
    runners = {}
    for num, g in gates.items():
        if num not in scratched:
            runners[g] = runners.get(g, 0) + 1
    rows, stake, refund, payout = [], 0, 0, 0
    for b in bets:
        t, hs, x = b['type'], b['horses'], b['stake']
        if t == '枠連':
            need = {}
            for g in hs:
                need[g] = need.get(g, 0) + 1
            void = any(runners.get(g, 0) < n for g, n in need.items())
        else:
            void = any(h in scratched for h in hs)
        sep = '>' if t in ('馬単', '三連単') else ('-' if len(hs) > 1 else None)
        pay = 0 if void else official['payouts'].get(t, {}).get(_key(hs, sep), 0) * x // 100
        stake += x
        refund += x if void else 0
        payout += pay
        rows.append({'type': t, 'horses': hs, 'stake': x, 'refund': x if void else 0, 'payout': pay})
    return _summary(stake, refund, payout) | {'tickets': rows}


def _summary(stake, refund, payout):
    invested = stake - refund
    return {'stake': stake, 'refund': refund, 'payout': payout, 'profit': payout + refund - stake,
            'roi': payout / invested if invested else None}


def decision_hash(race):
    """判断時点の内容（オッズ・取消・結果・参考試算・確定）のハッシュ。"""
    keys = ('odds', 'odds_meta', 'scratched', 'result', 'reference', 'decision')
    blob = json.dumps({k: race.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def settle(record_path, page, source, fetched_at=None):
    race = engine._read_json(record_path)
    if not race.get('decision'):
        raise SystemExit('判断前のレースは精算しない（decision がない）')
    official = parse_result(page)
    gates = {h['num']: h['gate'] for h in race['horses']}
    plans = {}
    for name, get in PLANS.items():
        try:
            bets = get(race)
        except (KeyError, TypeError):
            continue
        plans[name] = settle_bets(bets, official, gates)
    return {'race_id': race['race_id'], 'race': race['race'], 'start_at': race['start_at'],
            'record': str(record_path), 'record_hash': decision_hash(race),
            'source': source, 'fetched_at': fetched_at or datetime.now().astimezone().isoformat(timespec='seconds'),
            'official': official, 'plans': plans}


def save(settlement, directory=SETTLEMENT_DIR):
    """精算を保存する。同じレースの精算が既にあれば、公式結果が同じなら何もしない・違えば止まる。"""
    directory.mkdir(exist_ok=True)
    path = directory / f"{settlement['race_id']}.json"
    if path.exists():
        old = engine._read_json(path)
        if old['official'] == settlement['official'] and old['record_hash'] == settlement['record_hash']:
            return path, 'unchanged'
        raise SystemExit(f'{path} は既にあり、内容が異なる。上書きしない（公式結果の訂正なら手で確認する）')
    engine._write_json(path, settlement)
    return path, 'saved'


def fetch(record_path):
    race = engine._read_json(record_path)
    url = RESULT_URL.format(race['race_id'])
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode('utf-8', 'replace')
    path, status = save(settle(record_path, page, url))
    print(f'{status}: {path}')
    return path


def _weekend(start_at):
    d = datetime.fromisoformat(start_at)
    sat = d.toordinal() - (d.weekday() - 5) % 7 if d.weekday() >= 5 else d.toordinal()
    return datetime.fromordinal(sat).strftime('%Y/%m/%d週')


def report(directory=SETTLEMENT_DIR):
    """レース別・週末別・累計の表（Markdown）を返す。記録が精算後に変わっていれば警告する。"""
    items = sorted((engine._read_json(p) for p in directory.glob('*.json')), key=lambda s: s['start_at'])
    lines = ['# 仮想収支（公式払戻で精算）', '',
             '回収率 = 払戻 ÷（提案額 − 返還）。購入は行っていない。混合率・重みはこの結果だけで変えない。', '']
    if not items:
        return '\n'.join(lines + ['精算済みのレースはまだない。'])
    warnings = []
    for s in items:
        try:
            if decision_hash(engine._read_json(s['record'])) != s['record_hash']:
                warnings.append(f"- {s['race']}: 記録の判断部分が精算後に変更されている")
        except OSError:
            warnings.append(f"- {s['race']}: 記録ファイルが見つからない（{s['record']}）")
    plan_names = list(dict.fromkeys(n for s in items for n in s['plans']))

    def fmt(v):
        return '—' if v['roi'] is None else f"{v['roi'] * 100:.1f}%"

    def row(label, v):
        return f"| {label} | {v['stake']:,} | {v['refund']:,} | {v['payout']:,} | {v['profit']:+,} | {fmt(v)} |"
    header = ['| 区分 | 提案額 | 返還 | 払戻 | 収支 | 回収率 |', '|---|---:|---:|---:|---:|---:|']
    for name in plan_names:
        lines += [f'## {name}', '', '### レース別', ''] + header
        weeks, total = {}, [0, 0, 0]
        for s in items:
            v = s['plans'].get(name)
            if not v:
                continue
            finish = '-'.join(map(str, s['official']['finish'][:3]))
            lines.append(row(f"{s['race']}（{finish}）", v))
            w = weeks.setdefault(_weekend(s['start_at']), [0, 0, 0])
            for acc in (w, total):
                acc[0] += v['stake']
                acc[1] += v['refund']
                acc[2] += v['payout']
        lines += ['', '### 週末別', ''] + header
        lines += [row(k, _summary(*w)) for k, w in weeks.items()]
        lines += ['', '### 累計', ''] + header + [row(f'{len(items)}レース', _summary(*total)), '']
    if warnings:
        lines += ['## 警告', ''] + warnings
    return '\n'.join(lines)


def main(argv):
    if argv[:1] == ['fetch']:
        for p in argv[1:]:
            fetch(p)
    elif argv[:1] == ['report']:
        text = report()
        if len(argv) > 1:
            Path(argv[1]).write_text(text + '\n', encoding='utf-8')
        print(text)
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
