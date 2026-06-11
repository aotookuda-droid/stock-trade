"""
2026年 宝塚記念 (G1) シミュレーション
阪神競馬場 芝2200m (外回り)
2026年6月28日(日)

コース概要:
- スタート: 3コーナー奥のポケット
- 1コーナーまで約600m の長い直線
- 外回り使用（4コーナー大回り）
- 最後の直線: 約473m
- 高低差: 約2.4m（3コーナー付近が最高点）
"""

import random
import math
from dataclasses import dataclass, field
from typing import Optional
import hashlib
import time


# ──────────────────────────────────────────────
# データ定義
# ──────────────────────────────────────────────

@dataclass
class Horse:
    name: str
    age: int
    sex: str            # 牡/牝/騸
    weight_kg: float    # 馬体重 (kg)
    jockey: str
    trainer: str
    stable: str         # 所属厩舎
    rating: float       # 総合レーティング 0-100
    stamina: float      # スタミナ 0-100
    speed: float        # スピード 0-100
    mental: float       # 気性・精神力 0-100
    track_bias: float   # 道悪適性 (-10 to +10)
    form: float         # 直近の調子 (-10 to +10)
    gate_no: int = 0    # 枠番
    horse_no: int = 0   # 馬番
    odds: float = 0.0   # オッズ


@dataclass
class RaceCondition:
    weather: str        # 晴/曇/雨/小雨
    track_state: str    # 良/稍重/重/不良
    moisture_pct: float # 含水率 %
    temperature_c: float
    humidity_pct: float
    wind_ms: float      # 風速 m/s
    wind_dir: str       # 向かい風/追い風/横風


@dataclass
class RunResult:
    horse: Horse
    finish_pos: int
    time_sec: float
    margin_len: float   # 着差 (馬身)
    last_3f_sec: float  # 上がり3F
    sectional: list     # ラップタイム
    comment: str


# ──────────────────────────────────────────────
# 2026年 宝塚記念 出走馬リスト
# (枠順確定版 – 想定 18頭フルゲート)
# ──────────────────────────────────────────────

ENTRIES_RAW = [
    # name, age, sex, weight, jockey, trainer, stable, rating, stamina, speed, mental, track_bias, form
    ("ドウデュース",        5, "牡", 468, "武豊",     "友道康夫", "栗東",  96, 90, 97, 85,  2, 8),
    ("リバティアイランド",  5, "牝", 446, "川田将雅", "中内田充",  "栗東",  94, 88, 96, 90,  1, 7),
    ("タスティエーラ",      5, "牡", 474, "松山弘平", "堀宣行",   "美浦",  88, 92, 86, 82, -1, 5),
    ("ソールオリエンス",    5, "牡", 460, "横山武史", "手塚貴久", "美浦",  89, 89, 88, 75,  0, 6),
    ("シャフリヤール",      7, "牡", 480, "藤岡佑介", "藤原英昭", "栗東",  85, 93, 84, 88,  3, 4),
    ("スターズオンアース", 6, "牝", 444, "ルメール",  "高柳大輔", "美浦",  91, 87, 93, 89,  0, 9),
    ("ジャスティンパレス", 6, "牡", 476, "岩田望来", "杉山晴紀", "栗東",  90, 94, 89, 83,  4, 7),
    ("プログノーシス",      7, "牡", 494, "中山雄太", "斉藤崇史", "栗東",  87, 91, 87, 80,  2, 6),
    ("ベラジオオペラ",      5, "牡", 470, "横山和生", "上村洋行", "栗東",  86, 88, 88, 84,  1, 8),
    ("ローシャムパーク",    6, "牡", 488, "戸崎圭太", "高木登",   "美浦",  84, 90, 83, 79,  5, 5),
    ("ブローザホーン",      7, "牡", 466, "菅原明良", "吉岡辰弥", "栗東",  83, 95, 80, 82,  8, 6),
    ("ヴェルトハイム",      5, "牡", 472, "池添謙一", "中竹和也", "栗東",  82, 86, 85, 81,  1, 5),
    ("チェルヴィニア",      4, "牝", 440, "ルメール",  "木村哲也", "美浦",  88, 84, 91, 87,  0, 8),
    ("エピファニー",        5, "牡", 476, "北村友一", "鮫島一歩", "栗東",  81, 87, 83, 80,  2, 4),
    ("ディープボンド",      8, "牡", 484, "幸英明",   "大久保龍", "栗東",  79, 97, 77, 86,  6, 3),
    ("ハーパー",            5, "牝", 434, "岩田康誠", "松永幹夫", "栗東",  80, 83, 83, 82,  0, 6),
    ("サトノグランツ",      5, "牡", 462, "坂井瑠星", "池添兼雄", "栗東",  82, 88, 82, 78,  1, 5),
    ("アドミラブル",        5, "牡", 458, "三浦皇成", "萩原清",   "美浦",  78, 86, 79, 76,  3, 4),
]

