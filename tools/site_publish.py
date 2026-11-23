#!/usr/bin/env python3
"""Publish one Meridian slot to the website feed (embassytransport.co.uk/meridian1 and /meridian2).

The pages on the website are fixed shells. Each one reads <desk>/latest.json from this repository when it
opens. This script builds that file from what the engines already write, then commits and pushes it.
No pandas needed. Prices come only from the engine's own files: nothing here invents a level.

Meridian 1, after each slot (06:40, 10:00, 12:00, 16:00):
  python3 tools/site_publish.py m1 --page <feed.py html> --m5 GU5.txt --h1 GU1h.txt --d1 GUD.txt \
      --card main_card_0640.json [--card10 card_1000.json] [--ny ny_main_card.json] --now 06:40 --push
Meridian 2, after each slot:
  python3 tools/site_publish.py m2 --m5 GU5.txt --h1 GU1h.txt --d1 GUD.txt --m2json gu.json \
      --slot 06:40|10:00|12:00 [--note "two sentences"] --now 10:00 --push
Common: --ahead 11 (hours the Twelve Data stamps run ahead of UK time; check the engine's own clock line)
        --date YYYY-MM-DD (default: today, UK)   --result "+10.2" (M1 16:00: the day's pips for the week strip)
Exit code 0 = published. Anything else prints PUBLISH FAILED and the reason.
"""
import argparse, json, os, re, subprocess, sys, html as H
from datetime import datetime, timedelta, date

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- price data
def read_td(path):
    s = open(path, encoding='utf-8').read().strip()
    if s.startswith('{'):
        try: s = json.loads(s).get('result', s)
        except Exception: pass
    rows = []
    for ln in s.splitlines():
        p = ln.strip().split(';')
        if len(p) < 5 or not p[0][:1].isdigit(): continue
        fmt = '%Y-%m-%d %H:%M:%S' if ' ' in p[0] else '%Y-%m-%d'
        rows.append([datetime.strptime(p[0], fmt)] + [float(x) for x in p[1:5]])
    return sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])

def to_uk(rows, ahead):
    return [[r[0] - timedelta(hours=ahead)] + r[1:] for r in rows]

def bucket(rows, minutes, anchor=None):
    """Aggregate bars into fixed buckets of `minutes`, aligned to UK midnight."""
    out = {}
    for t, o, h, l, c in rows:
        m = (t.hour * 60 + t.minute) // minutes * minutes
        k = t.replace(hour=0, minute=0, second=0) + timedelta(minutes=m)
        if k not in out: out[k] = [k, o, h, l, c]
        else: b = out[k]; b[2] = max(b[2], h); b[3] = min(b[3], l); b[4] = c
    return [out[k] for k in sorted(out)]

# ---------------------------------------------------------------- PD arrays (FVGs) and OTE
TFSPAN = {'15m': timedelta(minutes=15), '45m': timedelta(minutes=45), '4H': timedelta(hours=4), 'Daily': timedelta(days=1)}
MINSZ = {'15m': 1.5, '45m': 2, '4H': 2, 'Daily': 2}

def fvgs(rows, tf):
    out = []
    for i in range(2, len(rows)):
        a, c = rows[i - 2], rows[i]
        f = c[0] + TFSPAN[tf]                     # confirmed when the third candle closes
        if c[3] > a[2]: out.append({'tf': tf, 'd': 'bull', 'lo': a[2], 'hi': c[3], 'f': f})
        if c[2] < a[3]: out.append({'tf': tf, 'd': 'bear', 'lo': c[2], 'hi': a[3], 'f': f})
    return out

