"""重賞の買い目を機械的に算出するエンジン。ルールは keiba/RULES.md を参照。

使い方: python3 -m keiba.engine keiba/records/YYYYMMDD_レース名.json [keiba/records/ledger_YYYYMMDD.json]
（最終提案専用。実取得オッズと取得時刻の検査を通らなければ止まる。前日の参考計算は analyze() を直接使う）
"""
import fcntl
import json
import math
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from itertools import combinations, permutations

RACE_CAP = 4000
WEEKEND_CAP = 8000
UNIT = 100
MAX_TICKETS = 15
# 券種グループごとの点数上限。馬連・馬単は合わせて1レース5点まで（組ごとの小さな確率に頼る買い目を広げすぎない）
TYPE_LIMITS = {'馬連・馬単': (('馬連', '馬単'), 5)}
# 暫定（2026/10/4〜）: 判断時の単勝がこの倍率以上の馬を含む買い目は買わない。過去5年の重賞で100倍以上は
# 勝率0.2%（市場0.5%）・単勝回収率30%と市場以上に来ないため。オッズ帯ごとの補正を入れたら外す
LONGSHOT_ODDS = 100
EV_THRESHOLD = 1.2
SOFTMAX_TEMP = 8.0
# 着順分布でのモデルの重み。モデル（Harville）と市場（三連単オッズ）の上位3頭の着順確率を
# 対数空間でこの比率で混ぜ、1つの分布にする。1.0 でモデルのみ。未検証
MODEL_WEIGHT = 0.5
TAKEOUT_WIN = 0.20
BIAS_FLAT = 0.08
# 資金配分の基準資金（期待対数成長の計算用）。Codex版 config.json と同じ値で、未検証
REFERENCE_CAPITAL = 80000
# 最終提案に使えるオッズ: 発走10分前（判断時刻）以前、かつ判断時刻の5分前以降に取得したもの
DECISION_BEFORE = timedelta(minutes=10)
ODDS_WINDOW = timedelta(minutes=5)
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
    'jockey_downgrade': -4,
    'temperament': -3,
}

PLUS_WEIGHTS = {
    'first_after_gelding': 3,
    'first_after_transfer': 3,
    'weight_drop': 3,
    'top_jockey_switch': 4,
    'jockey_switch': 1,
    'first_surface': 2,
    'big_distance_change': 2,
    'distance_change_try': 1,
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
# ハナ候補の頭数ごとのペース確率（2026/10/5〜）。1つのペースに決め打ちせず、位置取り適性をこの確率で平均する。
# 10/4 の毎日王冠・京都大賞典で「逃げ候補3頭以上→ハイ」が2レースとも外れた（実際はスローと平均ペース）ための変更。
# 値は仮の設定で未検証。過去の重賞の通過順・ラップで校正する
PACE_PRIOR = {
    0: {'slow': 0.50, 'middle': 0.40, 'high': 0.10},
    1: {'slow': 0.45, 'middle': 0.40, 'high': 0.15},
    2: {'slow': 0.25, 'middle': 0.50, 'high': 0.25},
    3: {'slow': 0.15, 'middle': 0.40, 'high': 0.45},
}
# 乖離チェック（2026/10/5〜、表示だけで買い目は変えない）: 市場前のモデル勝率が、単勝市場と他の出どころ（Codex版など）
# の全てのこの倍率以上なら印を付ける。期待値がこの値を超える買い目も見直し対象として表示する
DIVERGENCE_RATIO = 1.5
EV_SUSPICIOUS = 2.0
FRONTNESS = {'逃げ': 1.0, '先行': 0.5, '差し': -0.5, '追込': -1.0}
ZONE = {'内': -1, '中': 0, '外': 1}


def select_races(results, n=2):
    """重賞がn個を超える週に、本命にするnレースを選ぶ。

    results は各レースの analyze() の結果（その時点のオッズで計算したもの）。提案配分の期待対数成長
    （期待値・配分額・同時損失を含む）が大きい順。成長が0のレース（見送り）は本命にしない。
    確率の推定誤差はまだ評価に入っていない。
    """
    ranked = sorted((r for r in results if r.get('growth', 0) > 0), key=lambda r: r['growth'], reverse=True)
    return ranked[:n]


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
    """過去5走の1角通過順で1番手（ハナ）が2回以上ならハナ候補。

    以前は「1〜2番手が2回以上」だったが、番手でも運べる先行馬まで数えてペースを速く見積もりすぎた（10/4の2重賞）。
    """
    return sum(1 for p in early_positions[-5:] if p == 1) >= 2


def predict_pace(horses):
    """ペースの確率 {'slow','middle','high'}。ハナ候補の頭数から PACE_PRIOR で決める。"""
    n = sum(1 for h in horses if is_front_candidate(h.get('early_positions', [])))
    return dict(PACE_PRIOR[min(n, 3)])


def pace_probs(pace):
    """レコードの pace（'slow' などの文字列、または確率の dict）を確率の dict にそろえる。"""
    if isinstance(pace, str):
        return {pace: 1.0}
    if set(pace) - set(PACE_FIT) or any(v < 0 for v in pace.values()) or abs(sum(pace.values()) - 1) > 1e-6:
        raise ValueError(f"pace の確率が不正: {pace}")
    return dict(pace)


def pace_label(pace):
    """表示用に最も確率の高いペース。"""
    p = pace_probs(pace)
    return max(PACE_FIT, key=lambda k: p.get(k, 0))


def pace_fit(style, pace):
    """位置取り適性。ペースの確率で PACE_FIT を平均する。"""
    return sum(w * PACE_FIT[k][style] for k, w in pace_probs(pace).items())


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
            + pace_fit(horse['style'], pace)
            + bias_fit(horse, inner_outer, front_back)
            + sum(MINUS_WEIGHTS[k] for k in horse.get('minus', []))
            + sum(PLUS_WEIGHTS[k] for k in horse.get('plus', [])))


