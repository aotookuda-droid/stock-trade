"""重賞の買い目を機械的に算出するエンジン。ルールは keiba/RULES.md を参照。

使い方: python3 -m keiba.engine keiba/records/YYYYMMDD_レース名.json [keiba/records/ledger_YYYYMMDD.json]
（最終提案専用。実取得オッズと取得時刻の検査を通らなければ止まる。前日の参考計算は analyze() を直接使う）
"""
import json
import math
import re
import sys
from datetime import datetime, timedelta
from itertools import combinations, permutations

RACE_CAP = 4000
WEEKEND_CAP = 8000
UNIT = 100
MAX_TICKETS = 15
EV_THRESHOLD = 1.2
SOFTMAX_TEMP = 8.0
MODEL_WEIGHT = 0.5
TAKEOUT_WIN = 0.20
BIAS_FLAT = 0.08
# 資金配分の基準資金（期待対数成長の計算用）。Codex版 config.json と同じ値で、未検証
REFERENCE_CAPITAL = 80000
# 最終提案に使えるオッズ: 発走60分前（判断時刻）以前、かつ判断時刻の10分前以降に取得したもの
DECISION_BEFORE = timedelta(minutes=60)
ODDS_WINDOW = timedelta(minutes=10)
# 単勝以外の買い目の確率を、その買い目のオッズから逆算した市場確率（払戻率÷オッズ）と
# 幾何平均で混ぜる比率（モデル側の重み。1.0 で補正なし）。Codex版 raceday と同じ 0.5、未検証
TICKET_MODEL_WEIGHT = 0.5
PAYOUT_RATE = {'単勝': 0.80, '複勝': 0.80, '枠連': 0.775, '馬連': 0.775, 'ワイド': 0.775,
               '馬単': 0.75, '三連複': 0.75, '三連単': 0.725}
BET_TYPES = ('win', 'place', 'wakuren', 'umaren', 'wide', 'umatan', 'sanrenpuku', 'sanrentan')

MINUS_WEIGHTS = {
    'front_runner_bad_pace': -6,
    'long_layoff': -5,
    'wins_special_ground': -4,
    'turf_to_outer': -3,
    'dirt_to_inner': -3,
    'fillies_to_open': -3,
    'slow_start': -4,
    'hates_kickback': -4,
    'stable_concern': -4,
    'weight_change_15': -4,
    'weight_increase': -2,
    'lucky_last_race': -4,
    'lucky_trip': -2,
    'first_long_trip': -2,
    'bad_direction': -3,
    'old_no_record': -3,
}

PLUS_WEIGHTS = {
    'first_after_gelding': 3,
    'first_after_transfer': 3,
    'weight_drop': 3,
    'top_jockey_switch': 4,
    'jockey_switch': 1,
    'first_surface': 2,
    'big_distance_change': 2,
    'first_blinker': 3,
    'breaking_out': 4,
    'hill_23': 3,
    'hill_23_extra': 1,
    'short_interval_unfulfilled': 3,
    'final_work_strong': 3,
    'course_repeater': 3,
    'second_start': 2,
    'fastest_last_unrewarded': 3,
    'good_in_higher_class': 3,
    'turf_to_inner': 2,
    'dirt_to_outer': 2,
    'bloodline_ground': 2,
    'outside_farm': 2,
}

PACE_FIT = {
    'slow': {'逃げ': 4, '先行': 2, '差し': -2, '追込': -4},
    'middle': {'逃げ': 1, '先行': 1, '差し': 0, '追込': -1},
    'high': {'逃げ': -3, '先行': -1, '差し': 2, '追込': 3},
}
FRONTNESS = {'逃げ': 1.0, '先行': 0.5, '差し': -0.5, '追込': -1.0}
ZONE = {'内': -1, '中': 0, '外': 1}


def select_races(races, n=2):
    """重賞がn個を超える週に、妙味の出やすいn レースを選ぶ。"""
    def score(r):
        return r['field_size'] + (3 if r.get('handicap') else 0) + min(r['fav_odds'], 5) * 2
    return sorted(races, key=score, reverse=True)[:n]