def build_entries() -> list[Horse]:
    horses = []
    for i, row in enumerate(ENTRIES_RAW):
        h = Horse(
            name=row[0], age=row[1], sex=row[2], weight_kg=row[3],
            jockey=row[4], trainer=row[5], stable=row[6],
            rating=row[7], stamina=row[8], speed=row[9],
            mental=row[10], track_bias=row[11], form=row[12],
        )
        horses.append(h)
    return horses


# ──────────────────────────────────────────────
# 枠順抽選
# ──────────────────────────────────────────────

def draw_gates(horses: list[Horse]) -> list[Horse]:
    """18頭の場合: 1〜6枠が各3頭, 7〜8枠が各0頭 → JRA方式で割り振り"""
    n = len(horses)
    order = list(range(n))
    random.shuffle(order)

    # 馬番割り振り
    for rank, idx in enumerate(order):
        horses[idx].horse_no = rank + 1

    horses.sort(key=lambda h: h.horse_no)

    # 枠番: 18頭フルゲートはJRA公式では
    # 1枠:1,2 / 2枠:3,4 / 3枠:5,6 / 4枠:7,8 / 5枠:9,10 / 6枠:11,12 / 7枠:13,14,15 / 8枠:16,17,18
    gate_map = {
        1: 1, 2: 1,
        3: 2, 4: 2,
        5: 3, 6: 3,
        7: 4, 8: 4,
        9: 5, 10: 5,
        11: 6, 12: 6,
        13: 7, 14: 7, 15: 7,
        16: 8, 17: 8, 18: 8,
    }
    for h in horses:
        h.gate_no = gate_map[h.horse_no]

    return horses


# ──────────────────────────────────────────────
# 当日天気シミュレーション (6月下旬・阪神)
# ──────────────────────────────────────────────

WEATHER_SCENARIOS = [
    # (weight, weather, track_state, moisture, temp, humidity, wind_ms, wind_dir)
    (40, "晴れ",  "良",   8.5,  27, 55, 3.2, "向かい風"),
    (25, "曇り",  "良",  10.0,  24, 68, 2.5, "横風"),
    (15, "曇り",  "稍重", 14.0, 22, 78, 3.8, "横風"),
    (12, "小雨",  "重",   18.5, 20, 85, 4.5, "向かい風"),
    ( 8, "雨",    "不良", 24.0, 19, 92, 5.2, "向かい風"),
]

def simulate_weather(seed: int) -> RaceCondition:
    rng = random.Random(seed)
    total = sum(w for w, *_ in WEATHER_SCENARIOS)
    r = rng.uniform(0, total)
    cumul = 0
    for w, weather, track, moisture, temp, humid, wind_ms, wind_dir in WEATHER_SCENARIOS:
        cumul += w
        if r <= cumul:
            return RaceCondition(
                weather=weather,
                track_state=track,
                moisture_pct=moisture,
                temperature_c=temp,
                humidity_pct=humid,
                wind_ms=wind_ms,
                wind_dir=wind_dir,
            )
    # fallback
    return RaceCondition("晴れ", "良", 8.5, 27, 55, 3.2, "向かい風")


