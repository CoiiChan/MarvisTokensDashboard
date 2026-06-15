#!/usr/bin/env python3
"""Marvis Model Call Real-time Monitor Dashboard - Open http://0.0.0.0:19999 after startup"""
import sqlite3, os, json, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ============================================================
# CONFIG: Replace YOUR_USER_ID_HERE with your Marvis user ID
# (found in %APPDATA%\Tencent\Marvis\User\)
# ============================================================
DB_PATH = os.path.expandvars(
    r"%APPDATA%\Tencent\Marvis\User\YOUR_USER_ID_HERE\database\data.db"
)
PORT = 19999

def get_stats():
    """Query database for model stats (today) + Marvis status"""
    if not os.path.exists(DB_PATH):
        return {"error": "Database not found", "models": [], "top_token_model": None}
    
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    
    today = time.strftime("%Y-%m-%d")
    
    # Today net consumption (deduplicated by response_id, using first chunk only)
    td = cur.execute("""
        SELECT COALESCE(SUM(ci),0) as cache_miss,
               COALESCE(SUM(ch),0) as cache_hit,
               COALESCE(SUM(co),0) as output_tokens
        FROM (
            SELECT t.input_tokens - t.cached_tokens as ci,
                   t.cached_tokens as ch,
                   t.output_tokens as co
            FROM llm_token_usage t
            INNER JOIN (SELECT response_id, MIN(rowid) as first_rowid 
                        FROM llm_token_usage WHERE usage_date=? 
                        GROUP BY response_id) x 
                ON t.rowid = x.first_rowid
        )
    """, (today,)).fetchone()
    today_net = td["cache_miss"] + td["output_tokens"]
    cache_miss  = td["cache_miss"]
    cache_hit   = td["cache_hit"]
    output_tok  = td["output_tokens"]
    
    # DeepSeek standard pricing (CNY per 1M tokens)
    PRICE_INPUT  = 1.0
    PRICE_CACHED = 0.1
    PRICE_OUTPUT = 2.0
    cost = round(cache_miss/1e6*PRICE_INPUT + cache_hit/1e6*PRICE_CACHED + output_tok/1e6*PRICE_OUTPUT, 2)
    
    # Aggregate by (model_id, response_id) after deduplication.
    # Cross-model response allocation: prioritize non-auto real models, assign by highest total_tokens
    all_models = cur.execute("""
        SELECT a.model_id,
               COUNT(*) as today_cnt,
               SUM(rt.ci + rt.co) as today_net,
               MAX(rt.ci + rt.co) as max_tokens,
               ROUND(AVG(rt.ci + rt.co)) as avg_tokens,
               a.last_used
        FROM (
            SELECT response_id, model_id, last_used
            FROM (
                SELECT response_id, model_id, MAX(created_at) as last_used,
                       ROW_NUMBER() OVER (
                           PARTITION BY response_id 
                           ORDER BY (model_id NOT LIKE '%-auto') DESC, MAX(total_tokens) DESC
                       ) as rn
                FROM llm_token_usage WHERE usage_date = ?
                GROUP BY response_id, model_id
            ) WHERE rn = 1
        ) a
        JOIN (
            SELECT t.response_id,
                   SUM(t.input_tokens - t.cached_tokens) as ci,
                   SUM(t.cached_tokens) as ch,
                   SUM(t.output_tokens) as co
            FROM llm_token_usage t
            INNER JOIN (SELECT response_id, MIN(rowid) as first_rowid 
                        FROM llm_token_usage WHERE usage_date = ? 
                        GROUP BY response_id) x 
                ON t.rowid = x.first_rowid
            GROUP BY t.response_id
        ) rt ON a.response_id = rt.response_id
        GROUP BY a.model_id
        ORDER BY today_net DESC
        LIMIT 8
    """, (today, today)).fetchall()
    
    # Today champion (based on deduplicated today_net)
    top_model = None
    if all_models:
        top_model = {
            "model_id": all_models[0]["model_id"],
            "tokens": all_models[0]["today_net"]
        }
    
    # Calculate percentage (denominator uses deduplicated today_net)
    model_list = []
    for m in all_models:
        pct = round(m["today_net"] / today_net * 100, 1) if today_net > 0 else 0
        model_list.append({
            "model_id": m["model_id"],
            "cnt": m["today_cnt"],
            "today_tokens": m["today_net"],
            "max_tokens": m["max_tokens"],
            "avg_tokens": m["avg_tokens"],
            "pct": pct,
            "last_used": m["last_used"]
        })
    
    # Marvis status
    busy = cur.execute("""
        SELECT COUNT(*) FROM conversations 
        WHERE status='in_progress' 
        AND updated_at > datetime('now','-2 minutes','localtime')
    """).fetchone()
    
    db.close()
    
    return {
        "today_net": today_net,
        "cache_miss": cache_miss,
        "cache_hit": cache_hit,
        "output_tok": output_tok,
        "cost": cost,
        "top_model": top_model,
        "models": model_list,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "busy" if busy and busy[0] > 0 else "idle"
    }

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Marvis</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background:#0d1117; color:#c9d1d9; font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  width:100vw; height:100vh; overflow:hidden;
  display:flex; flex-direction:column; align-items:center;
}