def pd_arrays(day, b15, s45, s4h, sd, path_rows, pip=1e-4):
    day0 = datetime.combine(day, datetime.min.time())
    T = lambda hm: day0.replace(hour=int(hm[:2]), minute=int(hm[3:]))
    start, end = T(b15[0][0]), T(b15[-1][0])
    r15 = [[T(b[0])] + b[1:5] for b in b15]
    cand = fvgs(r15, '15m') + fvgs(s45, '45m') + fvgs(s4h, '4H') + [g for g in fvgs(sd, 'Daily') if g['f'] <= day0]
    lo_d = min(b[3] for b in b15); hi_d = max(b[2] for b in b15)
    keep, near = [], {}
    for g in cand:
        if g['f'] > end or (g['hi'] - g['lo']) / pip < MINSZ[g['tf']]: continue
        filled = next((t for t, h, l in path_rows if t >= g['f'] and (l <= g['lo'] if g['d'] == 'bull' else h >= g['hi'])), None)
        if filled and filled < start: continue
        mid = (g['lo'] + g['hi']) / 2
        if not (lo_d - 15 * pip <= mid <= hi_d + 15 * pip):
            dist = min(abs(g['lo'] - hi_d), abs(g['hi'] - lo_d))
            if g['tf'] not in near or dist < near[g['tf']][0]: near[g['tf']] = (dist, g)
            continue
        hm = lambda t: None if t is None or t <= start else t.strftime('%H:%M')
        a = {'tf': g['tf'], 'd': g['d'], 'lo': round(g['lo'], 5), 'hi': round(g['hi'], 5), 'from': hm(g['f']), 'to': hm(filled)}
        if not (a['to'] and a['from'] and a['to'] <= a['from']): keep.append(a)
    off = {tf: f"{g['d']} {g['lo']:.5f}-{g['hi']:.5f}" for tf, (_, g) in near.items() if not any(k['tf'] == tf for k in keep)}
    return keep, off

def ote_of(lo_p, hi_p, down, frm, leg):
    """OTE (62-79%, 70.5% mid) of a leg. down=True: the leg fell, the OTE is the retrace up."""
    r = hi_p - lo_p
    if down: a, b, m = lo_p + .62 * r, lo_p + .79 * r, lo_p + .705 * r
    else:    a, b, m = hi_p - .79 * r, hi_p - .62 * r, hi_p - .705 * r
    return {'lo': round(a, 5), 'hi': round(b, 5), 'mid': round(m, 5), 'from': frm, 'leg': leg}

# ---------------------------------------------------------------- page pieces
STAGE = ('<section class="stage" aria-label="Session chart"><header><div><strong id="cPair"></strong><small>15m · UK time</small></div>'
         '<div class="views" id="views"></div></header>\n<svg class="chart" id="chart" viewBox="0 0 760 420" role="img" '
         'aria-label="Candlestick chart of the session with the desk\'s levels"></svg><div class="pdk" id="pdk"></div>'
         '<div class="scap" id="cap"></div></section>')
LV = {'Beginner': 'b', 'Intermediate': 'i', 'Pro': 'p'}

def m1_page_parts(page):
    s = open(page, encoding='utf-8').read()
    tk = re.search(r'<div class="tk-row">.*?</div>\s*</section>', s, re.S)
    tracker = tk.group(0).rsplit('</section>', 1)[0] if tk else ''
    sub = re.search(r'<header class="mast">.*?<p>(.*?)</p>', s, re.S)
    feed = s[s.find('<div class="feed">') + 18: s.find('<section class="gloss') if '<section class="gloss' in s else s.find('</div>\n  <footer')]
    feed = re.sub(r'<div class="vid">.*?</div>\s*</div>', '', feed, flags=re.S)        # media stay on the in-app page
    feed = re.sub(r'<div class="gallery">.*?</div>(?=</div></article>|<)', '', feed, flags=re.S)
    feed = re.sub(r'<(video|img)\b[^>]*>', '', feed)
    def lv(m):
        if m.group(2) not in LV: return m.group(0)
        return f'<div class="nt" data-lv="{LV[m.group(2)]}"><h4>{m.group(1).strip()}</h4>{m.group(3)}</div>'
    feed = re.sub(r'<details[^>]*><summary><span>(.*?)<span class="lvl">(\w+)</span></span></summary><div class="in">(.*?)</div></details>', lv, feed, flags=re.S)
    feed = re.sub(r'((?:<div class="nt".*?</div>){3})', r'<div class="notes">\1</div>', feed, flags=re.S)
    arts = re.findall(r'<article.*?</article>', feed, flags=re.S)
    arts = [a for a in arts if not re.search(r'<div class="alert"[^>]*><b>\s*</b><span>\s*</span>', a)]   # never show empty alerts
    if not arts: raise SystemExit('PUBLISH FAILED: no posts found in the feed page ' + page)
    arts[0] = arts[0][:-len('</div></article>')] + STAGE + '</div></article>'
    return tracker, (sub.group(1).strip() if sub else ''), '\n'.join(arts)

