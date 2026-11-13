"""重賞の買い目を機械的に算出するエンジン。ルールは keiba/RULES.md を参照。

使い方: python -m keiba.engine keiba/records/20261004_スプリンターズS.json
"""
import json
import math
import sys
from itertools import combinations, permutations

RACE_CAP = 4000
UNIT = 100
MAX_TICKETS = 15
EV_THRESHOLD = 1.2
SOFTMAX_TEMP = 8.0
MODEL_WEIGHT = 0.5
TAKEOUT_WIN = 0.20
BIAS_FLAT = 0.08
ORDER_RATIO = 2.0

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


def harville(p):
    """勝率から複勝・ワイド・馬連・馬単・三連複・三連単の的中確率を算出する。"""
    place = {k: 0.0 for k in p}
    wide, umaren, umatan, trio, trifecta = {}, {}, {}, {}, {}
    for a, b, c in permutations(p, 3):
        pab = p[a] * p[b] / (1 - p[a])
        pabc = pab * p[c] / (1 - p[a] - p[b])
        trifecta[(a, b, c)] = pabc
        for k in (a, b, c):
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


def _bet(kind, horses, prob, o):
    return {'type': kind, 'horses': list(horses), 'prob': prob, 'odds': o, 'ev': prob * o,
            'kelly': (prob * o - 1) / (o - 1) if o > 1 else -1.0}


def pair_bets(p, odds, probs, gates):
    """同じ2頭の組では馬連・枠連・馬単のうち期待値が最大の1つだけを候補にする。

    枠連は枠連オッズが馬連オッズより高い場合だけ、馬単は着順イメージが明確
    （1着側の勝率が2着側の ORDER_RATIO 倍以上）な場合だけ比較対象に入れる。
    """
    waku = wakuren_probabilities(probs['umaren'], gates) if gates else {}
    waku_odds = {_key(k): o for k, o in odds.get('wakuren', {}).items()}
    umatan_odds = {_ordered(k): o for k, o in odds.get('umatan', {}).items()}
    bets = []
    for k, o in odds.get('umaren', {}).items():
        pair = _key(k)
        options = [_bet('馬連', pair, probs['umaren'].get(pair, 0.0), o)]
        if gates:
            gp = tuple(sorted((gates[pair[0]], gates[pair[1]])))
            if gp in waku_odds and waku_odds[gp] > o:
                bet = _bet('枠連', gp, waku.get(gp, 0.0), waku_odds[gp])
                options.append(bet)
        for order in (pair, pair[::-1]):
            if order in umatan_odds and p[order[0]] >= ORDER_RATIO * p[order[1]]:
                options.append(_bet('馬単', order, probs['umatan'][order], umatan_odds[order]))
        bets.append(max(options, key=lambda b: b['ev']))
    return bets


def candidate_bets(p, odds, gates=None):
    probs = harville(p)
    bets = []
    for k, o in odds.get('win', {}).items():
        bets.append(_bet('単勝', (int(k),), p[int(k)], o))
    for k, o in odds.get('place', {}).items():
        bets.append(_bet('複勝', (int(k),), probs['place'][int(k)], _low(o)))
    for k, o in odds.get('wide', {}).items():
        bets.append(_bet('ワイド', _key(k), probs['wide'].get(_key(k), 0.0), _low(o)))
    bets += pair_bets(p, odds, probs, gates)
    for k, o in odds.get('sanrenpuku', {}).items():
        bets.append(_bet('三連複', _key(k), probs['trio'].get(_key(k), 0.0), o))
    for k, o in odds.get('sanrentan', {}).items():
        bets.append(_bet('三連単', _ordered(k), probs['trifecta'].get(_ordered(k), 0.0), o))
    # 枠連は複数の馬連の組から同じ枠の組が出うるので重複を除く
    seen, unique = set(), []
    for b in bets:
        key = (b['type'], tuple(b['horses']))
        if key not in seen and b['odds'] > 1 and b['ev'] >= EV_THRESHOLD:
            seen.add(key)
            unique.append(b)
    unique.sort(key=lambda b: b['kelly'], reverse=True)
    return unique[:MAX_TICKETS]


def allocate(bets, cap=RACE_CAP):
    """ケリー比率に比例して100円単位で配分する。合計はcapを超えない。"""
    if not bets:
        return []
    total_f = sum(b['kelly'] for b in bets)
    budget = int(cap * min(1.0, total_f * 4) // UNIT * UNIT)
    for b in bets:
        b['stake'] = int(budget * b['kelly'] / total_f // UNIT * UNIT)
    kept = [b for b in bets if b['stake'] > 0]
    rest = budget - sum(b['stake'] for b in kept)
    for b in kept:
        if rest < UNIT:
            break
        b['stake'] += UNIT
        rest -= UNIT
    return kept


def analyze(race):
    horses = race['horses']
    if 'bias' in race and 'results' in race['bias']:
        inner_outer, front_back = track_bias(race['bias']['results'])
    else:
        bias = race.get('bias', {})
        inner_outer, front_back = bias.get('inner_outer', 'flat'), bias.get('front_back', 'flat')
    pace = race.get('pace') or predict_pace(horses)
    scores = {h['num']: total_score(h, pace, inner_outer, front_back) for h in horses}
    p = win_probabilities(scores, {int(k): v for k, v in race['odds']['win'].items()})
    gates = {h['num']: h['gate'] for h in horses} if all('gate' in h for h in horses) else None
    bets = allocate(candidate_bets(p, race['odds'], gates))
    return {
        'race': race['race'],
        'bias': {'inner_outer': inner_outer, 'front_back': front_back},
        'pace': pace,
        'scores': scores,
        'win_prob': {k: round(v, 4) for k, v in p.items()},
        'bets': bets,
        'total_stake': sum(b['stake'] for b in bets),
    }


def format_result(res):
    lines = [f"■ {res['race']}  ペース:{res['pace']}  バイアス:{res['bias']['inner_outer']}/{res['bias']['front_back']}"]
    if not res['bets']:
        lines.append('見送り（期待値1.2以上の馬券なし）')
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


def main(path):
    with open(path, encoding='utf-8') as f:
        race = json.load(f)
    res = analyze(race)
    race['result'] = res
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(race, f, ensure_ascii=False, indent=2)
    print(format_result(res))


if __name__ == '__main__':
    main(sys.argv[1])
