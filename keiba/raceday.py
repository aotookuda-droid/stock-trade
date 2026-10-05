"""発走10分前の最終提案。判断時刻の直前に netkeiba の実オッズを取得して engine で買い目を出す。

使い方: python3 -m keiba.raceday keiba/records/20261004_京都大賞典.json keiba/records/ledger_20261003.json

- 判断時刻（発走10分前）の FETCH_LEAD 前まで待ってからオッズを取得する（待ちが長すぎるときは止まる）。
- 判断時刻を過ぎていたら、後のオッズは使わず見送り（0円）として台帳に記録する。
- 判断済みのレースは何も上書きしない。台帳ロックで二重実行を防ぎ、保存途中で止まった場合は次回に台帳を補う。
- 三連単が全組合せ揃わなければ判断時刻まで取り直し、それでも欠ければ市場側を単勝で代用したと表示する。
- netkeiba が取消・除外と明示した馬は scratched に加える。それ以外で単勝が欠けた馬がいれば提案を出さずに止まる。
- 馬券は購入しない。
"""
import sys
import time
from datetime import datetime, timedelta

from keiba import engine, live_card

FETCH_LEAD = timedelta(minutes=2, seconds=30)
MAX_WAIT = timedelta(minutes=9)
TRIFECTA_RETRIES = 2
RETRY_PAUSE = timedelta(seconds=10)
ODDS_PAGE = 'https://race.netkeiba.com/odds/index.html?race_id={}'


def _now():
    return datetime.now().astimezone()


def run(record_path, ledger_path, now=_now, sleep=time.sleep, fetch=live_card.fetch_odds):
    with engine.ledger_lock(ledger_path):
        race, ledger = engine._read_json(record_path), engine._read_json(ledger_path)
        try:
            engine.guard(race, ledger, ledger_path)
        except engine.AlreadyDecided as e:
            print(e)
            return 'already'
        return _run(race, ledger, record_path, ledger_path, now, sleep, fetch)


def _fetch_complete(race, now, decision, sleep, fetch):
    """オッズを取得する。三連単が出走馬の全組合せで揃わなければ、判断時刻まで最大 TRIFECTA_RETRIES 回取り直す。"""
    for attempt in range(TRIFECTA_RETRIES + 1):
        got = fetch(race['race_id'])
        if not got['odds']:
            return got, None
        scratched = set(race.get('scratched', [])) | set(got['scratched'])
        nums = [h['num'] for h in race['horses'] if h['num'] not in scratched]
        missing = engine.sanrentan_missing(got['odds'], nums)
        if not missing or attempt == TRIFECTA_RETRIES or now() + RETRY_PAUSE >= decision:
            return got, missing
        print(f"三連単オッズが {missing} 組不足。取り直す（{attempt + 1}回目）")
        sleep(RETRY_PAUSE.total_seconds())
    return got, missing


def _codex_probs(race, scratched, decision):
    """Codex版の勝率を答え合わせ用に記録する（買い目には使わない）。作成元・モデルID・確定時刻・採用しない理由は
    race['codex_meta'] に残す。独立したCodex予測は 'codex'、Claude作成など独立でないものは 'codex_reference' に入れる。"""
    running = [h['num'] for h in race['horses'] if h['num'] not in scratched]
    codex = live_card.load_codex(race['race_id'], scratched, decision, running)
    if not codex:
        return {}
    race['codex_meta'] = {k: codex[k] for k in ('file', 'author', 'model_id', 'model_status', 'independent', 'kind',
                                                'prep_status', 'frozen_at', 'reason', 'scratch_note')}
    return {codex['kind']: codex['win']} if codex['win'] else {}


def _run(race, ledger, record_path, ledger_path, now, sleep, fetch):
    decision = datetime.fromisoformat(race['start_at']) - engine.DECISION_BEFORE
    target = decision - FETCH_LEAD
    if now() > decision:
        engine.commit(race, record_path, ledger, ledger_path, 0, note='判断時刻超過のため見送り')
        print(f"見送り（判断時刻 {decision:%H:%M} を過ぎたため。後のオッズは使わない）")
        return 'missed'
    wait = target - now()
    if wait > MAX_WAIT:
        print(f"まだ早い: {target:%H:%M:%S} 以降に再実行する（あと{int(wait.total_seconds() // 60)}分）")
        return 'early'
    if wait.total_seconds() > 0:
        sleep(wait.total_seconds())
    got, tri_missing = _fetch_complete(race, now, decision, sleep, fetch)
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
                         'official_at': got['asof'], 'fetched_at': got['fetched_at'],
                         'sanrentan_missing': tri_missing}
    res = engine.finalize(race, record_path, ledger, ledger_path, extra_probs=_codex_probs(race, scratched, decision))
    print(engine.format_result(res))
    if res['market_source'] != 'sanrentan':
        print(f"※ 三連単オッズが揃わず、市場側を単勝オッズで代用した（不足 {res['sanrentan_missing']}組）")
    return 'decided'


if __name__ == '__main__':
    run(*sys.argv[1:3])