def week_html(title, days):
    return (f'<section class="week"><h3>{title}</h3><div class="wk">'
            + ''.join(f'<div class="{d[2]}"><small>{d[0]}</small><b class="{d[3]}">{d[1]}</b></div>' for d in days) + '</div></section>')

def week_days(store, day):
    mon = day - timedelta(days=day.weekday())
    names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    out = []
    for i, n in enumerate(names):
        k = (mon + timedelta(days=i)).isoformat()
        v = store.get(k)
        out.append([n, v[0] if v else '—', '', v[1] if v else ''])
    return out, mon

def post_html(p):
    o = f'<span class="tag" style="--k:var(--{p["k"]})">{p["tag"]}</span><h2>{p["h"]}</h2>'
    if p.get('lede'): o += f'<p class="lede">{p["lede"]}</p>'
    if p.get('plan'):
        o += (f'<div class="plan"><header><strong>{p["plan"]["title"]}</strong><span class="side {p["plan"]["cls"]}">{p["plan"]["side"]}</span></header><dl>'
              + ''.join(f'<dt>{a}</dt><dd>{b}</dd>' for a, b in p['plan']['rows']) + '</dl></div>')
    if p.get('pools'):
        o += (f'<div class="plan"><header><strong>{p["pools"]["title"]}</strong><span class="side map">DRAW MAP</span></header><div class="pools">'
              + ''.join(f'<div class="pool"><div class="nm">{r[0]}<small>{r[1]}</small></div><div class="meter"><i style="--p:{r[3]}%"></i></div><div class="odds">{r[2]}</div></div>' for r in p['pools']['rows'])
              + '</div></div>')
    for t in p.get('text', []): o += f'<p>{t}</p>'
    if p.get('dim'): o += f'<p class="dim">{p["dim"]}</p>'
    return f'<article class="post"><div class="time">{p["t"]}<small>{p["slot"]}</small></div><div class="body">{o}</div></article>'

# ---------------------------------------------------------------- helpers
px = lambda v, c='': f'<span class="px {c}">{v:.5f}</span>'
esc = lambda s: H.escape(str(s or ''), quote=False)

def prev_slot(hm):   # the last complete 15m bar before hm
    t = datetime.strptime(hm, '%H:%M') - timedelta(minutes=15)
    return f'{t.hour:02d}:{t.minute // 15 * 15:02d}'

def first_touch(b15, price, after, until=None):
    for b in b15:
        if b[0] >= after and (until is None or b[0] <= until) and b[3] <= price <= b[2]: return b[0]
    return None

# ---------------------------------------------------------------- Meridian 1
def m1_views(cards, b15, now):
    levels, views = [], []
    def lv(pref, card):
        ids = []
        t = card.get('trade') or {}
        side = (t.get('side') or card.get('bias') or '').upper()
        add = lambda k, p, lab, c, **kw: (levels.append(dict(id=pref + k, p=float(p), label=lab, c=c, **kw)), ids.append(pref + k))
        if t.get('entry') is not None: add('e', t['entry'], 'Entry', 'green' if side == 'BUY' else 'red', w=1.8)
        if t.get('stop') is not None: add('s', t['stop'], 'Stop', 'red', w=1.8)
        if t.get('target') is not None: add('t1', t['target'], 'Target 1' if t.get('target2') else 'Target', 'blue', w=1.6)
        if t.get('target2') is not None: add('t2', t['target2'], 'Target 2', 'blue', w=1.6)
        for i, z in enumerate((card.get('zones') or [])[:2]):
            if z.get('kind') == 'watch':
                for j, edge in enumerate(('lo', 'hi')):
                    if z.get(edge) is not None and not any(abs(l['p'] - float(z[edge])) < 2e-5 for l in levels if l['id'] in ids):
                        add(f'z{i}{j}', z[edge], 'Zone', 'mute', dash=1)
        d = card.get('draw') or {}
        if d.get('price') is not None and not any(abs(l['p'] - float(d['price'])) < 2e-5 for l in levels if l['id'] in ids):
            add('d', d['price'], 'Draw', 'gold', dash=1)
        return ids
    def path(card):
        try: return [[p[0], float(p[1])] for p in card['scenarios']['A']['path']]
        except Exception: return None
    def cap(card, head):
        t = (card.get('trade') or {}).get('status') or ''
        return f'<b>{head}</b> {esc(card.get("thesis"))}{". " + esc(t) if t else ""}'
    c0, c10, cny = cards
    if c0:
        views.append({'label': '06:40 plan', 'upto': prev_slot('06:40'), 'show': lv('a', c0), 'path': path(c0),
                      'cap': cap(c0, 'How it should play.')})
    if c10 and now >= '10:00':
        views.append({'label': '10:00 call', 'upto': prev_slot('10:00'), 'show': lv('b', c10), 'path': path(c10),
                      'cap': cap(c10, 'The 10:00 call.')})
    if now >= '12:00' and (c0 or c10):
        note = ' '.join(esc(c.get('review_note')) for c in (c0, c10) if c and c.get('review_note'))
        views.append({'label': 'London review', 'upto': min(prev_slot('12:00'), b15[-1][0]),
                      'show': (views[-1]['show'] if views else []), 'cap': '<b>London review.</b> ' + (note or 'See the review post below.')})
    if cny and now >= '12:00':
        views.append({'label': '12:05 NY plan', 'upto': min(prev_slot('12:05'), b15[-1][0]), 'show': lv('n', cny), 'path': path(cny),
                      'cap': cap(cny, 'The New York plan.')})
    if cny and now >= '16:00':
        views.append({'label': 'How it played', 'upto': b15[-1][0], 'show': [i for i in views[-1]['show']],
                      'cap': '<b>New York review.</b> ' + (esc(cny.get('review_note')) or 'See the review post below.')})
    for v in views:
        if not v.get('path'): v.pop('path', None)
    return levels, views

