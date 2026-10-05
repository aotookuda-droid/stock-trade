"""公式払戻による仮想精算。本番提案（result）と参考試算（reference）をそれぞれ精算して集計する。

使い方:
  python3 -m keiba.settlement fetch keiba/records/20261004_京都大賞典.json   # 公式払戻を取得して精算を保存
  python3 -m keiba.settlement report [出力.md]                               # レース別・週末別・累計を表示

- レース記録（判断時点の分析・オッズ・買い目）は読むだけで書き換えない。精算は keiba/settlements/<race_id>.json に保存する。
- 結果ページのレースIDが記録と違えば止まる。枠は結果ページの公式の枠を使い、記録の枠と違えば止まる。
- 記録にある全馬の結果行・枠・出走状態（着順／取消・除外・中止・失格）が揃わなければ「精算未完了」（記録側で補わない）。
  取消・除外は返還、競走中止・失格は不的中。
- 買った券種の払戻表が揃っていなければ「精算未完了」として止まる（不的中として0円にしない）。
- 精算時に記録全体（出走馬・枠・分析・根拠・展開・バイアス・オッズ・提案）のハッシュを保存し、集計時に記録が後から変わっていないか確認する。
- 取消・除外の馬を含む買い目は返還（枠連は、その枠の出走馬が足りなくなった場合だけ返還）。
- 回収率 = 払戻 ÷（提案額 − 返還）。購入・投票は行わない。
"""
import hashlib
import html
import json
import math
import re
import sys
import time
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


# 着順以外の出走状態。取消・除外は返還、競走中止・失格は不的中（返還しない）
STATUSES = ('取消', '除外', '中止', '失格')


class Incomplete(Exception):
    """払戻・着順の取得が不足していて精算できない（不的中とは区別する）。"""


def _text(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s)).strip()


def _key(nums, sep):
    nums = [int(n) for n in nums]
    if sep == '-':
        nums = sorted(nums)
    return str(nums[0]) if sep is None else sep.join(map(str, nums))


def parse_result(page):
    """結果ページから {'race_id', 'payouts': {券種: {組: 100円あたり払戻}}, 'finish': [馬番...], 'scratched': [馬番...],
    'gates': {馬番: 枠}}。"""
    canon = re.search(r'<link rel="canonical" href="[^"]*race_id=(\d{12})"', page)
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
    finish, scratched, gates, status = [], [], {}, {}
    for row in re.findall(r'<tr[^>]*class="[^"]*HorseList[^"]*"[^>]*>(.*?)</tr>', page, re.S):
        num = re.search(r'<td class="Num Txt_C">(.*?)</td>', row, re.S)
        if not num or not _text(num.group(1)).isdigit():
            continue  # 馬番の読めない行は記録の馬と結び付けられない（check_complete で欠落として扱う）
        num = int(_text(num.group(1)))
        rank = re.search(r'<td class="Result_Num">(.*?)</td>', row, re.S)
        rank = _text(rank.group(1)) if rank else ''
        waku = re.search(r'<td class="Num Waku(\d)">', row)
        if waku:
            gates[num] = int(waku.group(1))
        place = re.match(r'(\d+)', rank)
        if place:
            status[num] = '完走'  # 降着の表記（例: 4(降)）も着順の数字で扱う
            finish.append((int(place.group(1)), num))
        elif rank in STATUSES:
            status[num] = rank
            if rank in ('取消', '除外'):
                scratched.append(num)
        else:
            status[num] = '不明'
    return {'race_id': canon.group(1) if canon else None, 'payouts': payouts,
            'finish': [n for _, n in sorted(finish)], 'scratched': sorted(scratched), 'gates': gates,
            'status': status}