/* ====== Topbar ====== */
.topbar {
  width:100%; display:flex; align-items:center; justify-content:center; gap:16px;
  padding:10px 24px 0;
}
.topbar .dot { width:7px; height:7px; border-radius:50%; background:#3fb950; flex-shrink:0; }
.topbar .dot.error { background:#f85149; }
.topbar .muted { font-size:12px; color:#8b949e; }
.topbar .spacer { flex:0 0 60px; }
.topbar .mode-btn {
  background:none; border:1px solid #30363d; border-radius:6px;
  color:#8b949e; font-size:14px; padding:2px 8px; cursor:pointer;
  line-height:1.4; user-select:none;
}
.topbar .mode-btn:hover { border-color:#58a6ff; color:#c9d1d9; }

/* ====== Status Badge ====== */
.status-badge {
  font-size:13px; font-weight:600; padding:4px 14px; border-radius:12px; display:inline-block;
}
.status-badge.busy { background:#da363320; color:#f85149; border:1px solid #f8514940; }
.status-badge.idle { background:#3fb95020; color:#3fb950; border:1px solid #3fb95040; }

/* ====== Busy Mode: Enlarge status + shrink clock ====== */
body.busy-mode .status-badge {
  font-size:28px; padding:10px 28px; border-radius:18px;
  transition: font-size 0.4s, padding 0.4s;
}
body.busy-mode .clock {
  font-size:9vw;
  transition: font-size 0.4s;
}
.status-badge, .clock { transition: font-size 0.4s, padding 0.4s; }

/* ====== Night Mode ====== */
body.night-mode {
  filter: brightness(0.45);
  transition: filter 0.8s;
}
body { transition: filter 0.8s; }

/* ====== Clock ====== */
.clock-block { margin-top:4vh; text-align:center; }
.clock {
  font-size:13.5vw; font-weight:200; color:#f0f6fc; line-height:1;
  font-family:'Cascadia Code','Fira Code',monospace; letter-spacing:2px;
}

/* ====== Today Consumption Row ====== */
.today-row { margin-top:1.5vh; text-align:center; }
.today-label { font-size:13px; color:#8b949e; margin-right:8px; }
.today-total { font-size:32px; font-weight:700; color:#f78166; font-family:'Cascadia Code','Fira Code',monospace; }
.today-unit { font-size:14px; color:#8b949e; margin-left:4px; }
.today-breakdown { margin-top:0.8vh; text-align:center; font-size:12px; color:#8b949e; display:flex; justify-content:center; gap:16px; flex-wrap:wrap; }
.today-breakdown span { white-space:nowrap; }
.tb-miss { color:#f85149; }
.tb-hit  { color:#d2a8ff; }
.tb-out  { color:#79c0ff; }
.tb-cost { color:#8b949e; }

/* ====== Champion Row ====== */
.champ-row { margin-top:2vh; text-align:center; }
.champ-card {
  display:inline-block;
  background:linear-gradient(135deg,#1c2533 0%,#161b22 100%);
  border:1px solid #30363d; border-radius:12px;
  padding:12px 32px;
}
.champ-label { font-size:20px; color:#8b949e; text-transform:uppercase; letter-spacing:0.5px; }
.champ-model { font-size:32px; font-weight:700; color:#58a6ff; font-family:'Cascadia Code','Fira Code',monospace; margin:0 12px; }
.champ-tokens { font-size:24px; color:#f78166; font-family:'Cascadia Code','Fira Code',monospace; }
.champ-inner { display:flex; align-items:baseline; justify-content:center; gap:4px; }

/* ====== Table ====== */
.table-wrap { width:100%; max-width:1100px; margin-top:2vh; flex:1; overflow-y:auto; padding:0 48px 16px; }
.table-card {
  background:#0d1117;
  border:1px solid #21262d; border-radius:10px;
  padding:8px 12px;
}
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:13px; font-weight:600; color:#8b949e; text-transform:uppercase; letter-spacing:0.4px; padding:5px 10px; border-bottom:1px solid #21262d; position:sticky; top:0; background:#0d1117; }
td { padding:5px 10px; font-size:16px; border-bottom:1px solid #21262d; }
tr:hover td { background:#161b22; }
.model-name { font-family:'Cascadia Code','Fira Code',monospace; font-size:16px; font-weight:500; }
.model-name.auto { color:#f0883e; }
.model-name.real { color:#58a6ff; }
.badge {
  display:inline-block; font-size:12px; padding:1px 6px; border-radius:7px; font-weight:600;
  margin-left:4px; vertical-align:middle;
}
.badge.alias { background:#f0883e20; color:#f0883e; border:1px solid #f0883e40; }
.badge.real { background:#58a6ff20; color:#58a6ff; border:1px solid #58a6ff40; }
.count { font-weight:600; color:#f0f6fc; }
.token-val { font-family:'Cascadia Code','Fira Code',monospace; font-size:14px; color:#79c0ff; }
.pct-val { font-family:'Cascadia Code','Fira Code',monospace; font-size:14px; color:#c9d1d9; }
.time { font-size:14px; color:#8b949e; }
.rank { width:28px; text-align:center; color:#484f58; font-size:14px; font-weight:600; }
.rank.top { color:#f0f6fc; }

/* ====== Mobile Portrait ====== */
@media (max-width: 480px) {
  body { padding:0; }
  .topbar { gap:6px; padding:6px 8px 0; flex-wrap:nowrap; justify-content:flex-start; }
  .topbar .muted { font-size:10px; white-space:nowrap; }
  .topbar .mode-btn { font-size:12px; padding:1px 5px; margin-right:auto; }
  .topbar .status-badge { font-size:10px; padding:2px 8px; white-space:nowrap; flex-shrink:0; }
  .topbar .spacer { display:none; }

  .clock { font-size:16vw; }
  .today-total { font-size:24px; }
  .today-breakdown { font-size:10px; gap:8px; flex-wrap:wrap; }

  .champ-card { padding:10px 20px; }
  .champ-inner { flex-direction:column; align-items:center; gap:2px; }
  .champ-label { font-size:13px; }
  .champ-model { font-size:28px; margin:0; }
  .champ-tokens { font-size:15px; }

  .table-wrap { padding:0 4px 16px; }
  .table-card { padding:6px 6px; overflow-x:visible; }
  table { min-width:0; }
  th:nth-child(6), td:nth-child(6),
  th:nth-child(7), td:nth-child(7) { display:none; }
  th { font-size:10px; padding:9px 3px; }
  td { font-size:12px; padding:9px 3px; }
  .model-name { font-size:12px; }
  .badge { font-size:9px; padding:1px 4px; }
  .token-val, .pct-val, .time, .rank { font-size:11px; }
  .rank { width:22px; }
  .count { font-size:12px; }

  body.busy-mode .status-badge { font-size:18px; padding:6px 18px; }
  body.busy-mode .clock { font-size:12vw; }
}
</style>
</head>
<body>
<div class="topbar">
  <span class="dot" id="dot"></span>
  <span class="muted">Updated</span>
  <span class="muted" id="updated">--</span>
  <span class="muted">5s</span>
  <span class="spacer"></span>
  <span class="mode-btn" id="modeBtn" onclick="cycleMode()" title="Auto Night Day">◴</span>
  <span class="spacer"></span>
  <span class="status-badge idle" id="marvisStatus">Idle</span>
</div>

<div class="clock-block">
  <div class="clock" id="clock">--:--:--</div>
</div>

<div class="today-row">
  <span class="today-label">Today</span>
  <span class="today-total" id="todayTotal">--</span>
  <span class="today-unit">tokens</span>
</div>
<div class="today-breakdown">
  <span class="tb-miss"  id="bdMiss">--</span>
  <span class="tb-hit"   id="bdHit">--</span>
  <span class="tb-out"   id="bdOut">--</span>
  <span class="tb-cost"  id="bdCost">--</span>
</div>

<div class="champ-row">
  <div class="champ-card">
    <div class="champ-inner">
      <span class="champ-label">Champion</span>
      <span class="champ-model" id="champModel">--</span>
      <span class="champ-tokens" id="champTokens">--</span>
    </div>
  </div>
</div>

<div class="table-wrap">
  <div class="table-card">
    <table>
    <thead>
      <tr>
        <th style="width:24px">#</th>
        <th>Model</th>
        <th style="text-align:right">Calls</th>
        <th style="text-align:right">Tokens</th>
        <th style="text-align:right">Share</th>
        <th style="text-align:right">Peak</th>
        <th style="text-align:right">Avg</th>
        <th>Last</th>
      </tr>
    </thead>
    <tbody id="tb"></tbody>
  </table>
  </div>
</div>

<script>
function fmt(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1e6) return (n/1e6).toFixed(1)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toString();
}

function fmtWan(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1e4) return (n/1e4).toFixed(1)+'万';
  return n.toString();
}

function fmtPct(v) { return v == null ? '-' : v+'%'; }

function fmtTime(s) {
  if (!s) return '--';
  let d = new Date(s.replace('T',' ').replace(' ','T'));
  let diff = Date.now() - d;
  if (diff < 6e4) return 'just now';
  if (diff < 36e5) return Math.floor(diff/6e4)+'m ago';
  if (diff < 864e5) return Math.floor(diff/36e5)+'h ago';
  return s.slice(5,10)+' '+s.slice(11,16);
}

function setStatus(busy) {
  var dot = document.getElementById('dot');
  var el = document.getElementById('marvisStatus');
  if (busy) {
    dot.className = 'dot';
    el.className = 'status-badge busy';
    el.textContent = 'Busy';
    document.body.classList.add('busy-mode');
  } else {
    dot.className = 'dot';
    el.className = 'status-badge idle';
    el.textContent = 'Idle';
    document.body.classList.remove('busy-mode');
  }
}

async function refresh() {
  try {
    let r = await fetch('/api/stats');
    let d = await r.json();
    document.getElementById('updated').textContent = d.updated_at.slice(11,19);
    setStatus(d.status === 'busy');

    document.getElementById('todayTotal').textContent = fmtWan(d.today_net);
    document.getElementById('bdMiss').textContent = 'Input (miss) '+fmtWan(d.cache_miss);
    document.getElementById('bdHit').textContent  = 'Cache hit '+fmtWan(d.cache_hit);
    document.getElementById('bdOut').textContent  = 'Output '+fmtWan(d.output_tok);
    document.getElementById('bdCost').textContent = 'Est. cost ¥'+d.cost.toFixed(2);

    let top = d.top_model;
    if (top) {
      document.getElementById('champModel').textContent = top.model_id;
      document.getElementById('champTokens').textContent = fmtWan(top.tokens);
    }

    document.getElementById('tb').innerHTML = d.models.map((m,i) => {
      let isAlias = m.model_id.endsWith('-auto');
      return '<tr>'+
        '<td class="rank'+(i<3?' top':'')+'">'+(i+1)+'</td>'+
        '<td><span class="model-name '+(isAlias?'auto':'real')+'">'+m.model_id+'</span>'+
        '<span class="badge '+(isAlias?'alias':'real')+'">'+(isAlias?'Alias':'Real')+'</span></td>'+
        '<td style="text-align:right" class="count">'+m.cnt+'</td>'+
        '<td style="text-align:right" class="token-val">'+fmtWan(m.today_tokens)+'</td>'+
        '<td style="text-align:right" class="pct-val">'+fmtPct(m.pct)+'</td>'+
        '<td style="text-align:right" class="token-val">'+fmtWan(m.max_tokens)+'</td>'+
        '<td style="text-align:right" class="token-val">'+fmtWan(m.avg_tokens)+'</td>'+
        '<td class="time">'+fmtTime(m.last_used)+'</td>'+
      '</tr>';
    }).join('');
  } catch(e) {
    document.getElementById('dot').className = 'dot error';
    document.getElementById('updated').textContent = 'Offline';
  }
}

refresh();
setInterval(refresh, 5000);

function updateClock() {
  document.getElementById('clock').textContent = 
    new Date().toLocaleTimeString('zh-CN',{hour12:false});
}
updateClock();
setInterval(updateClock, 1000);

var modeState = 0;  // 0=auto, 1=night, 2=day
var modeChars = ['◴','☾','☀'];

function updateNightMode() {
  if (modeState !== 0) return;
  let h = new Date().getHours();
  let m = new Date().getMinutes();
  let t = h * 60 + m;
  let night = t < 570 || t >= 1080;  // before 9:30 or after 18:00
  document.body.classList.toggle('night-mode', night);
}
updateNightMode();
setInterval(updateNightMode, 60000);

function cycleMode() {
  modeState = (modeState + 1) % 3;
  document.getElementById('modeBtn').textContent = modeChars[modeState];
  if (modeState === 1) {
    document.body.classList.add('night-mode');
  } else if (modeState === 2) {
    document.body.classList.remove('night-mode');
  } else {
    updateNightMode();
  }
}
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(json.dumps(get_stats(), ensure_ascii=False).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(HTML.encode())

    def log_message(self, format, *args):
        pass  # suppress logs

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Marvis Model Monitor started: http://0.0.0.0:{PORT}")
    print("Press Ctrl+C to stop...")
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()

if __name__ == '__main__':
    main()