# ---------------------------------------------------------------- Meridian 2
POOL_NAMES = {'PDH': "Yesterday's high", 'PDL': "Yesterday's low", 'PWH': "Last week's high", 'PWL': "Last week's low"}

def m2_slot(slot, J, b15, note, pair):
    dm, kl = J.get('draw_map') or {}, J.get('killzone_leg') or None
    nm = lambda n: POOL_NAMES.get(n, n)
    levels, posts, views = [], [], []
    hhmm = lambda s: str(s)[11:16] if s and len(str(s)) >= 16 else str(s or '')
    if slot == '06:40':
        pools = dm.get('pools') or []
        rows = [[nm(p['name']), f"{p['price']:.5f} · {abs(p['pips']):.1f}p {'above' if p['pips'] > 0 else 'below'}",
                 f"{p['reach']}%" if p.get('reach') is not None else 'n/a', p.get('reach') or 0] for p in pools[:5]]
        for i, p in enumerate(pools[:5]):
            levels.append({'id': f'p{i}', 'p': p['price'], 'label': f"{nm(p['name'])}" + (f" · {p['reach']}%" if p.get('reach') is not None else ''), 'c': 'blue', 'dash': 1})
        near = next((p for p in pools if abs(p['pips']) >= 10), pools[0] if pools else None)
        h = (f"The nearest real draw is {nm(near['name']).lower() if near['name'] in POOL_NAMES else 'the ' + near['name']}, "
             f"{abs(near['pips']):.0f} pips {'above' if near['pips'] > 0 else 'below'}." if near else 'No pools within reach this morning.')
        posts.append({'t': '06:40', 'slot': 'Morning map', 'tag': 'Morning map', 'k': 'blue', 'h': h,
                      'lede': 'Before London opens, this is where the resting orders are and how often price has gone to get them by noon.',
                      'pools': {'title': f"{pair} · {dm.get('price', 0):.5f}", 'rows': rows},
                      'text': [esc(note)] if note else [], 'dim': 'Watching only. Handover at 10:00.'})
        views.append({'label': '06:40 map', 'upto': prev_slot('06:40'), 'show': [l['id'] for l in levels],
                      'cap': '<b>The map at 06:40.</b> Pools above and below, with how often a pool at that distance has been reached by noon.'})
    if slot == '10:00' and kl:
        levels.append({'id': 'eq', 'p': kl['eq'], 'label': 'Equilibrium', 'c': 'copper', 'w': 2.2, 'from': hhmm(kl['to_at'])})
        f_at, t_at = hhmm(kl['from_at']), hhmm(kl['to_at'])
        side = 'above' if kl['eq_dist'] > 0 else 'below'
        rows = [['Leg', f"{px(kl['from'])} at {f_at} → {px(kl['to'])} at {t_at} · {kl['size']:.1f} pips {kl['dir']}"],
                ['Equilibrium', f"{px(kl['eq'], 'c')} · {abs(kl['eq_dist']):.1f} pips {side} price"
                 + (f" · returns by noon on <b>{kl['reach']}%</b> of sessions" if kl.get('reach') else '')]]
        obs = (J.get('setup') or {})
        if obs: rows.append(['Swept', f"{esc(obs.get('swept'))} {px(obs['level'])}, {obs.get('depth_pips', 0):.0f} pips past"])
        posts.append({'t': '10:00', 'slot': 'Handover', 'tag': 'Killzone handover', 'k': 'copper',
                      'h': f"A {kl['size']:.0f}-pip leg {kl['dir']}. Equilibrium is {kl['eq']:.5f}.",
                      'plan': {'title': pair, 'cls': 'map', 'side': 'HANDOVER', 'rows': rows},
                      'text': [esc(note)] if note else []})
        views.append({'label': '10:00 handover', 'upto': prev_slot('10:00'), 'show': ['eq'],
                      'leg': [[f_at, kl['from']], [t_at, kl['to']]],
                      'cap': f"<b>The handover at 10:00.</b> A {kl['size']:.1f}-pip leg {kl['dir']} from {kl['from']:.5f} to {kl['to']:.5f}. Equilibrium {kl['eq']:.5f}."})
    if slot == '12:00':
        posts.append({'t': '12:00', 'slot': 'Scorecard', 'tag': 'Scorecard', 'k': 'gold', 'h': '', 'plan': None, 'text': [esc(note)] if note else []})
    return levels, posts, views

