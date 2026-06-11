#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第67回 宝塚記念 (GI) 緻密モンテカルロ・シミュレーション
================================================================

開催: 2026年6月14日(日) 阪神競馬場 芝・内回り2200m 3歳上 国際GI 18頭立て

このスクリプトは、確定枠順・想定騎手・予想オッズ・脚質をもとに、
ペース展開と能力のばらつきを織り込んだプラケット=ルース型の
モンテカルロ・モデルでレースを多数回試行し、
各馬の勝率／連対率／複勝率・期待着順・推奨馬券(期待値)を算出する。

依存ライブラリなし (Python 標準ライブラリのみ)。

モデル概要
----------
1. 各馬の「素の能力」 rating_i = ln(p_i) を予想オッズの正規化勝率 p_i から付与。
   → ノイズが Gumbel(0,1) のとき、無補正ベースラインの勝率は市場オッズに一致する
     (多項ロジット / Plackett-Luce)。これを土台に独自補正を重ねる。
2. 各試行ごとにペース脚質を確率的に決定 (超スロー〜超ハイ)。
   逃げ・先行馬の頭数(=隊列の前圧)からペースの平均をずらす。
3. 脚質適性: ハイペースなら差し・追込に加点、逃げ・先行に減点。
   スローはその逆。各馬の脚質係数で展開利・不利を反映。
4. 枠順補正: 阪神2200mはスタート後すぐ1コーナー、内枠やや有利・大外やや不利。
5. 各試行で performance_i = rating + 展開補正 + 枠補正 + Gumbel ノイズ を引き、
   降順に並べて着順を決定 → N 回繰り返して分布を集計。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# 出走馬データ (2026/06/14 枠順確定・想定騎手・予想オッズ・脚質)
#   odds   : 各予想ソースを平均した想定単勝オッズ
#   style  : 脚質 ("逃げ"/"先行"/"差し"/"追込")
#   note   : 簡易メモ
# --------------------------------------------------------------------------
@dataclass
class Horse:
    num: int          # 馬番
    draw: int         # 枠番
    name: str
    jockey: str
    odds: float       # 想定単勝オッズ
    style: str        # 脚質
    note: str = ""
    # 集計用
    rating: float = field(default=0.0, init=False)
    wins: int = field(default=0, init=False)
    top2: int = field(default=0, init=False)
    top3: int = field(default=0, init=False)
    finish_sum: int = field(default=0, init=False)


FIELD: list[Horse] = [
    Horse(1, 1, "ダノンデサイル",   "戸崎圭",  8.0, "差し", "24年ダービー馬。安定した差し脚"),
    Horse(2, 1, "ミュージアムマイル", "レーン",  4.8, "先行", "前走有馬好走。先行から渋太く"),
    Horse(3, 2, "シュガークン",     "吉村誠", 60.0, "先行", "上がり馬。展開待ち"),
    Horse(4, 2, "ミクニインスパイア", "丹内",  46.0, "先行", "ハナも切れる積極策"),
    Horse(5, 3, "クロワデュノール", "北村友",  2.5, "先行", "大阪杯・天皇賞春連勝。春古馬三冠挑戦の中心"),
    Horse(6, 3, "ビザンチンドリーム", "西村淳", 14.0, "追込", "末脚一閃型。展開はまる必要"),
    Horse(7, 4, "ファミリータイム", "幸",    80.0, "先行", "ローカル実績。相手強化"),
    Horse(8, 4, "タガノデュード",   "高杉",  120.0, "差し", "格下挑戦。一発は薄い"),
    Horse(9, 5, "コスモキュランダ", "横山武", 14.0, "差し", "皐月賞2着の実績。差し脚確か"),
    Horse(10, 5, "ジューンテイク",  "松山",  55.0, "逃げ", "前々から粘り込み図る"),
    Horse(11, 6, "シンエンペラー",  "坂井",  30.0, "先行", "凱旋門賞挑戦帰り。血統は2200向き"),
    Horse(12, 6, "マイネルエンペラー","川田",  50.0, "先行", "川田鞍上で位置取り良化期待"),
    Horse(13, 7, "シェイクユアハート","古川吉", 90.0, "差し", "牝馬。展開待ちの差し"),
    Horse(14, 7, "スティンガーグラス","岩田望", 41.0, "差し", "重賞好走歴。一本調子の差し脚"),
    Horse(15, 7, "マイユニバース",  "横山典", 17.0, "差し", "ノリ騎乗で展開突く差し"),
    Horse(16, 8, "メイショウタバル", "武豊",   5.0, "逃げ", "昨年覇者・連覇狙い。逃げて自分の競馬"),
    Horse(17, 8, "レガレイラ",     "ルメール", 9.5, "追込", "24年有馬記念覇者。上がり最速級の決め手"),
    Horse(18, 8, "ミステリーウェイ", "松本",  120.0, "差し", "格下挑戦。基本は厳しい"),
]