# ──────────────────────────────────────────────
# 馬場状態補正
# ──────────────────────────────────────────────

TRACK_BASE_TIME = {
    "良":   132.0,  # 2200m 標準タイム (秒)
    "稍重": 133.5,
    "重":   135.5,
    "不良": 138.0,
}

TRACK_TIME_PENALTY = {
    "良":   0.0,
    "稍重": 0.8,
    "重":   2.2,
    "不良": 4.5,
}


def track_bias_bonus(horse: Horse, track_state: str) -> float:
    """道悪適性を基にボーナス/ペナルティを計算"""
    if track_state == "良":
        # 良馬場では適性値がマイナスの馬（重馬場得意）は不利
        return -horse.track_bias * 0.05
    elif track_state == "稍重":
        return horse.track_bias * 0.10
    elif track_state == "重":
        return horse.track_bias * 0.20
    else:  # 不良
        return horse.track_bias * 0.30


def gate_bias(gate_no: int, track_state: str) -> float:
    """阪神2200m 枠順有利不利 (外回り・長い1コーナーまでの直線で内枠有利)"""
    # 良馬場: 内枠やや有利 / 道悪: 外枠有利（内が荒れる）
    if track_state == "良":
        bias = {1: 0.4, 2: 0.3, 3: 0.2, 4: 0.0, 5: -0.1, 6: -0.2, 7: -0.3, 8: -0.4}
    else:
        bias = {1: -0.3, 2: -0.2, 3: -0.1, 4: 0.0, 5: 0.1, 6: 0.2, 7: 0.3, 8: 0.5}
    return bias.get(gate_no, 0.0)


# ──────────────────────────────────────────────
# ペース・展開シミュレーション
# ──────────────────────────────────────────────

def simulate_pace(horses: list[Horse], rng: random.Random) -> tuple[str, float]:
    """先行馬の数からペースを決定"""
    # 簡易的に rating が低い馬ほど前に行く傾向
    front_runners = sum(1 for h in horses if h.speed > 88)
    if front_runners >= 5:
        pace = "ハイペース"
        pace_factor = 1.8   # スタミナ消耗増
    elif front_runners <= 2:
        pace = "スローペース"
        pace_factor = -0.5  # 上がり勝負
    else:
        pace = "ミドルペース"
        pace_factor = 0.5
    return pace, pace_factor


# ──────────────────────────────────────────────
# 個別走破タイム計算
# ──────────────────────────────────────────────

def calc_horse_time(
    horse: Horse,
    condition: RaceCondition,
    pace: str,
    pace_factor: float,
    rng: random.Random,
) -> tuple[float, float]:
    """(走破タイム秒, 上がり3F秒) を返す"""
    base = TRACK_BASE_TIME[condition.track_state]

    # 総合能力スコア
    ability = (horse.rating * 0.4 + horse.stamina * 0.3 + horse.speed * 0.3)
    ability_delta = (ability - 88) * 0.12  # 88を基準に±

    # 馬場適性
    tb = track_bias_bonus(horse, condition.track_state)

    # 枠番バイアス
    gb = gate_bias(horse.gate_no, condition.track_state)

    # 調子
    form_delta = horse.form * 0.05

    # ペース適性: スタミナ型はスロー不利、スピード型はハイペース不利
    if pace == "ハイペース":
        pace_delta = (horse.stamina - 85) * 0.04 - pace_factor * 0.1
    elif pace == "スローペース":
        pace_delta = (horse.speed - 85) * 0.04 + pace_factor * 0.1
    else:
        pace_delta = 0.0

    # 風の影響（向かい風は全馬に影響）
    wind_penalty = 0.0
    if condition.wind_dir == "向かい風":
        wind_penalty = condition.wind_ms * 0.04

    # ランダム要素（レース中の不確定要素）
    noise = rng.gauss(0, 0.35)

    total_delta = -ability_delta - tb - gb - form_delta - pace_delta + wind_penalty + noise
    finish_time = base + total_delta

    # 上がり3F: スピード特化馬は速い上がり
    last_3f_base = 34.5 if condition.track_state == "良" else 35.5
    last_3f = last_3f_base - (horse.speed - 85) * 0.03 + rng.gauss(0, 0.15)

    return round(finish_time, 1), round(last_3f, 1)