def check_complete(official, bets, race):
    """精算に必要な情報が揃っているか確認し、不足なら Incomplete。

    買った券種ごとに払戻表があること、複勝・ワイドは的中の組数が足りること（同着で増えるのは可）、着順が3頭以上あること。
    提案はその時点で発売されていた券種のオッズからだけ作るので、買った券種の払戻表がないのは未発売ではなく取得不足とみなす。
    """
    problems = []
    for h in race['horses']:
        n = h['num']
        if n not in official['status']:
            problems.append(f'{n}番の結果行がない')
        elif official['status'][n] == '不明':
            problems.append(f'{n}番の出走状態（着順・取消・除外・中止・失格）が読めない')
        if n in official['status'] and n not in official['gates']:
            problems.append(f'{n}番の枠がない')
    if len(official['finish']) < 3:
        problems.append(f"着順が{len(official['finish'])}頭分しかない")
    places = engine.place_slots(race.get('field_size_at_sales_start') or len(race['horses']))
    expected = {'複勝': places, 'ワイド': 3}
    for t in sorted({b['type'] for b in bets}):
        table = official['payouts'].get(t)
        if not table:
            problems.append(f'{t} の払戻表がない')
        elif len(table) < expected.get(t, 1):
            problems.append(f'{t} の払戻が{len(table)}組しかない（{expected.get(t, 1)}組以上のはず）')
    if problems:
        raise Incomplete('精算未完了: ' + '、'.join(problems))


def settle_bets(bets, official, gates):
    """買い目を精算する（払戻表が揃っていることは check_complete で確認済みとする）。
    各買い目の status は 的中 / 不的中 / 返還。戻り値 {'stake','refund','payout','profit','roi','tickets'}。"""
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
        status = '返還' if void else ('的中' if pay else '不的中')
        rows.append({'type': t, 'horses': hs, 'stake': x, 'refund': x if void else 0, 'payout': pay, 'status': status})
    return _summary(stake, refund, payout) | {'tickets': rows}


def _summary(stake, refund, payout):
    invested = stake - refund
    return {'stake': stake, 'refund': refund, 'payout': payout, 'profit': payout + refund - stake,
            'roi': payout / invested if invested else None}


