#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宝塚記念2026 馬券オプティマイザ / 期待値(EV)判定ツール
================================================================

`takarazuka_kinen_2026.py` と同じ確率モデル（プラケット=ルース型＋ペース展開）で
モンテカルロ試行し、任意の買い目プランの「回収率(期待値)・的中率・払戻分布」を算出する。

主な機能
--------
1. 単勝/複勝/馬連/馬単/ワイド/3連複/3連単 のモデル的中確率を算出
2. 当日の実オッズ(ACTUAL_ODDS)を入れれば、それを使ってEVを計算
   （未入力の券種は「市場想定モデル＋控除率」から推定オッズを生成）
3. プラン(複数の買い目＋掛金)を渡すと、回収率・いずれか的中率・払戻の分位点を出力
4. 特定の決着シナリオで「どの買い目が当たるか」を判定

使い方
------
  $ python3 simulations/bet_optimizer.py
  当日オッズが分かったら ACTUAL_ODDS に追記して再実行 → 本当のEVが出る。

依存ライブラリなし (Python標準ライブラリのみ)。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# 出走馬データ（2026/06/14 確定枠順・想定騎手・予想オッズ・脚質・追い切り・馬体重増減）
# --------------------------------------------------------------------------
@dataclass
class Horse:
    num: int
    draw: int
    name: str
    jockey: str
    odds: float      # 予想/想定 単勝オッズ
    style: str       # 逃げ/先行/差し/追込
    oikiri: str      # 追い切り評価 ◎/○/▲
    wdiff: int       # 調教後馬体重の前走比(kg)
    rating: float = field(default=0.0, init=False)


FIELD: list[Horse] = [
    Horse(1, 1, "ダノンデサイル",   "戸崎圭",  9.3, "差し", "◎",  6),
    Horse(2, 1, "ミュージアムマイル", "レーン",  5.0, "先行", "○",  8),
    Horse(3, 2, "シュガークン",     "吉村誠", 60.0, "先行", "○",  0),
    Horse(4, 2, "ミクニインスパイア", "丹内",  46.0, "先行", "▲",  2),
    Horse(5, 3, "クロワデュノール", "北村友",  2.5, "先行", "◎",  2),
    Horse(6, 3, "ビザンチンドリーム", "西村淳", 13.0, "追込", "○",  0),
    Horse(7, 4, "ファミリータイム", "幸",    80.0, "先行", "▲",  4),
    Horse(8, 4, "タガノデュード",   "高杉", 120.0, "差し", "▲", -2),
    Horse(9, 5, "コスモキュランダ", "横山武", 14.0, "差し", "○",  6),
    Horse(10, 5, "ジューンテイク",  "松山",  55.0, "逃げ", "▲",  0),
    Horse(11, 6, "シンエンペラー",  "坂井",  30.0, "先行", "○",  2),
    Horse(12, 6, "マイネルエンペラー","川田",  50.0, "先行", "▲",  4),
    Horse(13, 7, "シェイクユアハート","古川吉", 90.0, "差し", "▲", -2),
    Horse(14, 7, "スティンガーグラス","岩田望", 41.0, "差し", "○",  0),
    Horse(15, 7, "マイユニバース",  "横山典", 17.0, "差し", "◎",  2),
    Horse(16, 8, "メイショウタバル", "武豊",   5.2, "逃げ", "◎",  6),
    Horse(17, 8, "レガレイラ",     "ルメール", 10.0, "追込", "○", 12),
    Horse(18, 8, "ミステリーウェイ", "松本", 120.0, "差し", "▲",  2),
]
BY_NUM = {h.num: h for h in FIELD}

# --------------------------------------------------------------------------
# モデル・パラメータ（HTML/統計版と同一）
# --------------------------------------------------------------------------
N_SIMS = 200_000
STYLE_SENS = {"逃げ": -0.95, "先行": -0.50, "差し": +0.55, "追込": +0.95}
LEADERS = {"逃げ": 1.0, "先行": 0.45}
PACE_CENTER, PACE_GAIN, NOISE = 4.70, 0.40, 0.95

# JRA控除率の目安（推定オッズ生成に使用）
TAKEOUT = {"単勝": 0.20, "複勝": 0.20, "馬連": 0.225,
           "馬単": 0.225, "ワイド": 0.225, "3連複": 0.25, "3連単": 0.275}

