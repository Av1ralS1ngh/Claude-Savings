"""
Generate apps/api/decompute_gateway/dashboard_template.html from the boss's exact
design file (decompute-dashboard.html), injecting only the dynamic hooks the
gateway route fills in. Re-run this whenever the source design changes.

This keeps the design byte-for-byte (logo, CSS, glyphs, layout) and only:
  - swaps hardcoded sample values for __TOKENS__ the route replaces
  - gives stat/delta/chart/list nodes stable ids the JS targets
  - adds the 5th time tab ("all")
  - replaces the mock range-switch JS with real per-range data + applyRange()
"""
import re
import sys

SRC = "/Users/avii/Downloads/decompute-dashboard.html"
DST = "/Users/avii/Documents/Claude-Coder/apps/api/decompute_gateway/dashboard_template.html"

o = open(SRC, encoding="utf-8").read()

def sub(pattern, repl, *, count=1, flags=0, label=""):
    global o
    o, n = re.subn(pattern, repl, o, count=count, flags=flags)
    assert n == count, f"[{label}] expected {count} replacement(s), got {n}"
    return n

def replace_str(old, new, *, label="", count=None):
    global o
    actual = o.count(old)
    if count is not None:
        assert actual == count, f"[{label}] expected {count} of {old!r}, found {actual}"
    assert actual >= 1, f"[{label}] {old!r} not found"
    o = o.replace(old, new)

# ---- sidebar: project + user (anchored so we never touch the base64 logo) ----
replace_str('acme-eng', '__PROJECT_NAME__', label='project-name')          # pname + crumb
sub(r'<span class="avatar">AC</span>',
    '<span class="avatar">__PROJECT_INITIALS__</span>', label='project-initials')
sub(r'Members <span class="badge">24</span>',
    'Members <span class="badge">__MEMBERS__</span>', label='members')
sub(r'<div class="user-av">JD</div>',
    '<div class="user-av">__USER_INITIALS__</div>', label='user-initials')
sub(r'<div class="un">Jordan Davies</div>',
    '<div class="un">__USER_NAME__</div>', label='user-name')
sub(r'<div class="ue">jordan@acme\.io</div>',
    '<div class="ue">__USER_EMAIL__</div>', label='user-email')

# ---- stat card values (ids already present in the design) ----
for vid, tok in [('vLatest', '__LATEST__'), ('vMedian', '__MEDIAN__'),
                 ('vSaved', '__SAVED_USD__'), ('vCost', '__COST_USD__')]:
    sub(rf'(<div class="value" id="{vid}">)[^<]*(</div>)', rf'\g<1>{tok}\g<2>', label=f'val-{vid}')

# ---- stat deltas: add ids + tokens, in document order ----
_delta_seq = iter([('dLatest', '__LATEST_DELTA__'), ('dMedian', '__MEDIAN_DELTA__'),
                   ('dSaved', '__SAVED_DELTA__'), ('dCost', '__COST_DELTA__')])
def _delta_repl(_m):
    did, tok = next(_delta_seq)
    return f'<span class="delta up" id="{did}">{tok}</span>'
sub(r'<span class="delta up">.*?</span>', _delta_repl, count=4, label='deltas')

# ---- workload table: meta + tbody ----
sub(r'<span class="meta">[^<]*</span>',
    '<span class="meta" id="wlMeta">__WL_META__</span>', label='wl-meta')   # the no-style one
sub(r'<tbody>.*?</tbody>',
    '<tbody id="wlBody">__WORKLOAD_ROWS__</tbody>', flags=re.DOTALL, label='wl-body')

# ---- capabilities grid (4 nested cap-cards -> single token) ----
sub(r'(<div class="cap-grid">).*?(</div>\s*</div>\s*</div>\s*<!-- LIVE REQUESTS -->)',
    r'\g<1>__CAP_CARDS__\g<2>', flags=re.DOTALL, label='cap-grid')

# ---- requests feed: meta + list ----
sub(r'<span class="meta" style="font-family:var\(--mono\);font-size:11px;color:var\(--dim\)">[^<]*</span>',
    '<span class="meta" id="reqMeta" style="font-family:var(--mono);font-size:11px;color:var(--dim)">__REQ_META__</span>',
    label='req-meta')
sub(r'(<div class="req-list">)(.*?)(</div>\s*</div>\s*<!-- LOCAL SETUP -->)',
    r'<div class="req-list" id="reqList">__REQUEST_ROWS__\g<3>', flags=re.DOTALL, label='req-list')