# --------------------------------------------------------------------------
# モデル・パラメータ
# --------------------------------------------------------------------------
N_SIMS = 300_000          # 試行回数

# 脚質ごとの「展開感応度」。pace_factor>0=ハイペース時に
#   closer(差し/追込)有利、leader(逃げ/先行)不利。
STYLE_PACE_SENS = {
    "逃げ": -1.00,   # ハイペースで最も苦しい
    "先行": -0.55,
    "差し": +0.55,
    "追込": +0.95,   # ハイペースで最も恵まれる
}

# 脚質ごとの「素の安定度(ノイズ幅)」補正。逃げ・追込は変動が大きい。
STYLE_SIGMA = {
    "逃げ": 1.15,
    "先行": 0.95,
    "差し": 0.95,
    "追込": 1.10,
}

# 隊列前圧(逃げ・先行の頭数)からペース平均を決めるための係数
LEADERS_STYLES = {"逃げ": 1.0, "先行": 0.45}

# 標準的な18頭立ての隊列前圧の目安。これを基準にペースの偏りを測り、
# 中央値付近に来るよう base_pace を中心化する(closer への系統的過剰加点を防ぐ)。
PACE_CENTER = 4.70

# ペース補正の強さ(展開利・不利の振れ幅を決める係数)
PACE_GAIN = 0.38


def assign_ratings(field_: list[Horse]) -> None:
    """予想オッズ→控除前の正規化勝率→rating=ln(p) を付与。"""
    raw = [1.0 / h.odds for h in field_]
    total = sum(raw)
    for h, r in zip(field_, raw):
        p = r / total
        h.rating = math.log(p)


def draw_bias(draw: int) -> float:
    """枠順補正(阪神2200・内回り)。内枠やや有利、大外やや不利。小さめに。"""
    if draw <= 2:
        return +0.08
    if draw == 3:
        return +0.05
    if draw in (4, 5):
        return 0.0
    if draw == 6:
        return -0.02
    if draw == 7:
        return -0.05
    return -0.08  # 8枠


def gumbel() -> float:
    """標準 Gumbel(0,1) 乱数。 -ln(-ln(U))."""
    u = random.random()
    # u が 0 になる確率は実質ゼロだが保険
    while u <= 0.0:
        u = random.random()
    return -math.log(-math.log(u))


def pace_pressure(field_: list[Horse]) -> float:
    """前に行く馬の数から基準ペース(正=ハイ寄り)を算出。"""
    press = sum(LEADERS_STYLES.get(h.style, 0.0) for h in field_)
    # press~2.5 前後を中庸とし、それを中心に展開
    return press - PACE_CENTER


def simulate(field_: list[Horse], n: int = N_SIMS, seed: int = 2026) -> None:
    random.seed(seed)
    base_pace = pace_pressure(field_)
    style_sens = [STYLE_PACE_SENS[h.style] for h in field_]
    style_sig = [STYLE_SIGMA[h.style] for h in field_]
    dbias = [draw_bias(h.draw) for h in field_]
    ratings = [h.rating for h in field_]
    m = len(field_)

    for _ in range(n):
        # 各レースのペース実現値: 隊列前圧 + ランダム変動
        pace = base_pace + random.gauss(0.0, 0.85)

        perfs = []
        for i in range(m):
            # 展開補正: ペースと脚質感応度の積
            pace_adj = PACE_GAIN * pace * style_sens[i]
            noise = style_sig[i] * gumbel()
            perf = ratings[i] + pace_adj + dbias[i] + noise
            perfs.append((perf, i))

        perfs.sort(reverse=True)
        for pos, (_, i) in enumerate(perfs, start=1):
            h = field_[i]
            h.finish_sum += pos
            if pos == 1:
                h.wins += 1
            if pos <= 2:
                h.top2 += 1
            if pos <= 3:
                h.top3 += 1