# ──────────────────────────────────────────────
# ラップタイム生成 (12ハロン換算)
# ──────────────────────────────────────────────

def generate_sectionals(finish_time: float, pace: str, condition: RaceCondition) -> list[float]:
    """阪神2200m を 200m×11区間 のラップで近似"""
    # ペース別テンポ係数
    templates = {
        "ハイペース":   [12.5, 11.2, 11.8, 12.0, 12.3, 12.5, 12.3, 12.0, 11.8, 11.5, 11.1],
        "ミドルペース": [12.7, 11.8, 12.0, 12.2, 12.3, 12.2, 12.0, 11.8, 11.6, 11.3, 11.1],
        "スローペース": [13.0, 12.5, 12.5, 12.5, 12.3, 12.2, 12.0, 11.8, 11.5, 11.0, 10.7],
    }
    template = templates[pace]
    total_template = sum(template)
    ratio = finish_time / total_template
    laps = [round(t * ratio, 1) for t in template]
    return laps


# ──────────────────────────────────────────────
# オッズ計算
# ──────────────────────────────────────────────

def assign_odds(horses: list[Horse]) -> list[Horse]:
    """能力ベースの単勝オッズを割り当て"""
    scores = []
    for h in horses:
        s = h.rating * 0.5 + h.form * 2 + h.stamina * 0.2 + h.speed * 0.3
        scores.append(s)

    total = sum(scores)
    raw_odds = [total / s for s in scores]

    # 最小オッズ 1.5 に調整
    min_odds = min(raw_odds)
    scale = 1.5 / min_odds
    adj_odds = [round(o * scale, 1) for o in raw_odds]

    for h, o in zip(horses, adj_odds):
        h.odds = o

    return horses


# ──────────────────────────────────────────────
# レースコメント生成
# ──────────────────────────────────────────────

POSITION_COMMENTS = {
    1: ["好スタートを切り", "抜群のゲートから"],
    2: ["好位から", "2番手追走"],
    3: ["中団外目を", "中団から追走"],
    4: ["後方から", "後方待機から"],
    5: ["最後方から", "後方内側で脚を溜め"],
}

WIN_TEMPLATES = [
    "{name}が{jockey}騎手の好騎乗で{gate}枠から{pos}、直線豪快に差し切った！",
    "{name}（{jockey}）が{pos}追走、残り200mで先頭に立ち完勝！",
    "{name}が{pos}先行し、残り200mで突き放してそのまま押し切った！",
]

PLACE_TEMPLATES = [
    "{name}は{pos}追走も、勝ち馬には一歩及ばず{rank}",
    "{name}（{jockey}）は直線で見せ場を作ったが{rank}まで",
]

def make_comment(horse: Horse, pos: int, pace: str) -> str:
    pos_key = min(pos, 5)
    pos_desc = random.choice(POSITION_COMMENTS[pos_key])
    if pos == 1:
        t = random.choice(WIN_TEMPLATES)
        return t.format(
            name=horse.name, jockey=horse.jockey,
            gate=horse.gate_no, pos=pos_desc,
        )
    else:
        rank_str = f"{pos}着"
        t = random.choice(PLACE_TEMPLATES)
        return t.format(
            name=horse.name, jockey=horse.jockey,
            rank=rank_str, pos=pos_desc,
        )


# ──────────────────────────────────────────────
# メインシミュレーション
# ──────────────────────────────────────────────

