"""発走60分前の最終提案。判断時刻の直前に netkeiba の実オッズを取得して engine で買い目を出す。

使い方: python3 -m keiba.raceday keiba/records/20261004_京都大賞典.json keiba/records/ledger_20261003.json

- 判断時刻（発走60分前）の FETCH_LEAD 前まで待ってからオッズを取得する（待ちが長すぎるときは止まる）。
- 判断時刻を過ぎていたら、後のオッズは使わず見送り（0円）として台帳に記録する。
- netkeiba が取消・除外と明示した馬は scratched に加える。それ以外で単勝が欠けた馬がいれば提案を出さずに止まる。
- 馬券は購入しない。
"""
import json
import sys
import time
from datetime import datetime, timedelta

from keiba import engine, live_card

FETCH_LEAD = timedelta(minutes=2, seconds=30)
MAX_WAIT = timedelta(minutes=9)
ODDS_PAGE = 'https://race.netkeiba.com/odds/index.html?race_id={}'


def _now():
    return datetime.now().astimezone()


def run(record_path, ledger_path, now=_now, sleep=time.sleep, fetch=live_card.fetch_odds):
    with open(record_path, encoding='utf-8') as f:
        race = json.load(f)
    decision = datetime.fromisoformat(race['start_at']) - engine.DECISION_BEFORE
    target = decision - FETCH_LEAD
    if now() > decision:
        with open(ledger_path, encoding='utf-8') as f:
            ledger = json.load(f)
        engine.record_decision(ledger, race['race_id'], 0, now().isoformat(timespec='seconds'))
        for e in ledger['races']:
            if e['race_id'] == race['race_id']:
                e['note'] = '判断時刻超過のため見送り'
        with open(ledger_path, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
            f.write('\n')
        print(f"見送り（判断時刻 {decision:%H:%M} を過ぎたため。後のオッズは使わない）")
        return 'missed'
    wait = target - now()
    if wait > MAX_WAIT:
        print(f"まだ早い: {target:%H:%M:%S} 以降に再実行する（あと{int(wait.total_seconds() // 60)}分）")
        return 'early'
    if wait.total_seconds() > 0:
        sleep(wait.total_seconds())
    got = fetch(race['race_id'])
    if not got['odds']:
        raise SystemExit('単勝オッズを取得できなかった。提案は出さない')
    scratched = sorted(set(race.get('scratched', [])) | set(got['scratched']))
    missing = [h['num'] for h in race['horses']
               if h['num'] not in scratched and str(h['num']) not in got['odds']['win']]
    if missing:
        raise SystemExit(f'単勝オッズが欠けた馬がいる（取消の明示なし）: {missing}。提案は出さない')
    race['scratched'] = scratched
    race['odds'] = got['odds']
    race['odds_meta'] = {'kind': 'observed', 'source': ODDS_PAGE.format(race['race_id']) + '（netkeiba オッズAPI）',
                         'official_at': got['asof'], 'fetched_at': got['fetched_at']}
    with open(record_path, 'w', encoding='utf-8') as f:
        json.dump(race, f, ensure_ascii=False, indent=2)
        f.write('\n')
    engine.main(record_path, ledger_path)
    return 'decided'


if __name__ == '__main__':
    run(*sys.argv[1:3])