def _normalize(d):
    s = sum(d.values())
    return {k: v / s for k, v in d.items()}


def model_probabilities(scores):
    """総合点をソフトマックス（温度 SOFTMAX_TEMP）で勝率にする。市場は混ぜない。"""
    top = max(scores.values())
    return _normalize({k: math.exp((s - top) / SOFTMAX_TEMP) for k, s in scores.items()})


def win_probabilities(scores, win_odds):
    """モデル勝率と市場勝率を対数空間でブレンドした勝率（参考用。買い目の計算は order_distribution を使う）。"""
    model = model_probabilities(scores)
    market = _normalize({k: 1 / win_odds[k] for k in scores})
    blended = {k: math.exp(MODEL_WEIGHT * math.log(model[k]) + (1 - MODEL_WEIGHT) * math.log(market[k]))
               for k in scores}
    return _normalize(blended)


def order_distribution(scores, odds, model_weight=MODEL_WEIGHT, market='auto'):
    """上位3頭の着順（1着,2着,3着）ごとの確率。全券種の的中確率と資金配分はこの1つの分布から出す。

    モデル側はモデル勝率の Harville 式。市場側は三連単オッズ（出走馬の全組合せが揃っている場合）の
    逆数を正規化したもの、揃っていなければ単勝オッズの Harville 式。両者を対数空間で
    model_weight : 1-model_weight に混ぜて正規化する。0.5 なら着順ごとの幾何平均 √(モデル×市場) を正規化したもので、
    市場がほぼ否定した着順は混ぜた後も小さくなる（算術平均のようにモデル確率の半分が残るわけではない）。
    market='win' なら三連単があっても単勝の Harville を使う（旧方式相当。検証用の参考試算に使う）。
    戻り値 ({着順: 確率}, 市場側の出どころ, 三連単オッズが欠けていた着順の数)。
    """
    nums = list(scores)
    model = harville(model_probabilities(scores))['trifecta']
    missing = sanrentan_missing(odds, nums)
    if market == 'auto' and not missing:
        tri = {_ordered(k): _low(v) for k, v in odds['sanrentan'].items()}
        market, source = _normalize({o: 1 / tri[o] for o in model}), 'sanrentan'
    else:
        win = _normalize({k: 1 / _low(odds['win'][str(k)]) for k in nums})
        market, source = harville(win)['trifecta'], 'win_harville'
    if model_weight >= 1:
        return model, 'model_only', missing
    pooled = {o: math.exp(model_weight * math.log(model[o]) + (1 - model_weight) * math.log(market[o]))
              for o in model}
    return _normalize(pooled), source, missing


def sanrentan_missing(odds, nums):
    """出走馬（nums）の三連単の全組合せのうち、有効なオッズがない組の数。"""
    tri = {_ordered(k): _low(v) for k, v in odds.get('sanrentan', {}).items()}
    return sum(1 for o in permutations(nums, 3) if not (o in tri and tri[o] > 1))


