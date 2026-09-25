"""
NEXUS VPS PANEL - LIGHT PREMIUM EDITION
Direct File Upload & Run Panel
"""
import os
import json
import time
import uuid
import shutil
import subprocess
import threading
import secrets
from collections import deque
from pathlib import Path
from functools import wraps
from flask import (
    Flask, request, redirect, url_for, session,
    render_template_string, jsonify, send_from_directory, send_file
)
from werkzeug.utils import secure_filename

# ============================================
#  INITIALIZATION
# ============================================
APP_DIR = Path(__file__).parent.absolute()
FILES_ROOT = APP_DIR / "user_files"

FILES_ROOT.mkdir(exist_ok=True)

WORKSPACE = "default"
USER_DIR = FILES_ROOT / WORKSPACE
USER_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# ============================================
#  FILTERS
# ============================================
@app.template_filter('timestamp_to_date')
def timestamp_to_date(ts):
    if not ts:
        return "LIFETIME"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except:
        return "INVALID"

# ============================================
#  PROCESS MANAGER (Multi-file support)
# ============================================
PROCS = {}

def start_process(filename):
    stop_process(filename)
    fpath = USER_DIR / filename
    if not fpath.exists():
        return False, "FILE NOT FOUND"
    
    ext = fpath.suffix.lower()
    if ext == ".py":
        cmd = ["python", "-u", str(fpath)]
    elif ext in (".js", ".mjs", ".cjs"):
        cmd = ["node", str(fpath)]
    elif ext == ".sh":
        cmd = ["bash", str(fpath)]
    else:
        return False, f"UNSUPPORTED FILE TYPE: {ext}"
    
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(USER_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1
        )
    except FileNotFoundError:
        return False, "RUNTIME NOT INSTALLED"
    
    logs = deque(maxlen=2000)
    logs.append(f"[START] {' '.join(cmd)}")
    PROCS[filename] = {"proc": proc, "logs": logs, "file": filename}
    
    def reader():
        try:
            for line in iter(proc.stdout.readline, b""):
                try:
                    txt = line.decode("utf-8", errors="replace").rstrip()
                except:
                    txt = str(line)
                logs.append(f"[{time.strftime('%H:%M:%S')}] {txt}")
        except Exception as e:
            logs.append(f"[ERROR] {e}")
        finally:
            logs.append(f"[EXIT] PROCESS ENDED WITH CODE {proc.poll()}")
    
    threading.Thread(target=reader, daemon=True).start()
    return True, "STARTED"

def stop_process(filename):
    info = PROCS.get(filename)
    if not info:
        return False
    proc = info["proc"]
    if proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except:
            pass
        info["logs"].append("[STOP] PROCESS TERMINATED")
    return True

def is_running(filename):
    info = PROCS.get(filename)
    return bool(info and info["proc"].poll() is None)

def get_logs(filename):
    info = PROCS.get(filename)
    return list(info["logs"]) if info else []

def get_all_running():
    return {fn: info for fn, info in PROCS.items() if info["proc"].poll() is None}

# ============================================
#  INSTALL MODULE
# ============================================
INSTALL_LOGS = {}