def m2_scorecard(state, b15, pair):
    """Fill the 12:00 scorecard from the bars: EQ revisited after 10:00? nearest morning draw reached by noon?"""
    kl = state.get('kl'); pools = state.get('pools') or []
    rows, eq_hit = [], None
    if kl:
        eq_hit = first_touch(b15, kl['eq'], '10:00', '11:45')
        rows.append([f"Equilibrium {kl['eq']:.5f}", f'<span class="g">Reached</span> at {eq_hit}' if eq_hit else '<span class="r">Not revisited</span> after 10:00'])
    for p in pools[:3]:
        hit = first_touch(b15, p['price'], '06:45', '11:45')
        rows.append([f"{POOL_NAMES.get(p['name'], p['name'])} {p['price']:.5f}", f'<span class="g">Reached</span> at {hit}' if hit else '<span class="r">Not reached</span>'])
    rows.append(['Trades called', 'None. This desk names levels; it does not call a side.'])
    head = ('Equilibrium came back by noon.' if eq_hit else 'No return to equilibrium by noon.') if kl else 'Scorecard'
    return head, rows, (eq_hit is not None) if kl else None

# ---------------------------------------------------------------- git
def git(*a, check=True):
    r = subprocess.run(['git', '-C', REPO] + list(a), capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError(' '.join(a) + ': ' + (r.stderr or r.stdout)[-400:])
    return r

def push(msg):
    git('add', '-A')
    if not git('status', '--porcelain', check=False).stdout.strip(): return 'nothing to commit'
    git('-c', 'user.name=Claude', '-c', 'user.email=noreply@anthropic.com', 'commit', '-qm', msg)
    for _ in range(3):
        r = git('-c', 'push.negotiate=true', 'push', 'origin', 'HEAD:main', check=False)
        if r.returncode == 0: return 'pushed'
        git('pull', '-q', '--rebase', 'origin', 'main', check=False)
    raise RuntimeError('push rejected 3 times: ' + (r.stderr or '')[-300:])

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('desk', choices=['m1', 'm2'])
    ap.add_argument('--m5', required=True); ap.add_argument('--h1'); ap.add_argument('--d1')
    ap.add_argument('--ahead', type=float, default=11.0)
    ap.add_argument('--date'); ap.add_argument('--now', required=True)
    ap.add_argument('--page'); ap.add_argument('--card'); ap.add_argument('--card10'); ap.add_argument('--ny')
    ap.add_argument('--m2json'); ap.add_argument('--slot'); ap.add_argument('--note', default='')
    ap.add_argument('--result', help='M1: the day\'s pips for the week strip, e.g. "+10.2" or "0 · no fill"')
    ap.add_argument('--push', action='store_true'); ap.add_argument('--dry', action='store_true')
    A = ap.parse_args()
    day = date.fromisoformat(A.date) if A.date else datetime.utcnow().date()
    day0 = datetime.combine(day, datetime.min.time())
    m5 = to_uk(read_td(A.m5), A.ahead)
    if not m5: raise SystemExit('PUBLISH FAILED: no bars in ' + A.m5)
    today = [r for r in m5 if r[0].date() == day]
    if not today: raise SystemExit(f'PUBLISH FAILED: no bars for {day} after shifting by {A.ahead}h; last bar {m5[-1][0]}')
    cut = day0.replace(hour=int(A.now[:2]), minute=int(A.now[3:]))
    b = [x for x in bucket(today, 15) if x[0] + timedelta(minutes=15) <= cut]     # complete bars only
    if len(b) < 8: raise SystemExit(f'PUBLISH FAILED: only {len(b)} complete 15m bars before {A.now}')
    b15 = [[x[0].strftime('%H:%M'), round(x[1], 5), round(x[2], 5), round(x[3], 5), round(x[4], 5)] for x in b]
    hist = [r for r in m5 if r[0] < day0]
    s45 = bucket([r for r in m5 if r[0] >= day0 - timedelta(days=4) and r[0] < cut], 45)
    s45 = [x for x in s45 if x[0] + timedelta(minutes=45) <= cut]
    h1 = to_uk(read_td(A.h1), A.ahead) if A.h1 else []
    s4h = [x for x in bucket([r for r in h1 if r[0] < cut], 240) if x[0] + timedelta(hours=4) <= cut]
    sd = [[r[0]] + r[1:] for r in read_td(A.d1)] if A.d1 else []      # daily stamps are dates; formation checked against the day
    path_rows = [[r[0], r[2], r[3]] for r in bucket(hist[-2000:], 15)] + [[day0.replace(hour=int(x[0][:2]), minute=int(x[0][3:])), x[2], x[3]] for x in b15]
    arrays, off = pd_arrays(day, b15, s45, s4h, sd, path_rows)
    zones = [['00:00', '07:00', '#6b95ff', 'ASIA'], ['07:00', '10:00', '#d08a4f', 'KILLZONE']]
    D = {'date': day.strftime('%A %-d %B %Y'), 'updated': A.now}
    folder = os.path.join(REPO, A.desk); os.makedirs(folder, exist_ok=True)
    store_p = os.path.join(folder, 'week.json')
    store = json.load(open(store_p)) if os.path.exists(store_p) else {}

    if A.desk == 'm1':
        load = lambda p: json.load(open(p)) if p and os.path.exists(p) else None
        c0, c10, cny = load(A.card), load(A.card10), load(A.ny)
        if not (c0 or c10 or cny): raise SystemExit('PUBLISH FAILED: no card file found')
        pair = (cny or c10 or c0).get('pair', '')          # the chart follows the newest call's pair
        c0, c10, cny = [c if c and c.get('pair', pair) == pair else None for c in (c0, c10, cny)]
        levels, views = m1_views((c0, c10, cny), b15, A.now)
        if not views: raise SystemExit('PUBLISH FAILED: no chart view could be built from the cards')
        tracker, sub, feed = m1_page_parts(A.page)
        until = '16:00'; zones.append(['13:30', '16:00', '#22c55e', 'NEW YORK'])
        ote = (c10 or c0 or {}).get('ote')      # optional: {lo, hi, mid, leg} from pdarrays.py
        k = day.isoformat()
        if A.result: store[k] = [A.result, 'g' if A.result.strip().startswith('+') else 'r' if A.result.strip().startswith(('−', '-')) else '']
        elif k not in store: store[k] = ['live', '']
        days, mon = week_days(store, day)
        D.update({'sub': sub or f'{D["date"]} · {pair}', 'tracker': tracker, 'feed': feed,
                  'week': week_html(f'Meridian 1 · week of {mon.strftime("%-d %b")} · pips on the feed', days + [['Week', '—', 'tot', '']])})
    else:
        if not (A.m2json and A.slot): raise SystemExit('PUBLISH FAILED: m2 needs --m2json and --slot')
        J = json.load(open(A.m2json)); pair = J.get('pair', 'GBP/USD')
        st_p = os.path.join(folder, f'state-{day.isoformat()}.json')
        S = json.load(open(st_p)) if os.path.exists(st_p) else {'levels': [], 'posts': [], 'views': [], 'pools': [], 'kl': None}
        levels, posts, views = m2_slot(A.slot, J, b15, A.note, pair)
        if A.slot == '06:40': S['pools'] = (J.get('draw_map') or {}).get('pools') or []
        if A.slot == '10:00': S['kl'] = J.get('killzone_leg')
        S['posts'] = [p for p in S['posts'] if p['t'] != A.slot]; S['views'] = [v for v in S['views'] if not v['label'].startswith(A.slot)]
        keep = {l['id'] for v in views for l in levels}; S['levels'] = [l for l in S['levels'] if l['id'] not in keep] + levels
        if A.slot == '12:00':
            head, rows, eq_ok = m2_scorecard(S, b15, pair)
            posts[0].update({'h': head, 'plan': {'title': pair, 'cls': 'res', 'side': 'SCORECARD', 'rows': rows}})
            views = [{'label': '12:00 scorecard', 'upto': b15[-1][0], 'show': [l['id'] for l in S['levels']],
                      'cap': '<b>The scorecard.</b> ' + head}]
            if S['views'] and S['views'][-1].get('leg'): views[0]['leg'] = S['views'][-1]['leg']
            if eq_ok is not None: store[day.isoformat()] = ['yes' if eq_ok else 'no', 'g' if eq_ok else 'r']
        S['posts'] += posts; S['views'] += views
        S['posts'].sort(key=lambda p: p['t'])
        json.dump(S, open(st_p, 'w'), separators=(',', ':'))
        levels, views = S['levels'], S['views']
        arts = [post_html(p) for p in S['posts']]
        arts[0] = arts[0][:-len('</div></article>')] + STAGE + '</div></article>'
        until = '12:00'; ote = None
        if S.get('kl'):
            kl = S['kl']; ote = ote_of(kl['lo'], kl['hi'], kl['dir'] == 'down', str(kl['to_at'])[11:16], f"{kl['from']:.5f} to {kl['to']:.5f}")
        if day.isoformat() not in store: store[day.isoformat()] = ['live', '']
        days, mon = week_days(store, day)
        D.update({'sub': f'{D["date"]} · {pair}. Watches the London killzone so you do not have to trade it: a map at 06:40, '
                         'the leg and its equilibrium at 10:00, a scorecard at noon. It never calls a trade.',
                  'feed': '\n'.join(arts),
                  'week': week_html(f'Meridian 2 · week of {mon.strftime("%-d %b")} · equilibrium reached by noon', days + [['Week', '—', 'tot', '']])})

    bars = [list(x) for x in b15]
    h, m = map(int, bars[-1][0].split(':')); c = bars[-1][4]
    while True:                       # carry the time axis to the session end; views stop at their own 'upto'
        m += 15; h, m = h + m // 60, m % 60; t = f'{h:02d}:{m:02d}'
        if t > until: break
        bars.append([t, c, c, c, c])
    for v in views: v['upto'] = min(v['upto'], b15[-1][0])
    D['chart'] = {'pair': pair, 'bars': bars, 'zones': zones, 'levels': levels, 'views': views,
                  'replay': 'The 15-minute picture as it stood at the first post, then the day printed candle by candle.',
                  'arrays': arrays, 'ote': ote, 'off': off}
    out = json.dumps(D, ensure_ascii=False, separators=(',', ':'))
    json.dump(store, open(store_p, 'w'), separators=(',', ':'))
    for name in ('latest.json', f'{day.isoformat()}.json'):
        open(os.path.join(folder, name), 'w', encoding='utf-8').write(out)
    print(f'{A.desk} {day} {A.now}: {len(b15)} bars to {b15[-1][0]}, {len(views)} views, {len(arrays)} arrays, {round(len(out)/1024,1)} KB')
    if A.push and not A.dry:
        print(push(f'{A.desk} {day} {A.now}'))

if __name__ == '__main__':
    try: main()
    except SystemExit as e:
        if e.code not in (0, None): print(e.code if isinstance(e.code, str) else 'PUBLISH FAILED'); sys.exit(1)
    except Exception as e:
        print('PUBLISH FAILED:', type(e).__name__, e); sys.exit(1)