# 当日の実オッズを入れるとEV計算に使う（前日夕〜当日朝に馬連/3連複が公開される）。
# 改善案の7点ぶんのキーを用意。発走前に「右辺」を実オッズに書き換えて再実行すれば、
# 推定でなく本当のEV・回収率で判定できる（コメントの値は現時点の推定オッズ）。
ACTUAL_ODDS: dict = {
    # ("馬連",  frozenset({5, 1})):  13.4,   # クロワ-ダノン
    # ("馬連",  frozenset({5, 2})):   6.6,   # クロワ-ミュージアム
    # ("馬連",  frozenset({5, 16})):  6.8,   # クロワ-タバル
    # ("馬連",  frozenset({5, 6})):  18.7,   # クロワ-ビザンチン
    # ("3連複", frozenset({5, 1, 2})): 18.8, # クロワ-ダノン-ミュージアム
    # ("3連複", frozenset({5, 1, 16})):19.2, # クロワ-ダノン-タバル
    # ("3連複", frozenset({5, 1, 6})): 56.9, # クロワ-ダノン-ビザンチン ←最妙味
}


def assign_ratings(market_only: bool = False) -> None:
    """予想オッズ→正規化勝率→ln(p)。market_only=Falseなら追い切り・馬体補正を加味。"""
    raw = [1.0 / h.odds for h in FIELD]
    tot = sum(raw)
    for h, r in zip(FIELD, raw):
        rating = math.log(r / tot)
        if not market_only:
            rating += (0.12 if h.oikiri == "◎" else 0.0 if h.oikiri == "○" else -0.08)
            if h.wdiff >= 8:
                rating -= 0.05
            elif h.wdiff <= -4:
                rating -= 0.04
        h.rating = rating


def draw_bias(d: int) -> float:
    if d <= 2: return 0.08
    if d == 3: return 0.05
    if d in (4, 5): return 0.0
    if d == 6: return -0.02
    if d == 7: return -0.05
    return -0.08


def gumbel() -> float:
    u = random.random()
    while u <= 0.0:
        u = random.random()
    return -math.log(-math.log(u))


def sample_order(market_only: bool) -> list[int]:
    """1レース分の着順（馬番リスト, index0=1着）を返す。"""
    press = sum(LEADERS.get(h.style, 0.0) for h in FIELD)
    pace = (press - PACE_CENTER) + random.gauss(0.0, 0.85)
    merit = []
    for h in FIELD:
        m = h.rating
        if not market_only:
            m += PACE_GAIN * pace * STYLE_SENS[h.style] + draw_bias(h.draw)
        m += gumbel() * NOISE
        merit.append((m, h.num))
    merit.sort(reverse=True)
    return [num for _, num in merit]


# --------------------------------------------------------------------------
# 確率の集計
# --------------------------------------------------------------------------
def bet_key(btype: str, sel):
    """買い目の辞書キー。順序不問券種はfrozenset、順序券種はtuple。"""
    if btype in ("馬連", "ワイド", "3連複"):
        return (btype, frozenset(sel))
    return (btype, tuple(sel))


def hits(btype: str, sel, order: list[int]) -> bool:
    top1, top2, top3 = order[0], order[:2], order[:3]
    s = list(sel)
    if btype == "単勝":   return top1 == s[0]
    if btype == "複勝":   return s[0] in top3
    if btype == "馬連":   return set(top2) == set(s)
    if btype == "馬単":   return order[0] == s[0] and order[1] == s[1]
    if btype == "ワイド": return set(s).issubset(set(top3))
    if btype == "3連複":  return set(top3) == set(s)
    if btype == "3連単":  return top3 == s
    raise ValueError(btype)


def prob_table(bets, market_only: bool, n: int = N_SIMS, seed: int = 2026) -> dict:
    """渡した買い目集合の的中確率を一括集計。"""
    assign_ratings(market_only)
    random.seed(seed)
    cnt = {bet_key(b[0], b[1]): 0 for b in bets}
    specs = [(b[0], tuple(b[1]), bet_key(b[0], b[1])) for b in bets]
    for _ in range(n):
        order = sample_order(market_only)
        for btype, sel, key in specs:
            if hits(btype, sel, order):
                cnt[key] += 1
    return {k: c / n for k, c in cnt.items()}