def run_simulation(seed: Optional[int] = None) -> dict:
    if seed is None:
        seed = int(time.time() * 1000) % 2**31

    rng = random.Random(seed)

    print("=" * 62)
    print("  2026年 宝塚記念 (G1)  シミュレーション")
    print("  阪神競馬場 芝2200m (外回り) 2026年6月28日(日)")
    print("=" * 62)

    # 出走馬生成
    horses = build_entries()

    # 枠順抽選
    horses = draw_gates(horses)

    # オッズ計算
    horses = assign_odds(horses)

    # 天気シミュレーション
    condition = simulate_weather(seed)

    print(f"\n【当日の天気・馬場情報】")
    print(f"  天候  : {condition.weather}")
    print(f"  馬場状態: {condition.track_state}")
    print(f"  含水率 : {condition.moisture_pct:.1f}%")
    print(f"  気温  : {condition.temperature_c}°C")
    print(f"  湿度  : {condition.humidity_pct}%")
    print(f"  風速  : {condition.wind_ms} m/s ({condition.wind_dir})")

    # 枠順・出走馬一覧
    print(f"\n【出走馬一覧】")
    print(f"{'枠':>2} {'馬':>2}  {'馬名':<18} {'性齢':>4} {'騎手':<8} {'厩舎':>2} {'馬体重':>5} {'オッズ':>6}")
    print("-" * 62)
    for h in horses:
        sex_age = f"{h.sex}{h.age}"
        print(
            f"{h.gate_no:>2} {h.horse_no:>2}  {h.name:<18} {sex_age:>4} "
            f"{h.jockey:<8} {h.stable:>2} {h.weight_kg:>5.0f}  {h.odds:>5.1f}倍"
        )

    # ペース判定
    pace, pace_factor = simulate_pace(horses, rng)
    print(f"\n【展開予想】{pace}")

    # 走破タイム計算
    results_raw = []
    for horse in horses:
        t, last3f = calc_horse_time(horse, condition, pace, pace_factor, rng)
        results_raw.append((horse, t, last3f))

    # タイム順にソート
    results_raw.sort(key=lambda x: x[1])

    winner_time = results_raw[0][1]
    sectionals = generate_sectionals(winner_time, pace, condition)

    results: list[RunResult] = []
    for pos, (horse, t, last3f) in enumerate(results_raw, start=1):
        margin = (t - winner_time) / 0.2 if pos > 1 else 0.0  # 1馬身≒0.2秒
        comment = make_comment(horse, pos, pace)
        r = RunResult(
            horse=horse,
            finish_pos=pos,
            time_sec=t,
            margin_len=round(margin, 1),
            last_3f_sec=last3f,
            sectional=sectionals if pos == 1 else [],
            comment=comment,
        )
        results.append(r)

    # ──── 結果表示 ────
    print(f"\n【レース結果】")
    print(f"{'着':>3} {'枠':>2} {'馬':>2}  {'馬名':<18} {'タイム':>8} {'着差':>6} {'上がり':>6}")
    print("-" * 62)
    for r in results:
        h = r.horse
        time_str = _format_time(r.time_sec)
        margin_str = "----" if r.finish_pos == 1 else f"{r.margin_len:.1f}馬身"
        print(
            f"{r.finish_pos:>3} {h.gate_no:>2} {h.horse_no:>2}  {h.name:<18} "
            f"{time_str:>8} {margin_str:>6} {r.last_3f_sec:>5.1f}秒"
        )

    # ウィナーラップ
    print(f"\n【勝ち馬ラップ (200m×11)】")
    lap_str = " - ".join(f"{x:.1f}" for x in sectionals)
    print(f"  {lap_str}")
    win3f = sum(sectionals[-3:])
    front9f = sum(sectionals[:9]) if len(sectionals) >= 9 else 0
    print(f"  前半1000m (5F): {sum(sectionals[:5]):.1f}秒  /  上がり600m (3F): {win3f:.1f}秒")

    # コメント
    print(f"\n【レースコメント】")
    for r in results[:5]:
        print(f"  {r.finish_pos}着 {r.horse.name}: {r.comment}")

    # 払戻金（簡易）
    payouts = calc_payouts(results)
    print(f"\n【払戻金（100円単位）】")
    for bet_type, amount in payouts.items():
        print(f"  {bet_type}: {amount:,}円")

    print(f"\n  ※ seed={seed} で再現可能です。")
    print("=" * 62)

    return {
        "seed": seed,
        "condition": condition,
        "results": results,
        "pace": pace,
        "sectionals": sectionals,
    }


