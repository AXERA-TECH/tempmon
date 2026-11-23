#!/usr/bin/env python3
"""板子监控聚合服务（本机运行，纯标准库，零依赖）。

- 后台线程每 1s 轮询 boards.json 里每块板的 /api/stats，缓存结果；
- HTTP 服务返回聚合 JSON 与仪表盘页面；
- 新增板子只需改 boards.json 后重启本服务。
"""
import json
import gzip
import hmac
import ipaddress
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
CONF = os.environ.get("TEMPMON_CONFIG", str(BASE_DIR / "boards.json"))
REGISTRATIONS = os.environ.get(
    "TEMPMON_REGISTRATIONS", str(BASE_DIR / "registrations.json")
)
DEFAULT_REPORT_TOKEN_FILE = Path.home() / ".config" / "tempmon" / "report.token"
PORT = int(os.environ.get("TEMPMON_MONITOR_PORT", "8888"))
POLL_INTERVAL = 1.0
AGENT_TIMEOUT = 3.0
LLAMA_TIMEOUT = 0.75
STALE_DATA_SECONDS = 10.0
FULL_SCAN_INTERVAL = 5.0
HISTORY_LENGTH = 300
DEFAULT_LLAMA_PORT_MIN = 18080
DEFAULT_LLAMA_PORT_MAX = 18089

# 缓存: { "name": {"ip":..., "port":..., "ok":bool, "age_ms":int, "data":{...}|None} }
cache = {}
cache_lock = threading.Lock()
conf = {"boards": []}
registrations = {}
registration_lock = threading.Lock()
llama_runtime = {}
board_history = {}


def board_name(board):
    return str(board.get("name") or board.get("ip") or "board")


def board_agent_port(board):
    try:
        return int(board.get("port", 8080))
    except (TypeError, ValueError):
        return 8080


def board_llama_range(board):
    try:
        lo = int(board.get("llama_port_min", DEFAULT_LLAMA_PORT_MIN))
        hi = int(board.get("llama_port_max", DEFAULT_LLAMA_PORT_MAX))
    except (TypeError, ValueError):
        lo, hi = DEFAULT_LLAMA_PORT_MIN, DEFAULT_LLAMA_PORT_MAX
    if hi < lo:
        lo, hi = hi, lo
    if lo < 1 or hi > 65535 or hi - lo > 127:
        return DEFAULT_LLAMA_PORT_MIN, DEFAULT_LLAMA_PORT_MAX
    return lo, hi

def load_conf():
    global conf
    try:
        with open(CONF) as f:
            conf = json.load(f)
        if not isinstance(conf.get("boards"), list):
            conf["boards"] = []
    except Exception as e:
        print("load conf error:", e)


def load_registrations():
    global registrations
    try:
        with open(REGISTRATIONS) as f:
            value = json.load(f)
        if not isinstance(value, dict):
            value = {}
        clean = {}
        for name, item in value.items():
            if not isinstance(item, dict):
                continue
            try:
                ipaddress.ip_address(str(item.get("ip", "")))
                port = int(item.get("port", 0))
            except (TypeError, ValueError):
                continue
            if not 1 <= port <= 65535:
                continue
            clean[str(name)] = {
                "ip": str(item["ip"]),
                "port": port,
                "reported_at": item.get("reported_at"),
                "updated_at": item.get("updated_at"),
            }
        with registration_lock:
            # A board heartbeat updates reported_at in memory every few seconds.
            # Reloading the file every poll cycle must not erase that timestamp;
            # persisted records only bootstrap address changes after a restart.
            current = dict(registrations)
            for name, item in clean.items():
                previous = current.get(name)
                if previous and previous.get("ip") == item.get("ip") and previous.get("port") == item.get("port"):
                    item["reported_at"] = previous.get("reported_at", item.get("reported_at"))
                    item["updated_at"] = previous.get("updated_at", item.get("updated_at"))
            registrations = clean
    except FileNotFoundError:
        with registration_lock:
            registrations = {}
    except Exception as exc:
        print("load registrations error:", exc)