def market_label(res):
    total = res.get('sanrentan_total', 0)
    if res.get('market_source') == 'sanrentan':
        return '市場: 三連単オッズ使用'
    if res.get('market_source') == 'model_only':
        return '市場: 混合なし（モデルのみ）'
    return f"市場: 単勝オッズで代用（三連単 不足 {res.get('sanrentan_missing', 0)}/{total}組）"


def ticket_probabilities(orders, places, gates=None):
    """着順分布から各券種の的中確率を集計する。"""
    out = {k: {} for k in ('win', 'place', 'wide', 'umaren', 'umatan', 'trio', 'trifecta', 'wakuren')}

    def add(kind, key, q):
        out[kind][key] = out[kind].get(key, 0.0) + q
    for (a, b, c), q in orders.items():
        add('win', a, q)
        for k in (a, b, c)[:places]:
            add('place', k, q)
        for pair in combinations(sorted((a, b, c)), 2):
            add('wide', pair, q)
        add('umaren', tuple(sorted((a, b))), q)
        add('umatan', (a, b), q)
        add('trio', tuple(sorted((a, b, c))), q)
        add('trifecta', (a, b, c), q)
        if gates:
            add('wakuren', tuple(sorted((gates[a], gates[b]))), q)
    return out


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


def _bet(kind, horses, prob, o):
    return {'type': kind, 'horses': list(horses), 'prob': prob, 'odds': o, 'ev': prob * o,
            'kelly': (prob * o - 1) / (o - 1) if o > 1 else -1.0}