def run_install(command):
    parts = command.strip().split()
    if not parts:
        return False, "EMPTY COMMAND"
    if parts[0] not in ("pip", "pip3", "npm"):
        return False, "ONLY 'PIP INSTALL' OR 'NPM INSTALL' ALLOWED"
    if len(parts) < 3 or parts[1] != "install":
        return False, "FORMAT: PIP INSTALL <MODULE> OR NPM INSTALL <MODULE>"
    if any(c in command for c in [";", "&", "|", "`", "$(", ">"]):
        return False, "INVALID CHARACTERS"
    
    logs = INSTALL_LOGS.setdefault(WORKSPACE, deque(maxlen=1000))
    logs.append(f"[INSTALL] $ {command}")
    cwd = str(USER_DIR)
    
    def worker():
        try:
            proc = subprocess.Popen(parts, cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in iter(proc.stdout.readline, b""):
                try:
                    txt = line.decode("utf-8", errors="replace").rstrip()
                except:
                    txt = str(line)
                logs.append(txt)
            proc.wait()
            logs.append(f"[INSTALL] FINISHED WITH CODE {proc.returncode}")
        except Exception as e:
            logs.append(f"[INSTALL-ERROR] {e}")
    
    threading.Thread(target=worker, daemon=True).start()
    return True, "INSTALLING..."

def get_install_logs():
    return list(INSTALL_LOGS.get(WORKSPACE, []))

# ============================================
#  ROUTES
# ============================================
@app.route("/")
def home():
    files = sorted([f.name for f in USER_DIR.iterdir() if f.is_file()])
    running_files = [fn for fn in files if is_running(fn)]
    return render_template_string(
        HTML_DASHBOARD,
        files=files,
        running_files=running_files,
        all_procs=PROCS,
        is_running=is_running
    )

@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("files")
    for f in files:
        if f and f.filename:
            name = secure_filename(f.filename)
            if name:
                f.save(USER_DIR / name)
    return redirect(url_for("home"))

@app.route("/file/delete/<name>", methods=["POST"])
def file_delete(name):
    name = secure_filename(name)
    p = USER_DIR / name
    if p.exists() and p.is_file():
        stop_process(name)
        PROCS.pop(name, None)
        p.unlink()
    return redirect(url_for("home"))

@app.route("/file/view/<name>")
def file_view(name):
    name = secure_filename(name)
    return send_from_directory(USER_DIR, name, as_attachment=False)

@app.route("/download/<filename>")
def download_file(filename):
    filename = secure_filename(filename)
    fpath = USER_DIR / filename
    if fpath.exists() and fpath.is_file():
        return send_file(fpath, as_attachment=True, download_name=filename)
    return "FILE NOT FOUND", 404

@app.route("/server/start", methods=["POST"])
def server_start():
    filename = secure_filename(request.form.get("file", ""))
    ok, msg = start_process(filename)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/stop", methods=["POST"])
def server_stop():
    filename = secure_filename(request.form.get("file", ""))
    if filename:
        stop_process(filename)
    return jsonify({"ok": True})

@app.route("/server/restart", methods=["POST"])
def server_restart():
    filename = secure_filename(request.form.get("file", ""))
    if not filename:
        return jsonify({"ok": False, "msg": "NO FILE"})
    stop_process(filename)
    time.sleep(0.3)
    ok, msg = start_process(filename)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/delete", methods=["POST"])
def server_delete():
    filename = secure_filename(request.form.get("file", ""))
    if filename:
        stop_process(filename)
        PROCS.pop(filename, None)
    return jsonify({"ok": True})

@app.route("/logs/<filename>")
def logs_api(filename):
    filename = secure_filename(filename)
    return jsonify({
        "running": is_running(filename),
        "file": filename,
        "logs": get_logs(filename),
        "install": get_install_logs()
    })

@app.route("/logs/all")
def logs_all():
    result = {}
    for fn in list(PROCS.keys()):
        result[fn] = {
            "running": is_running(fn),
            "logs": get_logs(fn)
        }
    return jsonify(result)

@app.route("/install", methods=["POST"])
def install():
    cmd = request.form.get("command", "").strip()
    ok, msg = run_install(cmd)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/healthz")
def health():
    return "OK"

# ============================================
#  HTML TEMPLATE - LIGHT PREMIUM THEME
# ============================================
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — CONTROL PANEL</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --primary:#E53935;
  --secondary:#C62828;
  --gold:#F9A825;
  --bg:#F8F9FB;
  --bg-alt:#FFFFFF;
  --card:#FFFFFF;
  --brd:rgba(229,57,53,0.10);
  --brd-strong:rgba(229,57,53,0.25);
  --txt:#1A1A1A;
  --txt2:#4A4A4A;
  --mt:#8A8A8A;
  --shadow:0 2px 12px rgba(0,0,0,0.05);
  --shadow-lg:0 8px 32px rgba(229,57,53,0.10);
}
body{font-family:'Rajdhani',sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;text-transform:uppercase;letter-spacing:1px}
canvas#bg{display:none}
.glass-nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,0.92);backdrop-filter:blur(20px);border-bottom:2px solid var(--brd);padding:0.7rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;box-shadow:0 2px 12px rgba(0,0,0,0.03)}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:36px;height:36px;border-radius:9px;background:linear-gradient(135deg,var(--primary),var(--secondary));display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:900;color:#fff;box-shadow:0 4px 14px rgba(229,57,53,0.30);font-family:'Orbitron',sans-serif}
.brand-text{font-size:18px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Orbitron',sans-serif;letter-spacing:3px}
.badge{padding:4px 14px;background:linear-gradient(135deg,rgba(229,57,53,0.08),rgba(249,168,37,0.08));border:1.5px solid var(--brd-strong);border-radius:50px;font-size:9px;font-weight:700;letter-spacing:3px;color:var(--primary);font-family:'Orbitron',sans-serif}
.btn{padding:7px 16px;border-radius:8px;border:none;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;cursor:pointer;transition:0.25s;font-family:'Orbitron',sans-serif;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
.btn-danger{background:rgba(229,57,53,0.08);color:#E53935;border:1.5px solid rgba(229,57,53,0.20)}
.btn-danger:hover{background:rgba(229,57,53,0.15);border-color:var(--primary)}
.btn-success{background:rgba(46,125,50,0.08);color:#2E7D32;border:1.5px solid rgba(46,125,50,0.20)}
.btn-success:hover{background:rgba(46,125,50,0.15);border-color:#2E7D32}
.btn-warning{background:rgba(249,168,37,0.10);color:#E65100;border:1.5px solid rgba(249,168,37,0.25)}
.btn-warning:hover{background:rgba(249,168,37,0.18);border-color:#F9A825}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;box-shadow:0 4px 16px rgba(229,57,53,0.25)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(229,57,53,0.35)}
.btn-outline{background:#fff;border:1.5px solid var(--brd);color:var(--txt2)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm{padding:5px 11px;font-size:9px}
.wrap{position:relative;z-index:1;max-width:1260px;margin:0 auto;padding:18px 24px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.card{background:var(--card);border-radius:14px;border:1.5px solid var(--brd);padding:20px 22px;transition:0.25s;box-shadow:var(--shadow)}
.card:hover{border-color:var(--brd-strong);box-shadow:var(--shadow-lg)}
.card-header{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:3px;margin-bottom:14px;font-family:'Orbitron',sans-serif;color:var(--txt)}
.card-header i{color:var(--primary)}
.upload-zone{border:2px dashed rgba(229,57,53,0.20);border-radius:12px;padding:30px 20px;text-align:center;cursor:pointer;transition:0.3s;background:linear-gradient(135deg,rgba(229,57,53,0.015),rgba(249,168,37,0.015))}
.upload-zone:hover{border-color:var(--primary);background:linear-gradient(135deg,rgba(229,57,53,0.04),rgba(249,168,37,0.03));transform:translateY(-2px)}
.upload-zone.drag-over{border-color:var(--primary);background:rgba(229,57,53,0.06);transform:scale(1.01)}
.upload-zone i{font-size:36px;color:var(--primary);display:block;margin-bottom:10px;opacity:0.7}
.upload-zone p{font-size:11px;color:var(--txt2);font-weight:700;letter-spacing:2px}
.upload-zone .sub{font-size:9px;margin-top:6px;color:var(--mt);font-weight:500}
.upload-zone input{display:none}
.file-list{display:flex;flex-direction:column;gap:10px}
.file-item{background:linear-gradient(135deg,#FFFFFF,#FAFAFB);border:1.5px solid var(--brd);border-radius:12px;padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;transition:0.25s;box-shadow:0 1px 4px rgba(0,0,0,0.02)}
.file-item:hover{border-color:var(--brd-strong);background:linear-gradient(135deg,#FFFFFF,#FFF8F8);box-shadow:0 4px 16px rgba(229,57,53,0.06)}
.file-info{display:flex;align-items:center;gap:12px;flex:1;min-width:150px}
.file-icon{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,rgba(229,57,53,0.08),rgba(249,168,37,0.08));display:flex;align-items:center;justify-content:center;flex-shrink:0}
.file-icon i{color:var(--primary);font-size:18px}
.file-info .fname{font-size:12px;font-weight:700;font-family:'Orbitron',monospace;letter-spacing:1px;color:var(--txt)}
.file-info .fstatus{font-size:9px;color:var(--mt);letter-spacing:2px;font-weight:700;margin-top:2px}
.file-info .fstatus.running{color:#2E7D32}
.file-info .fstatus.running::before{content:"● ";animation:pulse 1.5s infinite}
.file-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.terminal{background:#1A1A1A;border-radius:12px;overflow:hidden;border:1.5px solid #2A2A2A;box-shadow:0 4px 20px rgba(0,0,0,0.12)}
.term-bar{display:flex;align-items:center;gap:6px;padding:8px 14px;background:#242424;border-bottom:1px solid #2E2E2E}
.term-dot{width:10px;height:10px;border-radius:50%}
.term-dot.red{background:#FF5F56}
.term-dot.yellow{background:#FFBD2E}
.term-dot.green{background:#27C93F}
.term-title{margin-left:6px;font-size:9px;color:#808080;letter-spacing:3px;font-family:'Orbitron',monospace;font-weight:600}
.term-body{padding:12px 16px;max-height:200px;overflow-y:auto;font-family:'JetBrains Mono',Consolas,monospace;font-size:10px;line-height:1.9;white-space:pre-wrap;word-break:break-word;color:#B8F0D0;letter-spacing:0.3px;text-transform:none}
.term-body::-webkit-scrollbar{width:6px}
.term-body::-webkit-scrollbar-track{background:#1A1A1A}
.term-body::-webkit-scrollbar-thumb{background:#3A3A3A;border-radius:10px}
.term-body::-webkit-scrollbar-thumb:hover{background:#4A4A4A}
.install-row{display:flex;gap:8px}
.install-row input{flex:1;padding:10px 14px;border-radius:8px;background:#F8F9FB;border:1.5px solid var(--brd);color:var(--txt);font-size:11px;font-family:'Orbitron',monospace;outline:none;transition:0.25s;text-transform:uppercase;letter-spacing:1px}
.install-row input:focus{border-color:var(--primary);background:#fff;box-shadow:0 0 0 4px rgba(229,57,53,0.06)}
.install-row input::placeholder{color:var(--mt);font-family:'Rajdhani',sans-serif;font-size:10px;letter-spacing:2px}
.empty-state{text-align:center;padding:40px 20px;color:var(--mt);font-size:11px;letter-spacing:2px}
.empty-state i{font-size:48px;display:block;margin-bottom:14px;color:var(--primary);opacity:0.2}
.empty-state .sub{font-size:10px;color:var(--mt);opacity:0.7;margin-top:6px}
.log-selector{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.log-chip{padding:5px 14px;border-radius:50px;font-size:9px;font-weight:700;letter-spacing:2px;font-family:'Orbitron',sans-serif;cursor:pointer;transition:0.25s;border:1.5px solid var(--brd);background:#fff;color:var(--txt2)}
.log-chip:hover{border-color:var(--primary);color:var(--primary)}
.log-chip.active{border-color:var(--primary);background:linear-gradient(135deg,rgba(229,57,53,0.08),rgba(249,168,37,0.06));color:var(--primary);box-shadow:0 2px 10px rgba(229,57,53,0.10)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
@media(max-width:600px){.glass-nav{padding:0.6rem 1rem}.wrap{padding:12px 14px}.file-item{flex-direction:column;align-items:flex-start}.file-actions{width:100%}}
</style>
</head>
<body>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span></div>
<span class="badge"><i class="fas fa-bolt"></i> VPS PANEL</span>
</nav>
<div class="wrap">

<!-- UPLOAD + INSTALL ROW -->
<div class="grid">
<div class="card">
<div class="card-header"><i class="fas fa-cloud-upload-alt"></i> UPLOAD FILES</div>
<div class="upload-zone" id="uploadZone">
<i class="fas fa-cloud-upload-alt"></i>
<p>DRAG &amp; DROP OR CLICK TO UPLOAD</p>
<p class="sub">PYTHON • NODE.JS • SHELL</p>
<input type="file" id="fileInput" multiple>
</div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-cubes"></i> INSTALL MODULE</div>
<div class="install-row">
<input type="text" id="installCmd" placeholder="PIP INSTALL REQUESTS">
<button class="btn btn-primary" onclick="installModule()"><i class="fas fa-download"></i></button>
</div>
<div class="terminal" style="margin-top:12px">
<div class="term-bar"><span class="term-dot red"></span><span class="term-dot yellow"></span><span class="term-dot green"></span><span class="term-title">INSTALL LOG</span></div>
<div class="term-body" id="installOutput" style="max-height:80px">— READY —</div>
</div>
</div>
</div>

<!-- FILES LIST -->
<div class="card" style="margin-bottom:16px">
<div class="card-header"><i class="fas fa-folder-open"></i> FILES <span style="font-size:9px;color:var(--mt);font-weight:400;letter-spacing:1px">({{ files|length }})</span></div>
{% if files %}
<div class="file-list">
{% for f in files %}
{% set running = is_running(f) %}
<div class="file-item">
<div class="file-info">
<div class="file-icon">
<i class="fas {% if f.endswith('.py') %}fa-python{% elif f.endswith('.js') or f.endswith('.mjs') %}fa-js{% elif f.endswith('.sh') %}fa-terminal{% else %}fa-file-code{% endif %}"></i>
</div>
<div>
<div class="fname">{{ f }}</div>
<div class="fstatus {% if running %}running{% endif %}">{% if running %}RUNNING{% else %}STOPPED{% endif %}</div>
</div>
</div>
<div class="file-actions">
{% if running %}
<button class="btn btn-danger btn-sm" onclick="stopFile('{{ f }}')"><i class="fas fa-stop"></i> STOP</button>
<button class="btn btn-warning btn-sm" onclick="restartFile('{{ f }}')"><i class="fas fa-sync"></i> RESTART</button>
{% else %}
<button class="btn btn-success btn-sm" onclick="startFile('{{ f }}')"><i class="fas fa-play"></i> START</button>
{% endif %}
<a href="/file/view/{{ f }}" target="_blank" class="btn btn-outline btn-sm"><i class="fas fa-eye"></i></a>
<a href="/download/{{ f }}" class="btn btn-outline btn-sm"><i class="fas fa-download"></i></a>
<button class="btn btn-danger btn-sm" onclick="deleteFile('{{ f }}')"><i class="fas fa-trash"></i></button>
</div>
</div>
{% endfor %}
</div>
{% else %}
<div class="empty-state"><i class="fas fa-inbox"></i>NO FILES UPLOADED YET<div class="sub">UPLOAD A FILE TO GET STARTED</div></div>
{% endif %}
</div>

<!-- LOGS -->
<div class="card">
<div class="card-header"><i class="fas fa-terminal"></i> LIVE LOGS</div>
<div class="log-selector" id="logSelector">
<button class="log-chip active" onclick="selectLog('__all__', this)">ALL</button>
{% for f in files %}
{% if is_running(f) %}
<button class="log-chip" onclick="selectLog('{{ f }}', this)">{{ f }}</button>
{% endif %}
{% endfor %}
</div>
<div class="terminal">
<div class="term-bar"><span class="term-dot red"></span><span class="term-dot yellow"></span><span class="term-dot green"></span><span class="term-title">OUTPUT</span></div>
<div class="term-body" id="logOutput" style="max-height:280px">[SYSTEM] WAITING FOR PROCESS...</div>
</div>
</div>
</div>

<script>
let currentLogFile = '__all__';

function showToast(msg, type='success'){
    const el=document.createElement('div');
    const isSuccess = type==='success';
    el.style.cssText=`position:fixed;top:80px;right:20px;z-index:9999;padding:12px 20px;border-radius:10px;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;font-family:'Orbitron',sans-serif;border:2px solid ${isSuccess?'rgba(46,125,50,0.25)':'rgba(229,57,53,0.25)'};background:#fff;color:${isSuccess?'#2E7D32':'#E53935'};box-shadow:0 8px 28px rgba(0,0,0,0.10);animation:slideIn 0.3s ease`;
    el.textContent=msg;
    document.body.appendChild(el);
    setTimeout(()=>el.remove(),3000);
}
const style=document.createElement('style');
style.textContent='@keyframes slideIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}';
document.head.appendChild(style);

// Upload
const uploadZone=document.getElementById('uploadZone');
const fileInput=document.getElementById('fileInput');
uploadZone.addEventListener('click',()=>fileInput.click());
uploadZone.addEventListener('dragover',e=>{e.preventDefault();uploadZone.classList.add('drag-over')});
uploadZone.addEventListener('dragleave',()=>uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop',e=>{e.preventDefault();uploadZone.classList.remove('drag-over');if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files)});
fileInput.addEventListener('change',function(){if(this.files.length)uploadFiles(this.files);this.value=''});
function uploadFiles(files){
    const fd=new FormData();
    for(let f of files)fd.append('files',f);
    showToast('UPLOADING '+files.length+' FILE(S)...');
    fetch('/upload',{method:'POST',body:fd}).then(()=>location.reload());
}

// File controls
function startFile(name){
    fetch('/server/start',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(name)})
    .then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),600)});
}
function stopFile(name){
    fetch('/server/stop',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(name)})
    .then(()=>location.reload());
}
function restartFile(name){
    fetch('/server/restart',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(name)})
    .then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),600)});
}
function deleteFile(name){
    if(!confirm('DELETE '+name+'?'))return;
    fetch('/file/delete/'+encodeURIComponent(name),{method:'POST'}).then(()=>location.reload());
}

// Logs
function selectLog(filename, btn){
    currentLogFile = filename;
    document.querySelectorAll('.log-chip').forEach(c=>c.classList.remove('active'));
    btn.classList.add('active');
    refreshLogs();
}
function refreshLogs(){
    const out=document.getElementById('logOutput');
    if(currentLogFile==='__all__'){
        fetch('/logs/all').then(r=>r.json()).then(data=>{
            let all=[];
            for(const [fn,info] of Object.entries(data)){
                if(info.logs && info.logs.length){
                    all.push('═══ '+fn+' ═══');
                    all.push(...info.logs);
                    all.push('');
                }
            }
            out.textContent = all.length ? all.join('\\n') : '[SYSTEM] NO ACTIVE PROCESSES';
            out.scrollTop = out.scrollHeight;
        });
    } else {
        fetch('/logs/'+encodeURIComponent(currentLogFile)).then(r=>r.json()).then(d=>{
            out.textContent = d.logs && d.logs.length ? d.logs.join('\\n') : '[SYSTEM] NO OUTPUT';
            out.scrollTop = out.scrollHeight;
        });
    }
}
function installModule(){
    const cmd=document.getElementById('installCmd').value.trim();
    if(!cmd)return;
    document.getElementById('installOutput').textContent='INSTALLING…';
    fetch('/install',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'command='+encodeURIComponent(cmd)})
    .then(r=>r.json()).then(d=>{
        document.getElementById('installOutput').textContent=d.msg;
        showToast(d.msg,d.ok);
    });
}
setInterval(refreshLogs,4000);
refreshLogs();
</script>
</body>
</html>"""

# ============================================
#  MAIN
# ============================================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 NEXUS VPS PANEL - LIGHT PREMIUM EDITION")
    print("="*70)
    print(f"📍 LOCAL:   http://127.0.0.1:5000")
    print(f"📍 NETWORK: http://0.0.0.0:5000")
    print(f"📁 FILES:   {USER_DIR}")
    print("="*70 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)