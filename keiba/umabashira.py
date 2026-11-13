"""記録ファイルと netkeiba 5走データから馬柱 HTML を生成する。

使い方: python -m keiba.umabashira keiba/records/20261115_エリザベス女王杯.json \
            keiba/records/20261115_エリザベス女王杯_past.json out.html
"""
import html
import json
import sys

from keiba.engine import EV_THRESHOLD, analyze, harville

FACTOR_JA = {
    'front_runner_bad_pace': '逃げ好走→展開不向き', 'long_layoff': '半年以上休み明け',
    'wins_special_ground': '特殊馬場の勝ち', 'turf_to_outer': '外枠替わり', 'dirt_to_inner': '内枠替わり(ダ)',
    'fillies_to_open': '牝限→混合', 'slow_start': '出遅れ癖', 'hates_kickback': 'キックバック嫌い',
    'stable_concern': '陣営に不安', 'weight_change_15': '馬体重±15kg', 'weight_increase': '斤量増',
    'lucky_last_race': '前走展開恩恵', 'lucky_trip': '前走ロスなし', 'first_long_trip': '初の長距離輸送',
    'bad_direction': '苦手な回り', 'old_no_record': '高齢・実績なし',
    'first_after_gelding': '去勢明け', 'first_after_transfer': '転厩初戦', 'weight_drop': '斤量減',
    'top_jockey_switch': '上位騎手へ乗替', 'jockey_switch': '乗替プラス', 'first_surface': '初芝/初ダ',
    'big_distance_change': '距離大幅変更', 'first_blinker': '初ブリンカー', 'breaking_out': '本格化',
    'hill_23': '坂路23秒台', 'hill_23_extra': '坂路終い重点', 'short_interval_unfulfilled': '詰めて使う',
    'final_work_strong': '最終追い良', 'course_repeater': 'リピーター', 'second_start': '叩き2戦目',
    'fastest_last_unrewarded': '上がり最速届かず', 'good_in_higher_class': '格上で好内容',
    'turf_to_inner': '内枠替わり', 'dirt_to_outer': '外枠替わり(ダ)', 'bloodline_ground': '血統◎馬場',
    'outside_farm': '外厩仕上げ',
}
MARKS = ['◎', '○', '▲', '△', '△', '△', '△']
PACE_JA = {'slow': 'スロー', 'middle': 'ミドル', 'high': 'ハイ'}
BIAS_JA = {'inner': '内有利', 'outer': '外有利', 'front': '前有利', 'closer': '差し有利', 'flat': 'フラット'}

e = html.escape


def past_cell(p):
    if not p:
        return '<td class="past empty"><span>—</span></td>'
    rank = p['rank']
    cls = {'1': 'r1', '2': 'r2', '3': 'r3'}.get(rank, '')
    date, _, place = p['date_place'].partition(' ')
    course = p['course'].split(' ')
    surface_dist = course[0] if course else ''
    time = course[1] if len(course) > 1 else ''
    going = course[2] if len(course) > 2 else ''
    info = p['info'].split(' ')
    field, num, pop = (info + ['', '', ''])[:3]
    jockey = ' '.join(info[3:-1]) if len(info) > 4 else ''
    kg = info[-1] if len(info) > 4 else ''
    pos = p['pos']
    corners, _, rest = pos.partition(' ')
    last3f = rest.split(' ')[0].strip('()') if rest else ''
    bw = rest.split(' ')[1] if len(rest.split(' ')) > 1 else ''
    winner, _, margin = p['winner'].rpartition(' ')
    grade = f'<span class="g g{e(p["grade"])}">{e(p["grade"])}</span>' if p['grade'] else ''
    return f'''<td class="past {cls}">
<div class="p-top"><span class="p-date">{e(date[2:])} {e(place)}</span><span class="p-rank">{e(rank)}</span></div>
<div class="p-race">{e(p["race"])}{grade}</div>
<div class="p-course">{e(surface_dist)} <b>{e(time)}</b> {e(going)}</div>
<div class="p-info">{e(field)} {e(num)} {e(pop)} {e(jockey)} {e(kg)}</div>
<div class="p-pos"><span>{e(corners)}</span> <span class="p-3f">{e(last3f)}</span> {e(bw)}</div>
<div class="p-win">{e(winner)} <span>{e(margin)}</span></div>
</td>'''