def est_odds(btype: str, market_prob: float) -> float:
    if market_prob <= 0:
        return 9999.0
    return (1.0 - TAKEOUT[btype]) / market_prob


def odds_for(btype: str, sel, market_prob: float) -> float:
    return ACTUAL_ODDS.get(bet_key(btype, sel)) or est_odds(btype, market_prob)


# --------------------------------------------------------------------------
# プラン評価
# --------------------------------------------------------------------------
def evaluate(name: str, plan: list, n: int = N_SIMS) -> dict:
    """plan = [(券種, (馬番...), 掛金), ...] を評価して表示。"""
    bets = [(b[0], b[1]) for b in plan]
    pmodel = prob_table(bets, market_only=False, n=n, seed=11)
    pmkt = prob_table(bets, market_only=True, n=n, seed=23)

    print("\n" + "=" * 76)
    print(f" {name}")
    print("=" * 76)
    print(f"{'券種':<6}{'買い目':<14}{'掛金':>6}{'model%':>8}{'市場%':>7}"
          f"{'使用O':>8}{'的中払戻':>9}{'EV円':>8}")
    print("-" * 76)
    total_stake, total_ev = 0, 0.0
    odds_map = {}
    for btype, sel, stake in plan:
        key = bet_key(btype, sel)
        pm, pk = pmodel[key], pmkt[key]
        o = odds_for(btype, sel, pk)
        odds_map[key] = o
        payout = o * stake
        ev = pm * payout
        total_stake += stake
        total_ev += ev
        actual = "実" if ACTUAL_ODDS.get(key) else "推"
        sel_s = "-".join(map(str, sel))
        print(f"{btype:<6}{sel_s:<14}{stake:>6}{pm*100:>7.2f}{pk*100:>7.2f}"
              f"{o:>7.1f}{actual}{payout:>8.0f}{ev:>8.0f}")
    print("-" * 76)

    # 払戻分布（重複的中も合算）をモンテカルロで実測
    assign_ratings(False)
    random.seed(99)
    specs = [(b[0], tuple(b[1]), bet_key(b[0], b[1]), b[2]) for b in plan]
    payouts = []
    hit_any = 0
    for _ in range(n):
        order = sample_order(False)
        pay = 0.0
        won = False
        for btype, sel, key, stake in specs:
            if hits(btype, sel, order):
                pay += odds_map[key] * stake
                won = True
        if won:
            hit_any += 1
        payouts.append(pay)
    payouts.sort()
    exp_pay = sum(payouts) / n
    roi = exp_pay / total_stake * 100
    p_hit = hit_any / n * 100
    two_x = sum(1 for p in payouts if p >= 2 * total_stake) / n * 100
    band = sum(1 for p in payouts if 2 * total_stake <= p <= 5 * total_stake) / n * 100
    print(f" 合計掛金 {total_stake:,}円 / 期待払戻 {exp_pay:,.0f}円 / "
          f"回収率 {roi:.1f}% / いずれか的中 {p_hit:.1f}%")
    print(f" ★払戻が2倍以上になる確率 {two_x:.1f}% / うち2〜5倍に収まる確率 {band:.1f}%")
    # 当たったときの払戻（条件付き）分位点
    wins = [p for p in payouts if p > 0]
    if wins:
        wins.sort()
        med = wins[len(wins)//2]
        mx = wins[-1]
        print(f" 的中時の払戻: 中央値 {med:,.0f}円 / 最大 {mx:,.0f}円 "
              f"（最大は予算の {mx/total_stake:.1f}倍）")
    return {"roi": roi, "p_hit": p_hit, "exp_pay": exp_pay}


def scenario(name: str, top3_order: list[int], plan: list) -> None:
    """指定の上位3頭(着順)で、どの買い目が当たるかを判定。"""
    order = list(top3_order) + [x for x in range(1, 19) if x not in top3_order]
    print(f"\n[シナリオ] {name}: " + "→".join(
        f"{m}{BY_NUM[m].name}" for m in top3_order))
    won = []
    for btype, sel, stake in plan:
        if hits(btype, sel, order):
            won.append((btype, sel, stake))
    if not won:
        print("  的中なし")
        return
    for btype, sel, stake in won:
        print(f"  ◎的中 {btype} {'-'.join(map(str, sel))}（{stake}円）")
    print(f"  → {len(won)}点的中")


# --------------------------------------------------------------------------
# プラン定義（馬番: クロワ=5, タバル=16, ダノン=1, ミュージアム=2, ビザ=6, シェイク=13）
# --------------------------------------------------------------------------
PLAN_F = [  # 案F（Fable5案・合計10,500円＝予算超過に注意）
    ("馬連", (5, 16), 6500), ("馬連", (5, 1), 2500), ("馬連", (16, 1), 1500),
]
PLAN_C = [  # 案C（ChatGPT案）
    ("馬連", (5, 2), 3000), ("馬連", (5, 16), 2000), ("馬連", (5, 1), 2000),
    ("馬単", (16, 5), 500),
    ("3連複", (5, 16, 2), 600), ("3連複", (5, 16, 1), 400), ("3連複", (5, 2, 1), 1000),
    ("3連複", (5, 2, 13), 300), ("3連複", (5, 1, 13), 200),
]
PLAN_IMP = [  # 改善案（クロワ軸＋妙味ダノン/ビザンチン上乗せ、死に金カット）
    ("馬連", (5, 1), 2500), ("馬連", (5, 2), 1500), ("馬連", (5, 16), 1000),
    ("馬連", (5, 6), 1500),
    ("3連複", (5, 1, 2), 1500), ("3連複", (5, 1, 16), 1000), ("3連複", (5, 1, 6), 1000),
]
# 最終推奨案：払戻「2〜5倍」になる確率を最大化（最頻ペアを各馬券ちょうど2倍超で詰め込み）。
# 当日ルール: 各掛金 = 切り上げ(20000 / 実オッズ)。合計が1万を超えたら出現率の低い順(⑤→④)に削る。
PLAN_FINAL = [
    ("馬連", (5, 2), 3100),   # クロワ-ミュージアム（最頻ペア）
    ("馬連", (5, 16), 3000),  # クロワ-タバル
    ("馬連", (5, 1), 1600),   # クロワ-ダノン
    ("馬連", (5, 6), 1500),   # クロワ-ビザンチン
    ("馬連", (16, 1), 800),   # タバル-ダノン（クロワ崩壊の保険）
]


def main() -> None:
    print("宝塚記念2026 馬券オプティマイザ（モンテカルロEV判定）")
    print(f"試行回数: {N_SIMS:,} 回 / 当日実オッズ登録数: {len(ACTUAL_ODDS)} 件"
          + ("（未登録の券種は推定オッズを使用）" if not ACTUAL_ODDS else ""))
    evaluate("案F（Fable5）: 馬連3点・クロワ-タバル偏重", PLAN_F)
    evaluate("案C（ChatGPT）: 馬連3＋馬単＋3連複5", PLAN_C)
    evaluate("改善案: クロワ軸＋ダノン/ビザンチン妙味", PLAN_IMP)
    res = evaluate("★最終推奨: 2〜5倍の的中確率を最大化（馬連5点）", PLAN_FINAL)

    # クロワ-ダノン-ビザンチン決着のシナリオ（改善案で何点当たるか）
    print("\n" + "=" * 76)
    print(" シナリオ検証：クロワ・ダノン・ビザンチンで上位独占したら（改善案）")
    print("=" * 76)
    scenario("クロワ1→ダノン2→ビザ3", [5, 1, 6], PLAN_IMP)
    scenario("クロワ1→ビザ2→ダノン3", [5, 6, 1], PLAN_IMP)
    scenario("ダノン1→ビザ2→クロワ3（クロワ取りこぼし）", [1, 6, 5], PLAN_IMP)

    print("\n※ 推定オッズは市場想定モデル＋控除率からの試算。当日の実オッズを")
    print("  ACTUAL_ODDS に入れて再実行すると、本当のEVで判定できます。")
    print("※ 馬券は自己責任で。本ツールは確率モデルの試算であり的中・利益を保証しません。")


if __name__ == "__main__":
    main()