def decision_hash(race):
    """記録全体のハッシュ。精算は記録を書き換えないので、判断後の変更（出走馬・枠・分析・根拠・展開・バイアス・
    オッズ・提案のどれでも）を検知できる。"""
    blob = json.dumps(race, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def settle(record_path, page, source, fetched_at=None):
    race = engine._read_json(record_path)
    if not race.get('decision'):
        raise SystemExit('判断前のレースは精算しない（decision がない）')
    official = parse_result(page)
    if official['race_id'] != race['race_id']:
        raise SystemExit(f"結果ページのレースID {official['race_id']} が記録の {race['race_id']} と違う。精算しない")
    plan_bets = {}
    for name, get in PLANS.items():
        try:
            plan_bets[name] = get(race)
        except (KeyError, TypeError):
            continue
    # 欠落は記録側で補わず精算未完了にする（取り直す）。揃った上で記録と食い違えば止める
    check_complete(official, [b for bets in plan_bets.values() for b in bets], race)
    recorded = {h['num']: h['gate'] for h in race['horses']}
    diff = {n: (g, official['gates'][n]) for n, g in recorded.items() if official['gates'][n] != g}
    extra = sorted(set(official['status']) - set(recorded))
    if diff or extra:
        raise SystemExit(f'結果ページの枠・馬番が記録と違う: {diff or extra}。精算しない')
    gates = official['gates']
    plans = {name: settle_bets(bets, official, gates) for name, bets in plan_bets.items()}
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
        new = json.loads(json.dumps(settlement['official'], ensure_ascii=False))  # 保存時と同じ形（キーは文字列）で比べる
        if old['official'] == new and old['record_hash'] == settlement['record_hash']:
            return path, 'unchanged'
        raise SystemExit(f'{path} は既にあり、内容が異なる。上書きしない（公式結果の訂正なら手で確認する）')
    engine._write_json(path, settlement)
    return path, 'saved'


def _get_page(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', 'replace')


def fetch(record_path, retries=2, pause=60, get=_get_page, sleep=time.sleep, directory=SETTLEMENT_DIR):
    """公式結果を取得して精算・保存する。取得不足なら pause 秒おきに retries 回まで取り直し、
    それでも揃わなければ精算せずに 'incomplete' を返す（不的中として記録しない）。"""
    race = engine._read_json(record_path)
    url = RESULT_URL.format(race['race_id'])
    for attempt in range(retries + 1):
        try:
            settlement = settle(record_path, get(url), url)
            break
        except Incomplete as e:
            print(f"{race['race']}: {e}")
            if attempt == retries:
                print('精算未完了のまま終了（何も保存していない）')
                return 'incomplete'
            sleep(pause)
    path, status = save(settlement, directory)
    print(f'{status}: {path}')
    return status


def _weekend(s):
    """集計の週。予算の単位と揃えるため、そのレースが載っている台帳（ledger_土曜日付.json）で決める。
    台帳が見つからなければ発走日から決め、土・日・月（3日間開催の祝日月曜）は直前の土曜にまとめる。"""
    for path in sorted(Path(s['record']).parent.glob('ledger_*.json')):
        try:
            if any(e.get('race_id') == s['race_id'] for e in engine._read_json(path)['races']):
                return datetime.strptime(path.stem[len('ledger_'):], '%Y%m%d').strftime('%Y/%m/%d週')
        except (OSError, ValueError, KeyError, TypeError):
            continue
    d = datetime.fromisoformat(s['start_at'])
    back = {5: 0, 6: 1, 0: 2}.get(d.weekday(), 0)
    return datetime.fromordinal(d.toordinal() - back).strftime('%Y/%m/%d週')


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
            w = weeks.setdefault(_weekend(s), [0, 0, 0])
            for acc in (w, total):
                acc[0] += v['stake']
                acc[1] += v['refund']
                acc[2] += v['payout']
        lines += ['', '### 週末別', ''] + header
        lines += [row(k, _summary(*w)) for k, w in weeks.items()]
        lines += ['', '### 累計', ''] + header + [row(f'{len(items)}レース', _summary(*total)), '']
    lines += probability_check(items)
    if warnings:
        lines += ['## 警告', ''] + warnings
    return '\n'.join(lines)


PROB_SOURCES = (('claude_model', 'Claude（市場前）'), ('codex', 'Codex（独立）'),
                ('codex_reference', 'Codex形式の参考'), ('market_win', '市場（単勝）'), ('final', '最終（買い目に使用）'))
CORE_SOURCES = ('claude_model', 'market_win', 'final')
PROB_BANDS = (0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 1.01)


def _probabilities(record):
    """判断時に記録した出どころ別の勝率。作成元が独立した Codex と記録されていない 'codex'（10/4 以前の記録など）は
    'codex_reference' として扱い、独立予測との比較に入れない（記録ファイル自体は変えない）。"""
    probs = dict(record.get('probabilities') or {})
    if 'codex' in probs and not (record.get('codex_meta') or {}).get('independent'):
        probs['codex_reference'] = probs.pop('codex')
    return probs


def _brier(p, winner):
    """多クラスの Brier スコア Σ(p − 勝ち=1/負け=0)²。小さいほど良い。"""
    return sum((v - (k == winner)) ** 2 for k, v in p.items()) + (0.0 if winner in p else 1.0)


def probability_check(items):
    """判断時に記録した出どころ別の勝率を、勝ち馬に付けた確率・対数損失（−log p）・Brier スコアで比べる。

    - 未記録（その出どころの記録が無い）と、0 と予測した（記録はあり確率0）を区別する。0 の馬が勝てば損失は無限大。
    - 平均は比較ごとに、比べる出どころが全て記録されたレースだけで取る（片側だけのレースを入れた優劣比較はしない）。
    - 独立した Codex と、Claude 作成など独立でない参考予測は別の比較にする。
    - 勝率帯ごとに、付けた勝率の平均と実際の勝率を並べる（較正の確認。数が少ないうちは参考）。
    """
    races = []
    for s in items:
        try:
            probs = _probabilities(engine._read_json(s['record']))
        except OSError:
            continue
        if probs:
            races.append((s, probs))
    if not races:
        return []
    rows, recorded = [], []
    for s, probs in races:
        winner = str(s['official']['finish'][0])
        got = {k: probs[k].get(winner, 0.0) if k in probs else None for k, _ in PROB_SOURCES}
        cells = ['未記録' if p is None else '0（損失∞）' if p == 0 else f'{p * 100:.1f}%' for p in got.values()]
        rows.append(f"| {s['race']}（{winner}番） | " + ' | '.join(cells) + ' |')
        recorded.append((got, {k: _brier(probs[k], winner) for k, _ in PROB_SOURCES if k in probs}))

    def avg_rows(keys, label):
        common = [r for r in recorded if all(r[0][k] is not None for k in keys)]
        ll, br = [], []
        for k, _ in PROB_SOURCES:
            losses = [math.inf if g[k] == 0 else -math.log(g[k]) for g, _ in common] if k in keys and common else []
            ll.append('—' if not losses else '∞' if math.inf in losses else f'{sum(losses) / len(losses):.3f}')
            briers = [b[k] for _, b in common] if k in keys and common else []
            br.append('—' if not briers else f'{sum(briers) / len(briers):.3f}')
        return [f'| 平均対数損失（{label}{len(common)}レース） | ' + ' | '.join(ll) + ' |',
                f'| 平均Brier（{label}{len(common)}レース） | ' + ' | '.join(br) + ' |']
    names = [n for _, n in PROB_SOURCES]
    out_rows = avg_rows(CORE_SOURCES, 'Claude・市場・最終が記録あり')
    if any(g['codex'] is not None for g, _ in recorded):
        out_rows += avg_rows(CORE_SOURCES + ('codex',), '独立Codexも記録あり')
    if any(g['codex_reference'] is not None for g, _ in recorded):
        out_rows += avg_rows(CORE_SOURCES + ('codex_reference',), '参考・独立比較ではない ')
    bands = ['', '### 勝率帯ごとの実績', '', '付けた勝率の帯ごとの頭数・付けた勝率の平均・実際に勝った割合。', '',
             '| 出どころ | ' + ' | '.join(f'{lo:.0%}〜{hi:.0%}' if hi <= 1 else f'{lo:.0%}〜' for lo, hi in
                                      zip(PROB_BANDS, PROB_BANDS[1:])) + ' |',
             '|---|' + '---:|' * (len(PROB_BANDS) - 1)]
    for k, name in PROB_SOURCES:
        acc = [[0, 0.0, 0] for _ in PROB_BANDS[1:]]
        for s, probs in races:
            if k not in probs:
                continue
            winner = str(s['official']['finish'][0])
            for h, p in probs[k].items():
                i = next(i for i, hi in enumerate(PROB_BANDS[1:]) if p < hi)
                acc[i][0] += 1
                acc[i][1] += p
                acc[i][2] += h == winner
        if any(a[0] for a in acc):
            bands.append(f'| {name} | ' + ' | '.join(f'{a[0]}頭 {a[1] / a[0]:.1%}→{a[2] / a[0]:.1%}' if a[0] else '—'
                                                   for a in acc) + ' |')
    return ['## 勝率の答え合わせ', '',
            '勝ち馬に付けていた勝率。「未記録」はその出どころの記録が無いレース、「0（損失∞）」は確率0と予測した馬が勝ったレース。',
            '平均（対数損失 −log p・Brier、どちらも小さいほど良い）は、比較ごとに比べる出どころが全て記録されたレースだけで計算する。',
            '「Codex形式の参考」は作成元が独立したCodexと記録されていない予測（10/4の2レースはClaudeがCodex形式で作成）で、'
            '独立モデルの比較には入れない。混合率・温度・加減点はレース数が十分たまるまで変えない。', '',
            '| レース（勝ち馬） | ' + ' | '.join(names) + ' |', '|---|' + '---:|' * len(names), *rows, *out_rows,
            *bands, '']


def main(argv):
    if argv[:1] == ['fetch']:
        results = [fetch(p) for p in argv[1:]]
        if 'incomplete' in results:
            raise SystemExit(1)
    elif argv[:1] == ['report']:
        text = report()
        if len(argv) > 1:
            Path(argv[1]).write_text(text + '\n', encoding='utf-8')
        print(text)
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