def track_bias(results):
    """当日同馬場の3着以内馬 [{'num','field_size','corner4'}] から (内外, 前後) を返す。

    内外: 'inner' / 'outer' / 'flat'、前後: 'front' / 'closer' / 'flat'
    """
    if not results:
        return 'flat', 'flat'
    io = sum((r['num'] - 1) / (r['field_size'] - 1) - 0.5 for r in results) / len(results)
    fb = sum((r['corner4'] - 1) / (r['field_size'] - 1) - 0.5 for r in results) / len(results)

    def level(v, neg, pos):
        if v <= -BIAS_FLAT:
            return neg
        if v >= BIAS_FLAT:
            return pos
        return 'flat'
    return level(io, 'inner', 'outer'), level(fb, 'front', 'closer')


def is_front_candidate(early_positions):
    """過去5走の1角通過順で1〜2番手が2回以上なら逃げ候補。"""
    return sum(1 for p in early_positions[-5:] if p <= 2) >= 2


def predict_pace(horses):
    n = sum(1 for h in horses if is_front_candidate(h.get('early_positions', [])))
    if n <= 1:
        return 'slow'
    if n == 2:
        return 'middle'
    return 'high'


def bias_fit(horse, inner_outer, front_back):
    io = {'inner': -1, 'flat': 0, 'outer': 1}[inner_outer]
    fb = {'front': -1, 'flat': 0, 'closer': 1}[front_back]
    return io * ZONE[horse['gate_zone']] * 3 - fb * FRONTNESS[horse['style']] * 3


def total_score(horse, pace, inner_outer, front_back):
    for key in horse.get('minus', []):
        if key not in MINUS_WEIGHTS:
            raise ValueError(f"unknown minus factor: {key}")
    for key in horse.get('plus', []):
        if key not in PLUS_WEIGHTS:
            raise ValueError(f"unknown plus factor: {key}")
    return (horse['ability']
            + PACE_FIT[pace][horse['style']]
            + bias_fit(horse, inner_outer, front_back)
            + sum(MINUS_WEIGHTS[k] for k in horse.get('minus', []))
            + sum(PLUS_WEIGHTS[k] for k in horse.get('plus', [])))


def _normalize(d):
    s = sum(d.values())
    return {k: v / s for k, v in d.items()}


def win_probabilities(scores, win_odds):
    """モデル勝率と市場勝率を対数空間でブレンドした勝率。"""
    top = max(scores.values())
    model = _normalize({k: math.exp((s - top) / SOFTMAX_TEMP) for k, s in scores.items()})
    market = _normalize({k: 1 / win_odds[k] for k in scores})
    blended = {k: math.exp(MODEL_WEIGHT * math.log(model[k]) + (1 - MODEL_WEIGHT) * math.log(market[k]))
               for k in scores}
    return _normalize(blended)


def place_slots(field_size_at_sales_start):
    """複勝の的中範囲。発売開始時8頭以上は3着まで、5〜7頭は2着まで、4頭以下は発売なし（JRA）。

    発売後の取消で頭数が減っても、発売開始時の頭数で決まる。
    """
    if field_size_at_sales_start >= 8:
        return 3
    if field_size_at_sales_start >= 5:
        return 2
    return 0


def harville(p, places=3):
    """勝率から上位3頭の着順分布と、各券種の的中確率を算出する。

    places は複勝の的中範囲（place_slots の値）。ワイドは頭数によらず3着以内の2頭。
    """
    place = {k: 0.0 for k in p}
    wide, umaren, umatan, trio, trifecta = {}, {}, {}, {}, {}
    for a, b, c in permutations(p, 3):
        pab = p[a] * p[b] / (1 - p[a])
        pabc = pab * p[c] / (1 - p[a] - p[b])
        trifecta[(a, b, c)] = pabc
        for k in (a, b, c)[:places]:
            place[k] += pabc
        for pair in combinations(sorted((a, b, c)), 2):
            wide[pair] = wide.get(pair, 0.0) + pabc
        key = tuple(sorted((a, b, c)))
        trio[key] = trio.get(key, 0.0) + pabc
    for a, b in permutations(p, 2):
        umatan[(a, b)] = p[a] * p[b] / (1 - p[a])
        pair = tuple(sorted((a, b)))
        umaren[pair] = umaren.get(pair, 0.0) + umatan[(a, b)]
    return {'place': place, 'wide': wide, 'umaren': umaren, 'umatan': umatan,
            'trio': trio, 'trifecta': trifecta}