def build(record_path, past_path):
    with open(record_path, encoding='utf-8') as f:
        race = json.load(f)
    with open(past_path, encoding='utf-8') as f:
        past = {h['num']: h for h in json.load(f)}
    res = analyze(race)
    p = {int(k): v for k, v in res['win_prob'].items()}
    place = harville(p)['place']
    ranked = sorted(p, key=p.get, reverse=True)
    mark = {k: MARKS[i] for i, k in enumerate(ranked[:len(MARKS)])}
    bets = {b['horses'][0]: b for b in res['bets'] if b['type'] == '単勝'}
    rows = []
    for h in sorted(race['horses'], key=lambda x: x['num']):
        n = h['num']
        pp = past[n]
        chips = ''.join(f'<span class="chip plus">{e(FACTOR_JA.get(k, k))}</span>' for k in h.get('plus', []))
        chips += ''.join(f'<span class="chip minus">{e(FACTOR_JA.get(k, k))}</span>' for k in h.get('minus', []))
        odds = race['odds']['win'].get(str(n))
        bet = bets.get(n)
        bet_html = f'<span class="buy">暫定買い {bet["stake"]}円</span>' if bet else ''
        cells = ''.join(past_cell(x) for x in (pp['past'] + [None] * 5)[:5])
        rows.append(f'''<tr data-num="{n}" data-prob="{p[n]:.5f}">
<td class="waku w{h["gate"]}">{h["gate"]}</td>
<td class="umaban">{n}</td>
<td class="mark">{mark.get(n, '')}</td>
<td class="horse">
  <div class="h-sire">{e(pp["sire"])}</div>
  <div class="h-name">{e(pp["name"])}</div>
  <div class="h-dam">{e(pp["dam"])} <span>（{e(pp["bms"])}）</span></div>
  <div class="h-meta">{e(pp["sexage"])} {e(pp["trainer"])}</div>
  <div class="h-jk"><b>{e(pp["jockey"])}</b> {e(pp["kg"])}kg ・ {e(pp["style"])} ・ {e(pp["interval"])}</div>
</td>
<td class="eval">
  <div class="ev-row"><span class="ev-l">総合</span><span class="ev-v">{res["scores"][n]:g}</span></div>
  <div class="ev-row"><span class="ev-l">勝率</span><span class="ev-v">{p[n] * 100:.1f}%</span></div>
  <div class="ev-row"><span class="ev-l">単ボーダー</span><span class="ev-v">{EV_THRESHOLD / p[n]:.1f}</span></div>
  <div class="ev-row"><span class="ev-l">複ボーダー</span><span class="ev-v">{EV_THRESHOLD / place[n]:.1f}</span></div>
  <div class="ev-row"><span class="ev-l">予想単勝</span><span class="ev-v">{odds if odds else '—'}</span></div>
  {bet_html}
</td>
{cells}
<td class="note">{chips}<p>{e(h.get("notes", ""))}</p></td>
</tr>''')
    bets_txt = '、'.join(f'{b["type"]}{"-".join(map(str, b["horses"]))} {b["stake"]}円' for b in res['bets']) or '見送り'
    with open(__file__.replace('umabashira.py', 'umabashira_template.html'), encoding='utf-8') as f:
        tpl = f.read()
    title, _, rest = race['race'].partition(' ')
    return (tpl.replace('{{ROWS}}', '\n'.join(rows))
            .replace('{{RACE}}', e(race['race']))
            .replace('{{PACE}}', PACE_JA[res['pace']])
            .replace('{{BIAS}}', f'{BIAS_JA[res["bias"]["inner_outer"]]}・{BIAS_JA[res["bias"]["front_back"]]}')
            .replace('{{BETS}}', e(bets_txt))
            .replace('{{STAGE}}', e(race.get('stage', '')))
            .replace('{{PACE_NOTE}}', e(race.get('pace_note', '')))
            .replace('{{BIAS_NOTE}}', e(race.get('bias', {}).get('note', ''))))


if __name__ == '__main__':
    out = build(sys.argv[1], sys.argv[2])
    with open(sys.argv[3], 'w', encoding='utf-8') as f:
        f.write(out)
    print('saved', sys.argv[3])