def report(field_: list[Horse], n: int = N_SIMS) -> None:
    order = sorted(field_, key=lambda h: h.wins, reverse=True)

    print("=" * 78)
    print(" 第67回 宝塚記念 (GI)  モンテカルロ・シミュレーション結果")
    print(" 2026/06/14 阪神 芝2200m 18頭立て / 試行回数: {:,} 回".format(n))
    print("=" * 78)
    print(f"{'馬番':>2} {'馬名':<9} {'騎手':<5} {'脚質':<3} "
          f"{'想定':>5} {'勝率':>6} {'連対':>6} {'複勝':>6} {'期待着':>6} {'単EV':>6}")
    print("-" * 78)
    for h in order:
        win = h.wins / n
        t2 = h.top2 / n
        t3 = h.top3 / n
        avg = h.finish_sum / n
        ev = win * h.odds  # 単勝期待値 (1.0 が損益分岐)
        star = " ★" if ev >= 1.0 and win >= 0.02 else ""
        print(f"{h.num:>2} {h.name:<9} {h.jockey:<5} {h.style:<3} "
              f"{h.odds:>5.1f} {win*100:>5.1f}% {t2*100:>5.1f}% {t3*100:>5.1f}% "
              f"{avg:>6.2f} {ev:>5.2f}{star}")
    print("-" * 78)
    print(" ★ = 単勝期待値1.0超(妙味あり) / 期待着=平均着順")

    # ---- 馬券フォーカス ----
    print()
    print(" ◎本命・相手評価 (勝率上位)")
    top = order[:5]
    for rank, h in enumerate(top, 1):
        print(f"   {rank}. {h.name:<9} 勝率{h.wins/n*100:4.1f}%  複勝{h.top3/n*100:4.1f}%  {h.note}")

    # 価値馬(単EV>1.05 かつ 妙味)
    print()
    print(" ◇妙味馬 (市場オッズに対し過小評価=単勝期待値が高い)")
    value = sorted(field_, key=lambda h: h.wins / n * h.odds, reverse=True)
    shown = 0
    for h in value:
        ev = h.wins / n * h.odds
        if ev >= 1.05 and h.wins / n >= 0.015:
            print(f"   {h.name:<9} 単EV {ev:4.2f}  (model勝率 {h.wins/n*100:4.1f}% / 想定{h.odds:.1f}倍)")
            shown += 1
        if shown >= 5:
            break
    if shown == 0:
        print("   該当なし (市場が概ね効率的)")


def pair_analysis(field_: list[Horse], n: int = N_SIMS, seed: int = 7) -> None:
    """馬連・ワイド上位の出目を別試行で集計。"""
    random.seed(seed)
    base_pace = pace_pressure(field_)
    style_sens = [STYLE_PACE_SENS[h.style] for h in field_]
    style_sig = [STYLE_SIGMA[h.style] for h in field_]
    dbias = [draw_bias(h.draw) for h in field_]
    ratings = [h.rating for h in field_]
    m = len(field_)

    quinella: dict[frozenset, int] = {}
    trio: dict[frozenset, int] = {}
    runs = min(n, 120_000)
    for _ in range(runs):
        pace = base_pace + random.gauss(0.0, 0.85)
        perfs = []
        for i in range(m):
            pace_adj = PACE_GAIN * pace * style_sens[i]
            perf = ratings[i] + pace_adj + dbias[i] + style_sig[i] * gumbel()
            perfs.append((perf, i))
        perfs.sort(reverse=True)
        a, b, c = perfs[0][1], perfs[1][1], perfs[2][1]
        q = frozenset((a, b))
        t = frozenset((a, b, c))
        quinella[q] = quinella.get(q, 0) + 1
        trio[t] = trio.get(t, 0) + 1

    def fmt(s):
        return "-".join(str(field_[i].num) for i in sorted(s))

    print()
    print(" 馬連 出現上位 (順不同2頭):")
    for s, cnt in sorted(quinella.items(), key=lambda kv: kv[1], reverse=True)[:8]:
        names = " / ".join(field_[i].name for i in sorted(s))
        print(f"   {fmt(s):<7} {cnt/runs*100:4.1f}%   {names}")

    print()
    print(" 3連複 出現上位 (順不同3頭):")
    for s, cnt in sorted(trio.items(), key=lambda kv: kv[1], reverse=True)[:8]:
        names = "・".join(field_[i].name for i in sorted(s))
        print(f"   {fmt(s):<9} {cnt/runs*100:4.1f}%   {names}")


def main() -> None:
    assign_ratings(FIELD)
    simulate(FIELD)
    report(FIELD)
    pair_analysis(FIELD)
    print()
    print("※ 本シミュレーションは予想オッズ・脚質・枠順から構築した確率モデルに")
    print("  基づく試算であり、的中・利益を保証するものではありません。")


if __name__ == "__main__":
    main()