def _format_time(sec: float) -> str:
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m}:{s:05.2f}"


def calc_payouts(results: list[RunResult]) -> dict:
    """単勝・複勝・馬連・三連単の払戻金を簡易計算"""
    win_odds = results[0].horse.odds
    place1 = max(1.1, results[0].horse.odds * 0.3)
    place2 = max(1.1, results[1].horse.odds * 0.35)
    place3 = max(1.1, results[2].horse.odds * 0.4)

    h1, h2, h3 = results[0].horse, results[1].horse, results[2].horse
    renpuku = round(max(100, h1.odds * h2.odds * 8), -2)
    sanrentan = round(max(100, h1.odds * h2.odds * h3.odds * 40), -2)

    return {
        f"単勝  {h1.horse_no}番 {h1.name}": int(win_odds * 100),
        f"複勝  {h1.horse_no}番 {h1.name}": int(place1 * 100),
        f"複勝  {h2.horse_no}番 {h2.name}": int(place2 * 100),
        f"複勝  {h3.horse_no}番 {h3.name}": int(place3 * 100),
        f"馬連  {h1.horse_no}-{h2.horse_no}": int(renpuku),
        f"三連単 {h1.horse_no}-{h2.horse_no}-{h3.horse_no}": int(sanrentan),
    }


# ──────────────────────────────────────────────
# 複数回シミュレーションで傾向分析
# ──────────────────────────────────────────────

def analyze(n_trials: int = 1000) -> None:
    print(f"\n{'='*62}")
    print(f"  モンテカルロ分析 ({n_trials}回シミュレーション)")
    print(f"{'='*62}")

    horses = build_entries()
    horses = draw_gates(horses)

    win_count: dict[str, int] = {h.name: 0 for h in horses}
    place_count: dict[str, int] = {h.name: 0 for h in horses}
    track_wins: dict[str, dict[str, int]] = {}

    for trial in range(n_trials):
        rng = random.Random(trial * 31337)
        condition = simulate_weather(trial * 7 + 3)
        pace, pace_factor = simulate_pace(horses, rng)

        times = []
        for horse in horses:
            t, last3f = calc_horse_time(horse, condition, pace, pace_factor, rng)
            times.append((horse, t))

        times.sort(key=lambda x: x[1])
        win_count[times[0][0].name] += 1
        for i in range(min(3, len(times))):
            place_count[times[i][0].name] += 1

        ts = condition.track_state
        if ts not in track_wins:
            track_wins[ts] = {h.name: 0 for h in horses}
        track_wins[ts][times[0][0].name] += 1

    # 勝率ランキング
    sorted_wins = sorted(win_count.items(), key=lambda x: -x[1])
    print(f"\n【勝率ランキング (上位10頭)】")
    print(f"  {'馬名':<20} {'勝率':>6}  {'複勝率':>6}")
    for name, wins in sorted_wins[:10]:
        win_pct = wins / n_trials * 100
        place_pct = place_count[name] / n_trials * 100
        print(f"  {name:<20} {win_pct:>5.1f}%  {place_pct:>5.1f}%")

    # 馬場状態別傾向
    print(f"\n【馬場状態別 有力馬】")
    for ts in ["良", "稍重", "重", "不良"]:
        if ts in track_wins:
            total = sum(track_wins[ts].values())
            if total == 0:
                continue
            top = max(track_wins[ts].items(), key=lambda x: x[1])
            print(f"  {ts:>3}: {top[0]}  ({top[1]/total*100:.0f}%)")

    print()


# ──────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "single"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else None

    if mode == "analyze":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
        analyze(n)
    else:
        run_simulation(seed)