# ---- time-range labels that update on tab change ----
sub(r'<span class="right">last 24h</span>',
    '<span class="right" id="rangeLabel">__RANGE_LABEL__</span>', label='range-label')
sub(r'<p>Tokens compressed vs sent[^<]*</p>',
    '<p>Tokens compressed vs sent &#183; <span id="rangeLabelLong">__RANGE_LABEL_LONG__</span></p>',
    label='range-label-long')

# ---- big chart: data paths + end dot become dynamic ----
sub(r'<path class="area-saved" d="[^"]*"/>',
    '<path id="chSavedArea" class="area-saved" d="__CH_SAVED_AREA_D__"/>', label='ch-saved-area')
sub(r'<path class="line-saved" d="[^"]*"/>',
    '<path id="chSavedLine" class="line-saved" d="__CH_SAVED_LINE_D__"/>', label='ch-saved-line')
sub(r'<path class="area-sent" d="[^"]*"/>',
    '<path id="chSentArea" class="area-sent" d="__CH_SENT_AREA_D__"/>', label='ch-sent-area')
sub(r'<path class="line-sent" d="[^"]*"/>',
    '<path id="chSentLine" class="line-sent" d="__CH_SENT_LINE_D__"/>', label='ch-sent-line')
sub(r'<circle class="chart-dot" cx="[^"]*" cy="[^"]*"/>',
    '<circle id="chDot" class="chart-dot" cx="__CH_DOT_X__" cy="__CH_DOT_Y__"/>', label='ch-dot')

# ---- chart x-axis tick labels become real, range-aware ages (were hardcoded clock times) ----
for _k, _x in enumerate((40, 220, 400, 580, 730)):
    sub(rf'<text class="axis-label" x="{_x}" y="238">[^<]*</text>',
        f'<text id="chAxis{_k}" class="axis-label" x="{_x}" y="238">__CH_AXIS_{_k}__</text>',
        label=f'axis-{_k}')

# ---- align the Y-scale to the data (was 100%=y40 while data plotted 100%=y20, so the
#      line overshot the 100% gridline) and add a 0% baseline. yf(pct)=220-(pct/100)*200
#      => 100%->y20, 75%->y70, 50%->y120, 25%->y170, 0%->y220. Labels sit 4px above each line.
_GRID = "\n            ".join(
    f'<line class="grid-line" x1="0" y1="{y}" x2="800" y2="{y}"/>' for y in (20, 70, 120, 170, 220))
sub(r'<line class="grid-line" x1="0" y1="40" x2="800" y2="40"/>\s*'
    r'<line class="grid-line" x1="0" y1="100" x2="800" y2="100"/>\s*'
    r'<line class="grid-line" x1="0" y1="160" x2="800" y2="160"/>\s*'
    r'<line class="grid-line" x1="0" y1="220" x2="800" y2="220"/>',
    _GRID, label='chart-gridlines')
_YLABELS = "\n            ".join(
    f'<text class="axis-label" x="0" y="{y - 4}">{pct}%</text>'
    for pct, y in ((100, 20), (75, 70), (50, 120), (25, 170), (0, 220)))
sub(r'<text class="axis-label" x="0" y="36">100%</text>\s*'
    r'<text class="axis-label" x="0" y="96">75%</text>\s*'
    r'<text class="axis-label" x="0" y="156">50%</text>\s*'
    r'<text class="axis-label" x="0" y="216">25%</text>',
    _YLABELS, label='chart-ylabels')

# ---- status pill reflects real health (was always-green hardcoded text) ----
sub(r'<span class="status-pill">all systems operational</span>',
    '<span class="status-pill __STATUS_CLASS__">__STATUS_TEXT__</span>', label='status-pill')
sub(r'(\.status-pill::before\{[^}]*\})',
    r'\g<1>\n  .status-pill.degraded{color:#9a6700;background:#fff4d6}'
    r'\n  .status-pill.degraded::before{background:var(--orange);box-shadow:0 0 0 3px rgba(255,149,0,.2)}',
    label='status-pill-css')

# ---- add the 5th time tab ("all") ----
sub(r'<button class="time-tab" data-range="30d">30d</button>',
    '<button class="time-tab" data-range="30d">30d</button>\n'
    '            <button class="time-tab" data-range="all">all</button>', label='tab-all')

# ---- give the inline script a CSP nonce slot (strict CSP blocks bare inline JS) ----
sub(r'<script>', '<script nonce="__SCRIPT_NONCE__">', label='script-nonce')