def wakuren_probabilities(umaren, gates):
    """馬連の確率を枠の組ごとに合算する。gates は {馬番: 枠番}。"""
    waku = {}
    for (a, b), prob in umaren.items():
        key = tuple(sorted((gates[a], gates[b])))
        waku[key] = waku.get(key, 0.0) + prob
    return waku


def _key(s):
    return tuple(sorted(int(x) for x in str(s).split('-')))


def _ordered(s):
    return tuple(int(x) for x in str(s).split('>'))


def _low(o):
    return o[0] if isinstance(o, (list, tuple)) else o


def _bet(kind, horses, prob, o, model_weight=TICKET_MODEL_WEIGHT):
    """prob はモデル（Harville）の的中確率。単勝以外は市場確率と混ぜた確率で期待値を出す。

    payout_odds は配分計算用の倍率で、モデルの着順分布のもとで期待値が補正後の値になるよう調整したもの。
    """
    model = prob
    if kind != '単勝' and model_weight < 1 and prob > 0:
        market = min(1.0, PAYOUT_RATE[kind] / o)
        prob = prob ** model_weight * market ** (1 - model_weight)
    return {'type': kind, 'horses': list(horses), 'prob': prob, 'model_prob': model, 'odds': o, 'ev': prob * o,
            'payout_odds': o * prob / model if model > 0 else 0.0,
            'kelly': (prob * o - 1) / (o - 1) if o > 1 else -1.0}


def candidate_bets(p, odds, gates=None, places=3, model_weight=TICKET_MODEL_WEIGHT):
    """8券種を独立に評価し、期待値が閾値以上の買い目を返す（券種間の制限はしない）。"""
    probs = harville(p, places)
    waku = wakuren_probabilities(probs['umaren'], gates) if gates else {}
    table = [
        ('単勝', 'win', lambda k: (int(k),), lambda t: p.get(t[0], 0.0)),
        ('複勝', 'place', lambda k: (int(k),), lambda t: probs['place'].get(t[0], 0.0)),
        ('ワイド', 'wide', _key, lambda t: probs['wide'].get(t, 0.0)),
        ('馬連', 'umaren', _key, lambda t: probs['umaren'].get(t, 0.0)),
        ('枠連', 'wakuren', _key, lambda t: waku.get(t, 0.0)),
        ('馬単', 'umatan', _ordered, lambda t: probs['umatan'].get(t, 0.0)),
        ('三連複', 'sanrenpuku', _key, lambda t: probs['trio'].get(t, 0.0)),
        ('三連単', 'sanrentan', _ordered, lambda t: probs['trifecta'].get(t, 0.0)),
    ]
    bets = []
    for label, kind, parse, prob in table:
        if kind == 'place' and places == 0:
            continue
        if kind == 'wakuren' and not gates:
            continue
        for k, o in odds.get(kind, {}).items():
            o = _low(o)
            b = _bet(label, parse(k), prob(parse(k)), o, model_weight)
            if b['odds'] > 1 and b['ev'] >= EV_THRESHOLD:
                bets.append(b)
    return bets, probs['trifecta']


def _hits(bet, orders, gates, places):
    """bet が的中する着順（上位3頭）の添字集合。"""
    t, h = bet['type'], tuple(bet['horses'])
    out = []
    for i, (a, b, c) in enumerate(orders):
        if t == '単勝':
            ok = h[0] == a
        elif t == '複勝':
            ok = h[0] in (a, b, c)[:places]
        elif t == 'ワイド':
            ok = set(h) <= {a, b, c}
        elif t == '馬連':
            ok = h == tuple(sorted((a, b)))
        elif t == '枠連':
            ok = h == tuple(sorted((gates[a], gates[b])))
        elif t == '馬単':
            ok = h == (a, b)
        elif t == '三連複':
            ok = h == tuple(sorted((a, b, c)))
        else:
            ok = h == (a, b, c)
        if ok:
            out.append(i)
    return out