def save_registrations():
    directory = os.path.dirname(REGISTRATIONS) or "."
    os.makedirs(directory, exist_ok=True)
    with registration_lock:
        value = dict(registrations)
    fd, temporary = tempfile.mkstemp(prefix=".registrations.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, REGISTRATIONS)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def configured_report_token():
    token = os.environ.get("TEMPMON_REPORT_TOKEN", "")
    if token:
        return token
    path = str(conf.get("report_token_file", "") or "")
    if not path:
        path = DEFAULT_REPORT_TOKEN_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        print("read report token error:", exc)
        return ""


def report_token_matches(provided):
    expected = configured_report_token()
    if not expected:
        return False
    provided = provided or ""
    return hmac.compare_digest(provided.encode(), expected.encode())


def register_board(name, port, client_ip):
    name = str(name or "").strip()
    try:
        ipaddress.IPv4Address(client_ip)
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("invalid board address")
    if not name or len(name) > 64 or any(ord(ch) < 32 for ch in name):
        raise ValueError("invalid board name")
    if not 1 <= port <= 65535:
        raise ValueError("invalid agent port")

    known = {board_name(board) for board in conf.get("boards", [])}
    if name not in known:
        raise LookupError("unknown board")

    now = time.time()
    with registration_lock:
        old = registrations.get(name)
        unchanged = bool(
            old
            and old.get("ip") == client_ip
            and int(old.get("port") or 0) == port
        )
        record = {
            "ip": client_ip,
            "port": port,
            "reported_at": now,
            "updated_at": old.get("updated_at", now) if unchanged else now,
        }
        registrations[name] = record
    if not unchanged:
        save_registrations()
    return record


def effective_board(board):
    name = board_name(board)
    with registration_lock:
        registration = dict(registrations.get(name, {}))
    if not registration:
        return dict(board)
    result = dict(board)
    result["ip"] = registration["ip"]
    result["port"] = registration["port"]
    result["registration"] = registration
    return result


def get_json(url, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ax8860-monitor/2"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                return None, None, "response too large"
            try:
                value = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                return response.status, None, "invalid JSON"
            return response.status, value, None
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(4096)
            message = raw.decode("utf-8", errors="replace") or str(exc)
        except Exception:
            message = str(exc)
        return exc.code, None, message
    except Exception as exc:
        return None, None, str(exc)


def connection_state(error):
    text = str(error or "").lower()
    if any(word in text for word in ("timed out", "timeout", "temporary failure", "unreachable")):
        return "unreachable"
    return "offline"

def fetch_board(b):
    ip = str(b.get("ip", ""))
    port = board_agent_port(b)
    url = "http://%s:%d/api/stats" % (ip, port)
    t0 = time.time()
    try:
        headers = {"Accept": "application/json"}
        token = str(b.get("token", "") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=AGENT_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        return {"ip": ip, "port": port, "ok": True, "age_ms": 0,
                "received_at": time.time(), "data": data,
                "fetch_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ip": ip, "port": port, "ok": False, "age_ms": 0,
                "received_at": time.time(), "data": None,
                "fetch_ms": int((time.time() - t0) * 1000), "error": str(e)}


def summarize_slots(slots):
    if not isinstance(slots, list):
        return {"count": 0, "processing": 0, "context_used": 0, "context_total": 0}
    context_used = context_total = processing = 0
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if slot.get("is_processing"):
            processing += 1
        try:
            context_used += int(slot.get("n_prompt_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            context_total += int(slot.get("n_ctx") or 0)
        except (TypeError, ValueError):
            pass
    return {"count": len(slots), "processing": processing,
            "context_used": context_used, "context_total": context_total}


def first_model_name(value):
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return None
    for item in value["data"]:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return None


def probe_llama_port(ip, port):
    base = "http://%s:%d" % (ip, port)
    health_status, health, health_error = get_json(base + "/health", LLAMA_TIMEOUT)
    if health_status is None:
        return {"port": port, "state": connection_state(health_error),
                "is_llama": False, "error": health_error}

    models_status, models, models_error = get_json(base + "/v1/models", LLAMA_TIMEOUT)
    slots_status, slots, slots_error = get_json(base + "/slots", LLAMA_TIMEOUT)
    model = first_model_name(models)
    slot_summary = summarize_slots(slots)
    health_ok = 200 <= int(health_status or 0) < 300 and isinstance(health, dict)
    looks_llama = (
        (isinstance(health, dict) and ("status" in health or "error" in health))
        or (200 <= int(models_status or 0) < 300 and model is not None)
        or (200 <= int(slots_status or 0) < 300 and isinstance(slots, list))
    )
    if not looks_llama:
        return {"port": port, "state": "unknown-service", "is_llama": False,
                "health_status": health_status,
                "error": health_error or "endpoint is not llama-server"}

    if health_ok and str(health.get("status", "ok")).lower() not in ("ok", "ready", "loaded"):
        state = "loading"
    elif health_ok and slot_summary["processing"]:
        state = "generating"
    elif health_ok:
        state = "ready"
    else:
        state = "unhealthy"

    result = {"port": port, "state": state, "is_llama": True,
              "health_status": health_status, "model": model,
              "slot_state": "processing" if slot_summary["processing"] else "idle",
              "checked_at": time.time(), **slot_summary}
    diagnostics = []
    if models_error:
        diagnostics.append("models: " + models_error)
    if slots_error:
        diagnostics.append("slots: " + slots_error)
    if diagnostics:
        result["error"] = "; ".join(diagnostics)
    return result


def scan_board_llama(board, runtime):
    ip = str(board.get("ip", ""))
    lo, hi = board_llama_range(board)
    now = time.time()
    full_due = float(runtime.get("next_full_scan", 0) or 0) <= now
    known = set()
    for port in runtime.get("known_ports", []):
        try:
            value = int(port)
        except (TypeError, ValueError):
            continue
        if lo <= value <= hi:
            known.add(value)
    ports = set(range(lo, hi + 1)) if full_due else known
    probes = []
    if ports:
        with ThreadPoolExecutor(max_workers=min(16, len(ports))) as executor:
            futures = {executor.submit(probe_llama_port, ip, port): port for port in sorted(ports)}
            for future in as_completed(futures):
                probes.append(future.result())
    probes.sort(key=lambda item: item["port"])
    servers = [item for item in probes if item.get("is_llama")]
    runtime["known_ports"] = [item["port"] for item in servers]
    if full_due:
        runtime["last_full_scan_at"] = now
        runtime["next_full_scan"] = now + FULL_SCAN_INTERVAL
    return {"port_range": [lo, hi], "online_count": len(servers), "servers": servers,
            "last_full_scan_at": runtime.get("last_full_scan_at"),
            "next_full_scan_at": runtime.get("next_full_scan")}


def poll_one_board(board):
    name = board_name(board)
    with cache_lock:
        runtime = llama_runtime.setdefault(name, {})
    result = fetch_board(board)
    result["name"] = name
    if isinstance(board.get("registration"), dict):
        result["registration"] = dict(board["registration"])
    result["llama"] = scan_board_llama(board, runtime)
    result["stale"] = False
    result["last_success_at"] = result.get("received_at") if result.get("ok") else None
    result["agent"] = {
        "online": bool(result["ok"]),
        "state": "online" if result.get("ok") else "offline",
        "ip": result["ip"],
        "port": result["port"],
        "age_ms": result["age_ms"],
        "fetch_ms": result.get("fetch_ms"),
        "error": result.get("error"),
    }
    result["inference"] = result["llama"]
    return name, result


def preserve_recent_good_data(result, previous, now=None):
    """Keep the last good agent/inference sample during a short network stall.

    A failed fetch must not erase the complete dashboard. The UI needs to show
    "stale" explicitly; only after STALE_DATA_SECONDS do we fall back to the
    offline card.
    """
    now = time.time() if now is None else now
    if not isinstance(result, dict) or result.get("ok"):
        return result
    previous = previous if isinstance(previous, dict) else {}
    last_success = previous.get("last_success_at") or (
        previous.get("received_at") if previous.get("ok") else None
    )
    result["last_success_at"] = last_success
    try:
        age = now - float(last_success) if last_success is not None else None
    except (TypeError, ValueError):
        age = None
    if age is None or age < 0 or age > STALE_DATA_SECONDS:
        return result

    for key in ("data", "llama", "inference"):
        if previous.get(key) is not None:
            result[key] = previous[key]
    result["stale"] = True
    result["data_age_ms"] = int(age * 1000)
    if isinstance(result.get("agent"), dict):
        result["agent"] = {
            **result["agent"],
            "online": False,
            "state": "stale",
            "data_age_ms": result["data_age_ms"],
        }
    return result


def finite_or_none(*values):
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return None


def make_history_sample(result):
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    memory = data.get("mem") if isinstance(data.get("mem"), dict) else {}
    cmm = data.get("cmm") if isinstance(data.get("cmm"), dict) else {}
    net = data.get("net") if isinstance(data.get("net"), dict) else {}
    npu = data.get("npu") if isinstance(data.get("npu"), dict) else {}
    inference = result.get("inference") if isinstance(result, dict) else {}
    inference = inference if isinstance(inference, dict) else {}
    llama = data.get("llama") if isinstance(data.get("llama"), dict) else {}
    servers = inference.get("servers") if isinstance(inference.get("servers"), list) else []
    processing = sum(
        1 for server in servers
        if isinstance(server, dict) and (
            server.get("state") == "generating" or server.get("slot_state") == "processing"
        )
    )
    temperatures = []
    for zone in data.get("zones", []) if isinstance(data.get("zones"), list) else []:
        if isinstance(zone, dict):
            temperature = finite_or_none(zone.get("c"))
            if temperature is not None:
                temperatures.append(temperature)

    return {
        "t": round(time.time(), 3),
        "agent_online": bool(result.get("ok")) if isinstance(result, dict) else False,
        "cmm_used_kb": finite_or_none(cmm.get("used_kb")),
        "os_used_kb": finite_or_none(
            memory.get("total_kb") - memory.get("avail_kb")
            if finite_or_none(memory.get("total_kb")) is not None and finite_or_none(memory.get("avail_kb")) is not None
            else None
        ),
        "os_available_kb": finite_or_none(memory.get("avail_kb")),
        "npu_temp_max_c": max(temperatures) if temperatures else None,
        "npu_util_avg": finite_or_none(npu.get("util_avg")),
        "rx_mbps": finite_or_none(net.get("rx_mbps")),
        "tx_mbps": finite_or_none(net.get("tx_mbps")),
        "inference_online": int(inference.get("online_count") or 0),
        "inference_processing": int(processing),
        "llama_rss_kb": finite_or_none(llama.get("rss_kb")),
    }


def record_history(name, result, limit=HISTORY_LENGTH):
    values = board_history.setdefault(name, [])
    values.append(make_history_sample(result))
    if len(values) > limit:
        del values[:len(values) - limit]


def poller():
    while True:
        cycle_started = time.monotonic()
        load_conf()
        load_registrations()
        boards = [effective_board(board) for board in conf.get("boards", [])]
        valid_names = set(board_name(board) for board in boards)
        with cache_lock:
            for stale in set(llama_runtime) - valid_names:
                llama_runtime.pop(stale, None)
            for stale in set(board_history) - valid_names:
                board_history.pop(stale, None)

        snapshot = {}
        if boards:
            with ThreadPoolExecutor(max_workers=min(16, len(boards))) as executor:
                futures = [executor.submit(poll_one_board, board) for board in boards]
                for future in as_completed(futures):
                    try:
                        name, result = future.result()
                        snapshot[name] = result
                    except Exception as exc:
                        print("poll board error:", exc)

        with cache_lock:
            for configured_board in boards:
                board = effective_board(configured_board)
                name = board_name(board)
                if name in snapshot:
                    result = preserve_recent_good_data(
                        snapshot[name],
                        cache.get(name),
                        now=time.time(),
                    )
                    cache[name] = result
                    record_history(name, result)
                elif name not in cache:
                    cache[name] = {"name": name, "ip": str(board.get("ip", "")),
                                   "port": board_agent_port(board), "ok": False,
                                   "age_ms": 0, "received_at": time.time(), "data": None,
                                   "error": "poll failed",
                                   "agent": {"online": False, "ip": str(board.get("ip", "")),
                                             "port": board_agent_port(board)},
                                   "inference": {"port_range": list(board_llama_range(board)),
                                                 "online_count": 0, "servers": []}}
        elapsed = time.monotonic() - cycle_started
        time.sleep(max(0.05, POLL_INTERVAL - elapsed))

def get_snapshot(include_history=False, history_limit=HISTORY_LENGTH):
    with cache_lock:
        now = time.time()
        out = []
        for name, r in cache.items():
            item = dict(r)
            received = r.get("received_at")
            item["age_ms"] = max(0, int((now - received) * 1000)) if isinstance(received, (int, float)) else None
            last_success = r.get("last_success_at")
            item["data_age_ms"] = (
                max(0, int((now - last_success) * 1000))
                if isinstance(last_success, (int, float)) else None
            )
            if isinstance(item.get("agent"), dict):
                item["agent"] = dict(item["agent"], age_ms=item["age_ms"])
                if item["agent"].get("state") != "offline":
                    item["agent"]["data_age_ms"] = item["data_age_ms"]
            if include_history:
                history = board_history.get(name, [])
                try:
                    limit = max(0, min(int(history_limit), HISTORY_LENGTH))
                except (TypeError, ValueError):
                    limit = HISTORY_LENGTH
                item["history"] = list(history[-limit:]) if limit else []
            out.append(item)
        return out

HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>AX8860 Boards Monitor</title>
<style>
body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:16px}
h1{font-size:18px;margin:0 0 4px}#ts{color:#8b949e;font-size:11px;margin-bottom:14px}
.board{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;margin-bottom:16px}
.bh{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.bh .n{font-size:15px;font-weight:700}
.dot{width:10px;height:10px;border-radius:50%;background:#f85149;display:inline-block}
.dot.on{background:#3fb950}
.dot.warn{background:#d29922}
.ip{color:#8b949e;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.card{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px}
.card .t{color:#8b949e;font-size:11px;margin-bottom:4px}
.card .v{font-size:16px;font-weight:600}
.bar{background:#21262d;border-radius:4px;height:7px;margin-top:4px;overflow:hidden}
.fill{height:100%;background:#3fb950}
.warn{color:#d29922}.hot{color:#f85149}.dim{color:#8b949e;font-size:11px}
.off{color:#f85149;font-size:13px}
.states{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px}
.state{display:inline-flex;align-items:center;gap:6px;border:1px solid #30363d;border-radius:999px;padding:3px 9px;font-size:11px;color:#8b949e}
.state.ok{color:#3fb950;border-color:#238636}
.state.bad{color:#f85149;border-color:#da3633}
.state.busy{color:#d29922;border-color:#9e6a03}
.services{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px;margin-bottom:10px}
.svc{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:9px 10px}
.svc .p{color:#79c0ff;font-size:13px;font-weight:700}
.svc .m{color:#c9d1d9;font-size:11px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc .s{color:#8b949e;font-size:11px;margin-top:2px}
</style></head><body>
<h1>AX8860 Boards Monitor</h1><div id=ts></div><div id=boards>loading...</div>
<script>
function bar(p){return '<div class=bar><div class=fill style="width:'+p+'%"></div></div>'}
function card(t,v,sub,cls){return '<div class=card><div class=t>'+t+'</div><div class="v '+(cls||'')+'">'+v+'</div>'+(sub?'<div class=dim>'+sub+'</div>':'')+'</div>'}
function fmtG(k){return (k/1024/1024).toFixed(1)+' GB'}
var updating=false,lastGoodAt=null,networkError='';
async function upd(){
 if(updating)return;updating=true;
 var controller=new AbortController();
 var timer=setTimeout(function(){controller.abort()},8000);
 try{
  var response=await fetch('/api/all',{cache:'no-store',signal:controller.signal});
  var d=await response.json();lastGoodAt=new Date();networkError='';
 document.getElementById('ts').textContent='updated: '+new Date().toLocaleTimeString();
 var h='';
 d.boards.forEach(b=>{
  var agent=b.agent||{};
  var stale=Boolean(b.stale&&b.data);
  var dot=agent.state==='online'?'<span class="dot on"></span>':(stale?'<span class="dot warn"></span>':'<span class="dot"></span>');
  var head='<div class=bh>'+dot+'<span class=n>'+b.name+'</span><span class=ip>'+b.ip+'</span></div>';
  var inf=b.inference||{online_count:0,servers:[],port_range:[18080,18089]};
  var reg=b.registration;
  var agentState;
  if(agent.state==='online'){agentState='<span class="state ok">Board Agent Online</span>'}
  else if(stale){agentState='<span class="state busy">Board Agent Stale · data '+((b.data_age_ms||0)/1000).toFixed(1)+'s</span>'}
  else{agentState='<span class="state bad">Board Agent Offline</span>'}
  if(reg){agentState+='<span class="state">Report '+reg.ip+':'+reg.port+'</span>'}
  var inferState;
  if(inf.online_count===0){inferState='<span class="state bad">Inference Offline</span>'}
  else if(inf.servers.some(function(x){return x.state==='generating'})){inferState='<span class="state busy">Inference Generating</span>'}
  else if(inf.servers.some(function(x){return x.state==='ready'})){inferState='<span class="state ok">Inference Ready</span>'}
  else{inferState='<span class="state busy">Inference '+inf.servers[0].state+'</span>'}
  var states='<div class=states>'+agentState+inferState+'<span class=state>'+inf.port_range[0]+'-'+inf.port_range[1]+' · '+inf.online_count+' online</span></div>';
  var services='<div class=services>'+inf.servers.map(function(s){
    return '<div class=svc><div class=p>:'+s.port+' · '+s.state+'</div><div class=m>'+((s.model||'model unknown').replace(/&/g,'&amp;').replace(/</g,'&lt;'))+'</div><div class=s>'+(s.slot_state||'slot unknown')+' · '+((s.context_used||0).toLocaleString())+' / '+((s.context_total||0).toLocaleString())+' ctx</div></div>';
  }).join('')+'</div>';
  if(!b.data){h+='<div class=board>'+head+states+services+'<div class=off>agent offline / no device data'+(b.error?' ('+b.error+')':'')+'</div></div>';return}
  var D=b.data;
  var used=D.mem.total_kb-D.mem.avail_kb;
  var mpct=Math.min(100,used/D.mem.total_kb*100);
  var cmin=Math.min.apply(null,D.cpu_freqs),cmax=Math.max.apply(null,D.cpu_freqs);
  var cf=(cmin==cmax)?(cmin/1000).toFixed(2)+' GHz':(cmin/1000).toFixed(2)+'~'+(cmax/1000).toFixed(2)+' GHz';
  var g='<div class=grid>';
  g+=card('CPU Load (1/5/15)', D.load['1'].toFixed(2)+' / '+D.load['5'].toFixed(2)+' / '+D.load['15'].toFixed(2), '');
  g+=card('CPU', D.cpu_cores+' cores', cf);
  g+=card('Memory', fmtG(used)+' / '+fmtG(D.mem.total_kb), bar(mpct));
  if(D.npu&&D.npu.util_avg!==undefined){
    var nu=D.npu.util_avg;
    var nucls=nu==null?'':(nu>=90?'hot':(nu>=70?'warn':''));
    g+=card('NPU Utilization', nu==null?'--':nu.toFixed(1)+'%',
      D.npu.tdp_freq_mhz+' MHz TDP · '+D.npu.clock_mhz.join(' / ')+' MHz clocks', nucls);
  }
  if(D.cmm&&D.cmm.total_kb>0){
    var cpct=Math.min(100,D.cmm.used_kb/D.cmm.total_kb*100);
    var ccls=cpct>=60?'hot':(cpct>=30?'warn':'');
    g+=card('CMM (NPU mem)', fmtG(D.cmm.used_kb)+' / '+fmtG(D.cmm.total_kb), bar(cpct)+cpct.toFixed(1)+'%');
  }
  if(D.net&&D.net.rx_mbps!==undefined){
    var rx=D.net.rx_mbps, tx=D.net.tx_mbps;
    var ncls=(rx>50||tx>50)?'hot':((rx>5||tx>5)?'warn':'');
    g+=card('Net ('+D.net.iface+')',
      '&darr;'+rx.toFixed(1)+' Mbps &nbsp;&uarr;'+tx.toFixed(1)+' Mbps',
      'total RX '+fmtG(D.net.rx_bytes/1024)+' &middot; TX '+fmtG(D.net.tx_bytes/1024), ncls);
  }
  if(D.disks)D.disks.forEach(function(dk){
    var pct=dk.total_kb?Math.min(100,dk.used_kb/dk.total_kb*100):0;
    var cls=pct>=85?'hot':(pct>=70?'warn':'');
    g+=card('Disk '+dk.mount, fmtG(dk.used_kb)+' / '+fmtG(dk.total_kb), bar(pct)+pct.toFixed(1)+'% · free '+fmtG(dk.avail_kb));
  });
  D.zones.forEach(z=>{var cls=z.c>=70?'hot':(z.c>=55?'warn':'');g+=card(z.type,z.c.toFixed(1)+' &deg;C',bar(Math.min(100,z.c)),cls)});
  g+='</div>';
  h+='<div class=board>'+head+states+services+g+'</div>';
 });
 document.getElementById('boards').innerHTML=h;
 }catch(err){
  networkError=err&&err.name==='AbortError'?'request timeout':String(err&&err.message||err);
  var suffix=lastGoodAt?' · last update '+lastGoodAt.toLocaleTimeString():'';
  document.getElementById('ts').textContent='web connection error: '+networkError+suffix;
 }finally{clearTimeout(timer);updating=false}
}
upd();setInterval(upd,1000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        body = body.encode() if isinstance(body, str) else body
        encoding = ""
        accept_encoding = self.headers.get("Accept-Encoding", "") if self.headers else ""
        if "gzip" in accept_encoding.lower() and len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            encoding = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # A browser refresh or an unstable proxy may close the connection while
            # a large JSON response is still being written. Keep the service quiet.
            pass

    def do_OPTIONS(self):
        self._send(204, "text/plain", "")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/report":
            self._send(404, "application/json", json.dumps({"error": "not found"}))
            return

        authorization = self.headers.get("Authorization", "")
        if not report_token_matches(authorization[7:] if authorization.startswith("Bearer ") else ""):
            self._send(401, "application/json", json.dumps({"error": "unauthorized"}))
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, "application/json", json.dumps({"error": "invalid content length"}))
            return
        if length <= 0 or length > 64 * 1024:
            self._send(400, "application/json", json.dumps({"error": "invalid body size"}))
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            client_ip = str(self.client_address[0])
            record = register_board(
                payload.get("name"),
                payload.get("port", 8080),
                client_ip,
            )
        except LookupError:
            self._send(404, "application/json", json.dumps({"error": "unknown board"}))
            return
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self._send(400, "application/json", json.dumps({"error": str(exc) or "invalid payload"}))
            return
        except Exception as exc:
            print("save board report error:", exc)
            self._send(500, "application/json", json.dumps({"error": "registration failed"}))
            return

        self._send(200, "application/json", json.dumps({
            "status": "ok",
            "name": board_name({"name": payload.get("name")}),
            "ip": record["ip"],
            "port": record["port"],
        }))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/all":
            history = 0
            values = query.get("history", [])
            if values:
                try:
                    history = max(0, min(int(values[-1]), HISTORY_LENGTH))
                except (TypeError, ValueError):
                    history = 0
            self._send(200, "application/json", json.dumps({
                "generated_at": int(time.time()),
                "boards": get_snapshot(include_history=history > 0, history_limit=history),
            }))
        elif path == "/api/history":
            limit = HISTORY_LENGTH
            values = query.get("limit", [])
            if values:
                try:
                    limit = max(0, min(int(values[-1]), HISTORY_LENGTH))
                except (TypeError, ValueError):
                    limit = HISTORY_LENGTH
            self._send(200, "application/json", json.dumps({
                "generated_at": int(time.time()),
                "boards": get_snapshot(include_history=True, history_limit=limit),
            }))
        elif self.path.startswith("/api/boards"):
            self._send(200, "application/json", json.dumps(conf))
        elif path == "/health":
            self._send(200, "application/json", json.dumps({"status": "ok"}))
        else:
            self._send(200, "text/html; charset=utf-8", HTML)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    load_conf()
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("monitor listening on :%d (%d boards)" % (PORT, len(conf.get("boards", []))))
    srv.serve_forever()