def candidate_bets(orders, odds, gates=None, places=3):
    """着順分布 orders から8券種を独立に評価し、期待値が閾値以上の買い目を返す（券種間の事前除外はしない）。"""
    probs = ticket_probabilities(orders, places, gates)
    table = [
        ('単勝', 'win', lambda k: (int(k),), lambda t: probs['win'].get(t[0], 0.0)),
        ('複勝', 'place', lambda k: (int(k),), lambda t: probs['place'].get(t[0], 0.0)),
        ('ワイド', 'wide', _key, lambda t: probs['wide'].get(t, 0.0)),
        ('馬連', 'umaren', _key, lambda t: probs['umaren'].get(t, 0.0)),
        ('枠連', 'wakuren', _key, lambda t: probs['wakuren'].get(t, 0.0)),
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
            b = _bet(label, parse(k), prob(parse(k)), o)
            if b['odds'] > 1 and b['ev'] >= EV_THRESHOLD:
                bets.append(b)
    return bets


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


def allocate(bets, orders, cap=RACE_CAP, gates=None, places=3, capital=REFERENCE_CAPITAL, type_limits=TYPE_LIMITS):
    """買い目同士の同時的中・同時不的中を含めて、期待対数成長が最も増える買い目へ100円ずつ積む。

    着順（上位3頭）ごとに全買い目の払戻を合算して評価するので、同じ馬に偏った買い目の
    同時損失が配分に反映される。cap と MAX_TICKETS 点・TYPE_LIMITS（馬連・馬単は合わせて5点）の範囲で、増分がなくなったら止める。
    確率は着順分布 orders（買い目の期待値と同じ分布）、払戻は実オッズ。
    戻り値は (配分済みの買い目, 見送り理由, 期待対数成長)。
    """
    if not bets:
        return [], 'no_value', 0.0
    if cap < UNIT:
        return [], 'no_budget', 0.0
    prob = list(orders.values())
    orders = list(orders)
    hits = [_hits(b, orders, gates, places) for b in bets]
    wealth = [float(capital)] * len(orders)
    stakes = [0] * len(bets)
    spent = 0
    while spent + UNIT <= cap:
        if min(wealth) <= UNIT:
            break
        base = math.fsum(q * (math.log(w - UNIT) - math.log(w)) for q, w in zip(prob, wealth))
        n_used = sum(1 for x in stakes if x)
        group_used = {g: sum(1 for b, x in zip(bets, stakes) if x and b['type'] in types)
                      for g, (types, _) in (type_limits or {}).items()}
        best, best_gain = None, 0.0
        for j, b in enumerate(bets):
            if not stakes[j] and (n_used >= MAX_TICKETS or any(
                    b['type'] in types and group_used[g] >= limit for g, (types, limit) in (type_limits or {}).items())):
                continue
            ret = UNIT * b['odds']
            gain = base + math.fsum(prob[i] * (math.log(wealth[i] - UNIT + ret) - math.log(wealth[i] - UNIT))
                                    for i in hits[j])
            if gain > best_gain:
                best, best_gain = j, gain
        if best is None:
            break
        stakes[best] += UNIT
        spent += UNIT
        ret = UNIT * bets[best]['odds']
        wealth = [w - UNIT for w in wealth]
        for i in hits[best]:
            wealth[i] += ret
    kept = []
    for b, x in zip(bets, stakes):
        if x:
            kept.append(dict(b, stake=x))
    kept.sort(key=lambda b: (-b['stake'], -b['ev']))
    growth = math.fsum(q * math.log(w / capital) for q, w in zip(prob, wealth))
    return kept, (None if kept else 'no_unit_allocation'), growth


SKIP_REASONS = {
    'no_value': '期待値1.2以上の馬券なし',
    'no_unit_allocation': '期待値1.2以上の候補はあるが、100円単位では期待対数成長が増えない',
    'no_budget': '土日の残り予算なし（先のレースへの予約・使用済み）',
}


def involves_longshot(bet, win_odds, horses):
    """買い目が単勝 LONGSHOT_ODDS 倍以上の馬に頼るか。枠連は、その枠の出走馬が全て大穴なら頼るとみなす。"""
    longshot = {n for n, o in win_odds.items() if o >= LONGSHOT_ODDS}
    if bet['type'] == '枠連':
        frames = {}
        for h in horses:
            frames.setdefault(h['gate'], []).append(h['num'])
        return any(frames.get(g) and all(n in longshot for n in frames[g]) for g in bet['horses'])
    return any(n in longshot for n in bet['horses'])


def bets_outcome(bets, orders, gates=None, places=3):
    """着順分布 orders のもとでの買い目全体の（回収期待＝期待払戻÷提案額, 全損の確率）。"""
    total = sum(b['stake'] for b in bets)
    if not total:
        return None
    keys = list(orders)
    pay = [0.0] * len(keys)
    for b in bets:
        for i in _hits(b, orders, gates, places):
            pay[i] += b['stake'] * b['odds']
    expected = sum(orders[k] * v for k, v in zip(keys, pay)) / total
    loss = sum(orders[k] for k, v in zip(keys, pay) if v == 0)
    return {'return': expected, 'loss': loss}


def pace_sensitivity(bets, horses, inner_outer, front_back, odds, model_weight, market, gates, places):
    """各ペースに決め打ちした場合の、決めた買い目の回収期待と全損の確率（展開を読み違えたときの損失の目安）。"""
    out = {}
    for pace in PACE_FIT:
        scores = {h['num']: total_score(h, pace, inner_outer, front_back) for h in horses}
        orders, _, _ = order_distribution(scores, odds, model_weight, market)
        out[pace] = bets_outcome(bets, orders, gates, places)
    return out


def divergence_warnings(model, market, bets, others=None):
    """モデルと市場・他の出どころとの大きなずれ（表示用）。

    model・market・others の各値は {馬番: 勝率}。買い目に含まれる馬のうち、モデル勝率が市場と others の全ての
    DIVERGENCE_RATIO 倍以上の馬（その馬を含む買い目の金額の割合つき、割合の大きい順）と、期待値が EV_SUSPICIOUS を
    超える買い目を返す。期待値が極端に高いときは市場の見落としよりモデル側の誤りを先に疑う。
    """
    sources = {'市場': market, **(others or {})}
    total = sum(b.get('stake', 0) for b in bets)
    horses = []
    for n in {h for b in bets for h in b['horses']}:
        p = model.get(n, 0)
        ratios = {name: p / q[n] for name, q in sources.items() if q.get(n, 0) > 0}
        if len(ratios) == len(sources) and min(ratios.values()) >= DIVERGENCE_RATIO:
            share = sum(b.get('stake', 0) for b in bets if n in b['horses']) / total if total else 0.0
            horses.append({'num': n, 'model': p, 'ratios': ratios, 'stake_share': share})
    horses.sort(key=lambda h: (-h['stake_share'], -h['model']))
    tickets = [{'type': b['type'], 'horses': b['horses'], 'ev': b['ev']} for b in bets if b['ev'] > EV_SUSPICIOUS]
    return {'horses': horses, 'tickets': tickets}


def analyze(race, cap=RACE_CAP, model_weight=MODEL_WEIGHT, market='auto', others=None):
    """race['scratched'] の馬番は出走取消として除く。複勝の範囲は発売開始時の頭数で決める。
    others（{出どころ: {馬番: 勝率}}、Codex版など）は乖離チェックの表示だけに使い、買い目には使わない。"""
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
    odds = {k: {kk: vv for kk, vv in v.items() if all(int(x) not in scratched for x in re.split('[->]', kk))}
            if k != 'wakuren' else v for k, v in race['odds'].items()}
    gates = {h['num']: h['gate'] for h in horses} if all('gate' in h for h in horses) else None
    orders, market_source, tri_missing = order_distribution(scores, odds, model_weight, market)
    cands = [b for b in candidate_bets(orders, odds, gates, places)
             if not involves_longshot(b, win_odds, horses)]
    bets, skip, growth = allocate(cands, orders, cap, gates, places)
    win = ticket_probabilities(orders, places)['win']
    market_win = _normalize({k: 1 / _low(win_odds[k]) for k in scores})
    return {
        'race': race['race'],
        'bias': {'inner_outer': inner_outer, 'front_back': front_back},
        'pace': pace_label(pace),
        'pace_probs': pace_probs(pace),
        'place_slots': places,
        'cap': cap,
        'model_weight': model_weight,
        'market_source': market_source,
        'sanrentan_missing': tri_missing,
        'sanrentan_total': len(scores) * (len(scores) - 1) * (len(scores) - 2),
        'scores': scores,
        'win_prob': {k: win.get(k, 0.0) for k in scores},
        'bets': bets,
        'total_stake': sum(b['stake'] for b in bets),
        'growth': growth,
        'skip_reason': skip,
        'warnings': divergence_warnings(model_probabilities(scores), market_win, bets, others),
        'pace_sensitivity': pace_sensitivity(bets, horses, inner_outer, front_back, odds, model_weight, market,
                                             gates, places) if bets else None,
    }


PACE_JA = {'slow': 'スロー', 'middle': 'ミドル', 'high': 'ハイ'}
SOURCE_LABELS = {'codex': 'Codex', 'codex_reference': 'Codex形式の参考'}


def format_warnings(res):
    w = res.get('warnings') or {}
    lines = []
    for h in w.get('horses', []):
        ratios = '・'.join(f"{SOURCE_LABELS.get(name, name)}の{r:.1f}倍" for name, r in h['ratios'].items())
        lines.append(f"⚠ 乖離 {h['num']}番 モデル勝率{h['model']:.1%}（{ratios}）、買い目の{h['stake_share']:.0%}がこの馬絡み。"
                     "評価の根拠を見直す")
    for b in w.get('tickets', []):
        lines.append(f"⚠ 期待値{b['ev']:.2f} {b['type']} {'-'.join(map(str, b['horses']))}。市場よりモデルの誤りを先に疑う")
    return lines


def format_result(res):
    probs = res.get('pace_probs') or {res['pace']: 1.0}
    pace = '/'.join(f"{k}{v:.0%}" for k, v in probs.items() if v > 0)
    lines = [f"■ {res['race']}  ペース:{pace}  バイアス:{res['bias']['inner_outer']}/{res['bias']['front_back']}"
             f"  上限:{res.get('cap', RACE_CAP)}円  {market_label(res)}"]
    lines += format_warnings(res)
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
    sens = res.get('pace_sensitivity')
    if sens:
        lines.append('展開別（そのペースに決め打ち）: ' + ' / '.join(
            f"{PACE_JA[k]} 回収期待{v['return']:.2f}・全損{v['loss']:.0%}" for k, v in sens.items()))
    w = res.get('warnings') or {}
    if w.get('horses') or w.get('tickets'):
        review = res.get('warning_review')
        lines.append(f"再点検: {review}" if review else '⚠ 警告に対する再点検の記録なし（レコードの warning_review に書く）')
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
    """最終提案に使えるか検査する。実取得オッズ（odds_meta）が判断時刻の前5分以内に揃っていること。"""
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


class AlreadyDecided(Exception):
    pass


def _write_json(path, obj):
    """一時ファイルに書いてから置き換える（途中で止まっても元のファイルは壊れない）。"""
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


@contextmanager
def ledger_lock(ledger_path):
    """同じ台帳を使う実行を1つに限る。別の実行中なら待たずに止まる。"""
    with open(f"{ledger_path}.lock", 'w') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('別の実行が同じ台帳を使用中のため止めた（二重実行しない）')
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def decision_state(race, ledger=None):
    """'open'（未判断）/ 'decided'（判断済み）/ 'recover'（レコードは確定済みだが台帳が未記録）。"""
    entry = None
    if ledger is not None:
        entry = next((e for e in ledger['races'] if e['race_id'] == race['race_id']), None)
        if entry is None:
            raise ValueError(f"台帳にないレース: {race['race_id']}")
    if entry is not None and entry.get('status') == 'decided':
        return 'decided'
    if race.get('decision'):
        return 'recover' if entry is not None else 'decided'
    return 'open'


def recover(race, ledger, ledger_path):
    """レコード保存後・台帳保存前に止まった場合、レコードの確定内容で台帳だけを補う（再計算しない）。"""
    d = race['decision']
    record_decision(ledger, race['race_id'], d['stake'], d['decided_at'])
    _write_json(ledger_path, ledger)


def commit(race, path, ledger, ledger_path, stake, note=None):
    """確定内容をレコード→台帳の順に保存する。レコードの decision が確定の印。"""
    race['decision'] = {'stake': stake, 'decided_at': datetime.now().astimezone().isoformat(timespec='seconds')}
    if note:
        race['decision']['note'] = note
    _write_json(path, race)
    if ledger is not None:
        recover(race, ledger, ledger_path)


def decision_probabilities(race, res, extra=None):
    """判断時の勝率を出どころ別に残す（事後に精算で答え合わせし、混合率・温度を決めるため）。
    claude_model は市場を混ぜる前、market_win は単勝オッズの逆数の正規化、final は買い目の計算に使った分布の勝率。"""
    win_odds = {int(k): _low(v) for k, v in race['odds']['win'].items()}
    probs = {'claude_model': model_probabilities(res['scores']),
             'market_win': _normalize({k: 1 / win_odds[k] for k in res['scores']}),
             'final': res['win_prob']}
    for name, p in (extra or {}).items():
        probs[name] = p
    # 丸めずに保存する（丸めで小さな確率が0になると、答え合わせの損失が変わる）
    return {name: {str(k): v for k, v in p.items()} for name, p in probs.items()}


def finalize(race, path, ledger=None, ledger_path=None, extra_probs=None):
    """最終提案を計算して確定する。呼び出し側で decision_state が 'open' であることを確認しておく。
    extra_probs（{出どころ: {馬番: 勝率}}、Codex版など）は答え合わせ用に一緒に記録するだけで、買い目には使わない。"""
    validate_final(race)
    cap = weekend_cap(ledger, race['race_id']) if ledger is not None else RACE_CAP
    others = {name: {int(k): v for k, v in p.items()} for name, p in (extra_probs or {}).items()}
    res = analyze(race, cap, others=others)
    res['warning_review'] = race.get('warning_review')
    race['probabilities'] = decision_probabilities(race, res, extra_probs)
    # 検証用の参考試算（市場側を単勝の Harville だけにした旧方式相当）。台帳には入れず、後で公式払戻と照合する
    ref = analyze(race, cap, market='win')
    race['result'] = res
    race['reference'] = {'win_market_only': {k: ref[k] for k in ('bets', 'total_stake', 'growth', 'skip_reason')}}
    commit(race, path, ledger, ledger_path, res['total_stake'])
    return res


def guard(race, ledger, ledger_path):
    """判断済みなら AlreadyDecided。台帳だけ欠けていれば補ってから AlreadyDecided。"""
    state = decision_state(race, ledger)
    if state == 'recover':
        recover(race, ledger, ledger_path)
        raise AlreadyDecided(f"中断した確定を台帳に補った（{race['decision']['stake']}円）。再計算はしない")
    if state == 'decided':
        raise AlreadyDecided('判断済みのレース。オッズ・分析・提案は上書きしない')


def main(path, ledger_path=None):
    if ledger_path:
        with ledger_lock(ledger_path):
            race, ledger = _read_json(path), _read_json(ledger_path)
            try:
                guard(race, ledger, ledger_path)
            except AlreadyDecided as e:
                print(e)
                return 'already'
            res = finalize(race, path, ledger, ledger_path)
    else:
        race = _read_json(path)
        if decision_state(race) != 'open':
            print('判断済みのレース。オッズ・分析・提案は上書きしない')
            return 'already'
        res = finalize(race, path)
    print(format_result(res))
    return 'decided'


if __name__ == '__main__':
    main(*sys.argv[1:])