# ---- replace mock range JS with real per-range data + applyRange ----
REAL_JS = """// time range tabs — real per-range data from the gateway
  const RANGE_DATA = __RANGE_DATA_JSON__;
  function setText(id,v){const e=document.getElementById(id); if(e) e.textContent=v;}
  function setHTML(id,v){const e=document.getElementById(id); if(e) e.innerHTML=v;}
  function setAttr(id,a,v){const e=document.getElementById(id); if(e) e.setAttribute(a,v);}
  function applyRange(r){
    const d=RANGE_DATA[r]; if(!d) return;
    setText('vLatest',d.latest); setText('vMedian',d.median); setText('vSaved',d.saved); setText('vCost',d.cost);
    setHTML('dLatest',d.dLatest); setHTML('dMedian',d.dMedian); setHTML('dSaved',d.dSaved); setHTML('dCost',d.dCost);
    setHTML('wlBody',d.workloadRows); setHTML('reqList',d.requestRows);
    setHTML('wlMeta',d.wlMeta); setHTML('reqMeta',d.reqMeta);
    setText('rangeLabel',d.rangeLabel); setText('rangeLabelLong',d.rangeLabelLong);
    const c=d.chart||{};
    setAttr('chSavedArea','d',c.savedAreaD||''); setAttr('chSavedLine','d',c.savedLineD||'');
    setAttr('chSentArea','d',c.sentAreaD||''); setAttr('chSentLine','d',c.sentLineD||'');
    const dot=document.getElementById('chDot');
    if(dot){ dot.setAttribute('cx',c.dotX!=null?c.dotX:-10); dot.setAttribute('cy',c.dotY!=null?c.dotY:-10); dot.style.display=c.hasData?'':'none'; }
    const xl=c.xLabels||[];
    for(let i=0;i<5;i++) setText('chAxis'+i, xl[i]||'');
  }
  document.querySelectorAll('#timeTabs .time-tab').forEach(t=>{
    t.addEventListener('click',()=>{
      document.querySelectorAll('#timeTabs .time-tab').forEach(x=>x.classList.remove('active'));
      t.classList.add('active');
      applyRange(t.dataset.range);
    });
  });

  // copy button"""
sub(r'// time range tabs \(mock different data\).*?// copy button',
    lambda _m: REAL_JS, flags=re.DOTALL, label='range-js')

# ---- sanity: every expected token present, no stray sample data left ----
EXPECTED_TOKENS = [
    '__PROJECT_INITIALS__', '__PROJECT_NAME__', '__MEMBERS__', '__USER_INITIALS__',
    '__USER_NAME__', '__USER_EMAIL__', '__LATEST__', '__MEDIAN__', '__SAVED_USD__',
    '__COST_USD__', '__LATEST_DELTA__', '__MEDIAN_DELTA__', '__SAVED_DELTA__',
    '__COST_DELTA__', '__WL_META__', '__WORKLOAD_ROWS__', '__CAP_CARDS__',
    '__REQ_META__', '__REQUEST_ROWS__', '__CH_SAVED_AREA_D__', '__CH_SAVED_LINE_D__',
    '__CH_SENT_AREA_D__', '__CH_SENT_LINE_D__', '__CH_DOT_X__', '__CH_DOT_Y__',
    '__RANGE_DATA_JSON__', '__RANGE_LABEL__', '__RANGE_LABEL_LONG__', '__SCRIPT_NONCE__',
    '__CH_AXIS_0__', '__CH_AXIS_1__', '__CH_AXIS_2__', '__CH_AXIS_3__', '__CH_AXIS_4__',
    '__STATUS_TEXT__', '__STATUS_CLASS__',
]
missing = [t for t in EXPECTED_TOKENS if t not in o]
assert not missing, f"missing tokens: {missing}"
leftover_samples = [s for s in ('Jordan Davies', 'jordan@acme.io', '93.2%', '82.1%',
                                '$0.0544', 'acme-eng', 'mock different data') if s in o]
assert not leftover_samples, f"sample data still present: {leftover_samples}"
# brand mark must be empty (no stray 'D')
assert '<span class="mark"></span>' in o, "brand .mark is not empty"

open(DST, "w", encoding="utf-8").write(o)
print(f"OK: wrote {DST}")
print(f"   tokens injected: {len(EXPECTED_TOKENS)}  |  size: {len(o):,} bytes")
print("   brand .mark empty (no D overlay): yes")
