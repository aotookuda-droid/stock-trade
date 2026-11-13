"""前日時点のモデル確率から、当日「この倍率以上なら期待値1.2を超える」ボーダーを出す。

使い方: python -m keiba.borders keiba/records/20261115_エリザベス女王杯.json [上位頭数]
単勝は市場とのブレンドで確率自体がオッズに依存するため目安。最終判断は必ず engine.py で行う。
"""
import json
import sys
from itertools import combinations

from keiba.engine import EV_THRESHOLD, analyze, harville


def main(path, top=7):
    with open(path, encoding='utf-8') as f:
        race = json.load(f)
    res = analyze(race)
    p = {int(k): v for k, v in res['win_prob'].items()}
    names = {h['num']: h['name'] for h in race['horses']}
    probs = harville(p)
    ranked = sorted(p, key=p.get, reverse=True)
    print('馬番 馬名           総合点  勝率   単勝ボーダー 複勝ボーダー(下限)')
    for k in ranked:
        print(f"{k:>3} {names[k]:<12} {res['scores'][k]:>5}  {p[k]:.3f}  {EV_THRESHOLD / p[k]:>8.1f}  {EV_THRESHOLD / probs['place'][k]:>8.1f}")
    head = ranked[:top]
    print(f'\n上位{top}頭の組み合わせボーダー（ワイドは下限オッズで判定）')
    rows = []
    for a, b in combinations(sorted(head), 2):
        rows.append((probs['umaren'][(a, b)], a, b))
    for pr, a, b in sorted(rows, reverse=True):
        print(f"{a:>2}-{b:<2} 馬連 {EV_THRESHOLD / pr:>6.1f}  ワイド {EV_THRESHOLD / probs['wide'][(a, b)]:>5.1f}")


if __name__ == '__main__':
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 7)