def allocate(bets, trifecta, cap=RACE_CAP, gates=None, places=3, capital=REFERENCE_CAPITAL):
    """買い目同士の同時的中・同時不的中を含めて、期待対数成長が最も増える買い目へ100円ずつ積む。

    着順（上位3頭）ごとに全買い目の払戻を合算して評価するので、同じ馬に偏った買い目の
    同時損失が配分に反映される。cap と MAX_TICKETS 点の範囲で、増分がなくなったら止める。
    戻り値は (配分済みの買い目, 見送り理由)。
    """
    if not bets:
        return [], 'no_value'
    if cap < UNIT:
        return [], 'no_budget'
    orders = list(trifecta)
    prob = [trifecta[o] for o in orders]
    hits = [_hits(b, orders, gates, places) for b in bets]
    wealth = [float(capital)] * len(orders)
    stakes = [0] * len(bets)
    spent = 0
    while spent + UNIT <= cap:
        if min(wealth) <= UNIT:
            break
        base = math.fsum(q * (math.log(w - UNIT) - math.log(w)) for q, w in zip(prob, wealth))
        n_used = sum(1 for x in stakes if x)
        best, best_gain = None, 0.0
        for j, b in enumerate(bets):
            if not stakes[j] and n_used >= MAX_TICKETS:
                continue
            ret = UNIT * b['payout_odds']
            gain = base + math.fsum(prob[i] * (math.log(wealth[i] - UNIT + ret) - math.log(wealth[i] - UNIT))
                                    for i in hits[j])
            if gain > best_gain:
                best, best_gain = j, gain
        if best is None:
            break
        stakes[best] += UNIT
        spent += UNIT
        ret = UNIT * bets[best]['payout_odds']
        wealth = [w - UNIT for w in wealth]
        for i in hits[best]:
            wealth[i] += ret
    kept = []
    for b, x in zip(bets, stakes):
        if x:
            kept.append(dict(b, stake=x))
    kept.sort(key=lambda b: (-b['stake'], -b['ev']))
    return kept, (None if kept else 'no_unit_allocation')


SKIP_REASONS = {
    'no_value': '期待値1.2以上の馬券なし',
    'no_unit_allocation': '期待値1.2以上の候補はあるが、100円単位では期待対数成長が増えない',
    'no_budget': '土日の残り予算なし（先のレースへの予約・使用済み）',
}


def analyze(race, cap=RACE_CAP, model_weight=TICKET_MODEL_WEIGHT):
    """race['scratched'] の馬番は出走取消として除く。複勝の範囲は発売開始時の頭数で決める。"""
    scratched = set(race.get('scratched', []))
    horses = [h for h in race['horses'] if h['num'] not in scratched]
    sales_start = race.get('field_size_at_sales_start') or len(race['horses'])
    places = place_slots(sales_start)
    if 'bias' in race and 'results' in race['bias']:
        inner_outer, front_back = track_bias(race['bias']['results'])
    else:
        bias = race.get('bias', {})
        inner_outer, front_back = bias.get('inner_outer', 'flat'), bias.get('front_back', 'flat')
    pace = race.get('pace') or predict_pace(horses)
    scores = {h['num']: total_score(h, pace, inner_outer, front_back) for h in horses}
    win_odds = {int(k): v for k, v in race['odds']['win'].items()}
    missing = [n for n in scores if n not in win_odds]
    if missing:
        raise ValueError(f"単勝オッズがない馬（取消なら scratched に入れる）: {missing}")
    p = win_probabilities(scores, win_odds)
    gates = {h['num']: h['gate'] for h in horses} if all('gate' in h for h in horses) else None
    odds = {k: {kk: vv for kk, vv in v.items() if all(int(x) not in scratched for x in re.split('[->]', kk))}
            if k != 'wakuren' else v for k, v in race['odds'].items()}
    cands, trifecta = candidate_bets(p, odds, gates, places, model_weight)
    bets, skip = allocate(cands, trifecta, cap, gates, places)
    return {
        'race': race['race'],
        'bias': {'inner_outer': inner_outer, 'front_back': front_back},
        'pace': pace,
        'place_slots': places,
        'cap': cap,
        'scores': scores,
        'win_prob': {k: round(v, 4) for k, v in p.items()},
        'bets': bets,
        'total_stake': sum(b['stake'] for b in bets),
        'skip_reason': skip,
    }


