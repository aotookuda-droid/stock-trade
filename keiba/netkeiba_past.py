"""netkeiba の5走馬柱ページ（shutuba_past）を取得・解析して JSON に保存する。

使い方: python -m keiba.netkeiba_past 202608050411 keiba/records/20261115_エリザベス女王杯_past.json
"""
import html as H
import json
import re
import sys
import subprocess

URL = 'https://race.netkeiba.com/race/shutuba_past.html?race_id={}'


def fetch(race_id):
    # urllib はこの環境のプロキシ経由で 400 になるため curl を使う
    out = subprocess.run(['curl', '-sSf', '-A', 'Mozilla/5.0', URL.format(race_id)],
                         capture_output=True, check=True, timeout=60)
    return out.stdout.decode('utf-8', 'replace')


def _txt(s):
    return re.sub(r'\s+', ' ', H.unescape(re.sub(r'<[^>]+>', ' ', s))).strip()


def parse(src):
    out = []
    for r in re.split(r'<tr class="HorseList"', src)[1:]:
        tds = re.findall(r'<td class="(Waku\d*)">\s*(\d+)\s*</td>', r)
        if len(tds) < 2:
            continue

        def g(c):
            m = re.search(r'class="%s[^"]*">(.*?)</div>' % c, r, re.S)
            return _txt(m.group(1)) if m else ''
        style, _, interval = g('Horse06').partition(' ')
        h = {'gate': int(tds[0][1]), 'num': int(tds[1][1]),
             'sire': g('Horse01'), 'name': g('Horse02'), 'dam': g('Horse03'), 'bms': g('Horse04').strip('()'),
             'trainer': g('Horse05'), 'style': style, 'interval': interval,
             'sexage': _txt(re.search(r'class="Barei">(.*?)<', r).group(1)),
             'jockey': _txt(re.search(r'class="Jockey">.*?<a [^>]*>(.*?)</a>', r, re.S).group(1)),
             'kg': re.search(r'class="Jockey">.*?<span>([\d.]+)</span>', r, re.S).group(1), 'past': []}
        for p in re.findall(r'<td class="Past[^"]*"[^>]*>(.*?)</td>', r, re.S):
            if 'Data01' not in p:
                h['past'].append(None)
                continue

            def d(c):
                m = re.search(r'class="%s">(.*?)</div>' % c, p, re.S)
                return _txt(m.group(1)) if m else ''
            d1 = re.search(r'class="Data01">\s*<span>(.*?)</span>\s*<span class="Num">(.*?)</span>', p, re.S)
            gr = re.search(r'Icon_GradeType\d+">(.*?)<', p)
            name = _txt(re.sub(r'<span class="Icon_GradeType.*?</span>', '',
                               re.search(r'class="Data02">(.*?)</div>', p, re.S).group(1)))
            h['past'].append({'date_place': _txt(d1.group(1)), 'rank': _txt(d1.group(2)), 'race': name,
                              'grade': gr.group(1) if gr else '', 'course': d('Data05'), 'info': d('Data03'),
                              'pos': d('Data06'), 'winner': d('Data07')})
        out.append(h)
    return out


if __name__ == '__main__':
    data = parse(fetch(sys.argv[1]))
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f'{len(data)}頭 保存: {sys.argv[2]}')