def format_result(res):
    lines = [f"■ {res['race']}  ペース:{res['pace']}  バイアス:{res['bias']['inner_outer']}/{res['bias']['front_back']}"
             f"  上限:{res.get('cap', RACE_CAP)}円"]
    if not res['bets']:
        lines.append(f"見送り（{SKIP_REASONS.get(res.get('skip_reason'), '期待値1.2以上の馬券なし')}）")
        return '\n'.join(lines)
    for b in res['bets']:
        sep = '>' if b['type'] in ('馬単', '三連単') else '-'
        horses = sep.join(str(h) for h in b['horses'])
        if b['type'] == '枠連':
            horses = '枠' + horses
        lines.append(f"{b['type']:<4} {horses:<8} {b['stake']:>5}円  オッズ{b['odds']:>6.1f}  "
                     f"確率{b['prob']:.3f}  期待値{b['ev']:.2f}")
    lines.append(f"合計 {res['total_stake']}円")
    return '\n'.join(lines)


# ---- 土日の予算台帳 -------------------------------------------------------

def weekend_cap(ledger, race_id):
    """このレースに使える上限。土日8,000円から、使用済み額と、まだ判断していない
    本命レース（primary）への予約（各4,000円）を引いた残り。3レース目（primary=false）は残りだけを使う。
    """
    entries = {e['race_id']: e for e in ledger['races']}
    if race_id not in entries:
        raise ValueError(f"台帳にないレース: {race_id}")
    used = sum(e.get('stake', 0) for e in ledger['races'] if e.get('status') == 'decided')
    reserved = sum(RACE_CAP for e in ledger['races']
                   if e['race_id'] != race_id and e.get('primary') and e.get('status') != 'decided')
    remaining = ledger.get('cap', WEEKEND_CAP) - used - reserved
    return max(0, min(RACE_CAP, remaining // UNIT * UNIT))


def record_decision(ledger, race_id, stake, decided_at):
    for e in ledger['races']:
        if e['race_id'] == race_id:
            if e.get('status') == 'decided':
                raise ValueError(f"判断済みのレース: {race_id}")
            e.update(status='decided', stake=stake, decided_at=decided_at)
            return ledger
    raise ValueError(f"台帳にないレース: {race_id}")


# ---- 最終提案の入力検査 -----------------------------------------------------

def _dt(s):
    return datetime.fromisoformat(s)


def validate_final(race):
    """最終提案に使えるか検査する。実取得オッズ（odds_meta）が判断時刻の前10分以内に揃っていること。"""
    if not race.get('start_at'):
        raise ValueError('start_at（発走時刻）がない')
    decision = _dt(race['start_at']) - DECISION_BEFORE
    meta = race.get('odds_meta') or {}
    if meta.get('kind') != 'observed' or not meta.get('source'):
        raise ValueError('odds_meta に実取得オッズの記録（kind=observed・source）がない。予想オッズでは最終提案しない')
    fetched = meta.get('fetched_at') or {}
    for kind in race['odds']:
        if kind not in fetched:
            raise ValueError(f'{kind} の取得時刻がない')
        t = _dt(fetched[kind])
        if t > decision:
            raise ValueError(f'{kind} のオッズが判断時刻 {decision:%H:%M} より後に取得されている（{t:%H:%M:%S}）')
        if t < decision - ODDS_WINDOW:
            raise ValueError(f'{kind} のオッズが古い（{t:%H:%M:%S}、判断時刻の{ODDS_WINDOW.seconds // 60}分前より前）')
    if 'win' not in race['odds']:
        raise ValueError('単勝オッズがない')
    return decision


def main(path, ledger_path=None):
    with open(path, encoding='utf-8') as f:
        race = json.load(f)
    validate_final(race)
    cap = RACE_CAP
    ledger = None
    if ledger_path:
        with open(ledger_path, encoding='utf-8') as f:
            ledger = json.load(f)
        cap = weekend_cap(ledger, race['race_id'])
    res = analyze(race, cap)
    race['result'] = res
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(race, f, ensure_ascii=False, indent=2)
        f.write('\n')
    if ledger is not None:
        record_decision(ledger, race['race_id'], res['total_stake'], datetime.now().astimezone().isoformat(timespec='seconds'))
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
            f.write('\n')
    print(format_result(res))


if __name__ == '__main__':
    main(*sys.argv[1:])
