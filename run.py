"""
NEXUS VPS PANEL - PREMIUM LIGHT EDITION
Complete Flask Application with Persistent Storage + Auto Restart
Owner: PR4MOD_H4X
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
import logging
logging.basicConfig(level=logging.DEBUG)

APP_DIR = Path(__file__).parent.absolute()
DATA_DIR = APP_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
PRICING_FILE = DATA_DIR / "pricing.json"
FILES_ROOT = APP_DIR / "user_files"

for d in [DATA_DIR, FILES_ROOT]:
    d.mkdir(exist_ok=True)

OWNER_USER = "PRAMOD"
OWNER_PASS = "9243"

DEFAULT_PRICING = {
    "currency": "$",
    "contact": "TELEGRAM: @PR4MOD_H4X",
    "plans": [
        {"name": "STARTER", "duration": "24 HOURS", "price": "49", "features": "1 FILE RUN, 512MB RAM"},
        {"name": "BASIC", "duration": "7 DAYS", "price": "199", "features": "MULTI-FILE UPLOAD, PIP/NPM"},
        {"name": "PRO", "duration": "30 DAYS", "price": "599", "features": "UNLIMITED MODULES, PRIORITY"},
        {"name": "PREMIUM", "duration": "LIFETIME", "price": "1999", "features": "ALL FEATURES, CUSTOM DOMAIN"},
    ]
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB max

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
#  STORAGE FUNCTIONS
# ============================================
_lock = threading.Lock()

def load_users():
    if not USERS_FILE.exists():
        return {}
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users):
    with _lock:
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f, indent=2)

def load_pricing():
    if not PRICING_FILE.exists():
        save_pricing(DEFAULT_PRICING)
        return DEFAULT_PRICING
    try:
        with open(PRICING_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return DEFAULT_PRICING

def save_pricing(pricing):
    with _lock:
        with open(PRICING_FILE, 'w') as f:
            json.dump(pricing, f, indent=2)

def user_dir(username):
    d = FILES_ROOT / username
    d.mkdir(parents=True, exist_ok=True)
    return d

# ============================================
#  AUTH DECORATORS
# ============================================
def is_owner():
    return session.get("role") == "owner"

def current_user():
    return session.get("username")

def require_owner(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not is_owner():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def require_user(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        username = current_user()
        if not username or session.get("role") != "user":
            return redirect(url_for("login"))
        users = load_users()
        if username not in users:
            session.clear()
            return redirect(url_for("login"))
        if users[username].get("expires_at") and time.time() > users[username]["expires_at"]:
            del users[username]
            save_users(users)
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# ============================================
#  PROCESS MANAGER WITH AUTO-RESTART
# ============================================
PROCS = {}
MAX_RESTARTS = 5           # Max restarts allowed in window
RESTART_WINDOW = 300       # 5 minutes window
RESTART_DELAY = 2          # Seconds to wait before restart

def start_process(username, filename, is_restart=False):
    if not is_restart:
        stop_process(username)
    
    udir = user_dir(username)
    fpath = udir / filename
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
            cmd, cwd=str(udir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True
        )
    except FileNotFoundError:
        return False, "RUNTIME NOT INSTALLED"
    
    old_info = PROCS.get(username, {})
    
    if is_restart:
        logs = old_info.get("logs", deque(maxlen=2000))
        logs.append(f"[AUTO-RESTART] {' '.join(cmd)}")
    else:
        logs = deque(maxlen=2000)
        logs.append(f"[START] {' '.join(cmd)}")
    
    PROCS[username] = {
        "proc": proc,
        "logs": logs,
        "file": filename,
        "auto_restart": old_info.get("auto_restart", False) if is_restart else False,
        "restart_count": old_info.get("restart_count", 0) if is_restart else 0,
        "restart_times": old_info.get("restart_times", deque(maxlen=MAX_RESTARTS)) if is_restart else deque(maxlen=MAX_RESTARTS),
        "stopping": False,
        "started_at": time.time(),
    }
    
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
            code = proc.poll()
            logs.append(f"[EXIT] PROCESS ENDED WITH CODE {code}")
            
            # ===== CRASH RECOVERY LOGIC =====
            info = PROCS.get(username)
            if not info or info.get("proc") is not proc:
                return
            
            if info.get("stopping"):
                logs.append("[RECOVERY] Manual stop — no restart")
                return
            
            if not info.get("auto_restart"):
                logs.append("[RECOVERY] Auto-restart DISABLED")
                return
            
            now = time.time()
            times = info["restart_times"]
            while times and now - times[0] > RESTART_WINDOW:
                times.popleft()
            
            if len(times) >= MAX_RESTARTS:
                logs.append(f"[RECOVERY] CRASH LOOP DETECTED — {MAX_RESTARTS} restarts in {RESTART_WINDOW}s. Giving up.")
                info["auto_restart"] = False
                return
            
            times.append(now)
            info["restart_count"] = info.get("restart_count", 0) + 1
            attempt = info["restart_count"]
            logs.append(f"[RECOVERY] Crash detected (code {code}). Restarting in {RESTART_DELAY}s... (attempt #{attempt})")
            
            time.sleep(RESTART_DELAY)
            
            check = PROCS.get(username)
            if not check or check.get("proc") is not proc or check.get("stopping"):
                return
            
            ok, msg = start_process(username, filename, is_restart=True)
            if ok:
                logs.append(f"[RECOVERY] Restarted successfully")
            else:
                logs.append(f"[RECOVERY] Restart failed: {msg}")
    
    threading.Thread(target=reader, daemon=True).start()
    return True, "STARTED"

def stop_process(username, manual=True):
    info = PROCS.get(username)
    if not info:
        return False
    proc = info["proc"]
    
    if manual:
        info["stopping"] = True
        info["auto_restart"] = False
    
    if proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except:
            pass
        info["logs"].append("[STOP] PROCESS TERMINATED")
    return True

def is_running(username):
    info = PROCS.get(username)
    return bool(info and info["proc"].poll() is None)

def get_logs(username):
    info = PROCS.get(username)
    return list(info["logs"]) if info else []

# ============================================
#  INSTALL MODULE
# ============================================
INSTALL_LOGS = {}

def run_install(username, command):
    parts = command.strip().split()
    if not parts:
        return False, "EMPTY COMMAND"
    if parts[0] not in ("pip", "pip3", "npm"):
        return False, "ONLY 'PIP INSTALL' OR 'NPM INSTALL' ALLOWED"
    if len(parts) < 3 or parts[1] != "install":
        return False, "FORMAT: PIP INSTALL <MODULE> OR NPM INSTALL <MODULE>"
    if any(c in command for c in [";", "&", "|", "`", "$(", ">"]):
        return False, "INVALID CHARACTERS"
    
    logs = INSTALL_LOGS.setdefault(username, deque(maxlen=1000))
    logs.append(f"[INSTALL] $ {command}")
    cwd = str(user_dir(username))
    
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

def get_install_logs(username):
    return list(INSTALL_LOGS.get(username, []))

# ============================================
#  ROUTES
# ============================================
@app.route("/")
def home():
    if is_owner():
        return redirect(url_for("owner_dashboard"))
    if current_user():
        return redirect(url_for("user_dashboard"))
    return render_template_string(HTML_LANDING, pricing=load_pricing())

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().upper()
        password = request.form.get("password", "")
        
        if username == OWNER_USER and password == OWNER_PASS:
            session.clear()
            session["role"] = "owner"
            session["username"] = username
            return redirect(url_for("owner_dashboard"))
        
        users = load_users()
        if username in users and users[username]["password"] == password:
            if users[username].get("expires_at") and time.time() > users[username]["expires_at"]:
                error = "ACCOUNT EXPIRED"
            else:
                session.clear()
                session["role"] = "user"
                session["username"] = username
                return redirect(url_for("user_dashboard"))
        else:
            error = "INVALID CREDENTIALS"
    
    return render_template_string(HTML_LOGIN, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.route("/auto/<token>")
def auto_login(token):
    users = load_users()
    for username, info in users.items():
        if info.get("token") == token:
            if info.get("expires_at") and time.time() > info["expires_at"]:
                return "ACCOUNT EXPIRED", 403
            session.clear()
            session["role"] = "user"
            session["username"] = username
            return redirect(url_for("user_dashboard"))
    return "INVALID LINK", 404

@app.route("/download/<filename>")
@require_user
def download_file(filename):
    username = current_user()
    filename = secure_filename(filename)
    udir = user_dir(username)
    fpath = udir / filename
    if fpath.exists() and fpath.is_file():
        return send_file(fpath, as_attachment=True, download_name=filename)
    return "FILE NOT FOUND", 404

# ============================================
#  OWNER ROUTES
# ============================================
@app.route("/owner")
@require_owner
def owner_dashboard():
    users = load_users()
    pricing = load_pricing()
    now = time.time()
    changed = False
    for username in list(users.keys()):
        if users[username].get("expires_at") and now > users[username]["expires_at"]:
            del users[username]
            stop_process(username)
            changed = True
    if changed:
        save_users(users)
    
    return render_template_string(
        HTML_OWNER,
        users=users,
        pricing=pricing,
        now=now,
        base_url=request.host_url.rstrip("/"),
        time=time
    )

@app.route("/owner/create", methods=["POST"])
@require_owner
def owner_create():
    username = request.form.get("username", "").strip().upper()
    password = request.form.get("password", "").strip()
    try:
        days = float(request.form.get("days", "7"))
    except:
        days = 7
    
    if not username or not password or username == OWNER_USER:
        return redirect(url_for("owner_dashboard"))
    
    users = load_users()
    users[username] = {
        "password": password,
        "created_at": time.time(),
        "expires_at": time.time() + days * 86400 if days > 0 else 0,
        "token": secrets.token_urlsafe(16)
    }
    save_users(users)
    user_dir(username)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/delete/<username>", methods=["POST"])
@require_owner
def owner_delete(username):
    users = load_users()
    if username in users:
        info = PROCS.get(username)
        if info:
            info["stopping"] = True
            info["auto_restart"] = False
        stop_process(username)
        PROCS.pop(username, None)
        del users[username]
        save_users(users)
        shutil.rmtree(FILES_ROOT / username, ignore_errors=True)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/extend/<username>", methods=["POST"])
@require_owner
def owner_extend(username):
    try:
        days = float(request.form.get("days", "7"))
    except:
        days = 7
    
    users = load_users()
    if username in users:
        base = max(users[username].get("expires_at") or time.time(), time.time())
        users[username]["expires_at"] = base + days * 86400
        save_users(users)
    return redirect(url_for("owner_dashboard"))

@app.route("/owner/pricing", methods=["POST"])
@require_owner
def owner_pricing():
    try:
        pricing = load_pricing()
        pricing["currency"] = request.form.get("currency", "$").strip() or "$"
        pricing["contact"] = request.form.get("contact", "").strip()
        plans = []
        names = request.form.getlist("p_name")
        durs = request.form.getlist("p_duration")
        prices = request.form.getlist("p_price")
        feats = request.form.getlist("p_features")
        for i in range(len(names)):
            if not names[i].strip():
                continue
            plans.append({
                "name": names[i].strip().upper(),
                "duration": durs[i].strip().upper() if i < len(durs) else "",
                "price": prices[i].strip() if i < len(prices) else "0",
                "features": feats[i].strip() if i < len(feats) else "",
            })
        pricing["plans"] = plans
        save_pricing(pricing)
        return redirect(url_for("owner_dashboard"))
    except Exception as e:
        return f"ERROR: {e}", 500

# ============================================
#  USER ROUTES
# ============================================
@app.route("/dashboard")
@require_user
def user_dashboard():
    username = current_user()
    users = load_users()
    info = users.get(username, {})
    udir = user_dir(username)
    files = sorted([f.name for f in udir.iterdir() if f.is_file()])
    pricing = load_pricing()
    
    proc_info = PROCS.get(username, {})
    
    return render_template_string(
        HTML_USER,
        username=username,
        info=info,
        files=files,
        running=is_running(username),
        running_file=proc_info.get("file") if is_running(username) else None,
        auto_restart=proc_info.get("auto_restart", False),
        restart_count=proc_info.get("restart_count", 0),
        expires_at=info.get("expires_at", 0),
        now=time.time(),
        pricing=pricing
    )

@app.route("/upload", methods=["POST"])
@require_user
def upload():
    username = current_user()
    udir = user_dir(username)
    files = request.files.getlist("files")
    
    for f in files:
        if f and f.filename:
            name = secure_filename(f.filename)
            if name:
                f.save(udir / name)
    
    return redirect(url_for("user_dashboard"))

@app.route("/file/delete/<name>", methods=["POST"])
@require_user
def file_delete(name):
    username = current_user()
    name = secure_filename(name)
    p = user_dir(username) / name
    if p.exists() and p.is_file():
        p.unlink()
    return redirect(url_for("user_dashboard"))

@app.route("/file/view/<name>")
@require_user
def file_view(name):
    username = current_user()
    name = secure_filename(name)
    return send_from_directory(user_dir(username), name, as_attachment=False)

@app.route("/server/start", methods=["POST"])
@require_user
def server_start():
    username = current_user()
    filename = secure_filename(request.form.get("file", ""))
    ok, msg = start_process(username, filename)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/stop", methods=["POST"])
@require_user
def server_stop():
    username = current_user()
    stop_process(username)
    return jsonify({"ok": True})

@app.route("/server/restart", methods=["POST"])
@require_user
def server_restart():
    username = current_user()
    info = PROCS.get(username)
    filename = info["file"] if info else secure_filename(request.form.get("file", ""))
    if not filename:
        return jsonify({"ok": False, "msg": "NO FILE"})
    was_auto = info.get("auto_restart", False) if info else False
    stop_process(username)
    time.sleep(0.3)
    ok, msg = start_process(username, filename)
    # Preserve auto-restart setting
    if ok and was_auto:
        new_info = PROCS.get(username)
        if new_info:
            new_info["auto_restart"] = True
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/delete", methods=["POST"])
@require_user
def server_delete():
    username = current_user()
    info = PROCS.get(username)
    if info:
        info["stopping"] = True
        info["auto_restart"] = False
    stop_process(username)
    PROCS.pop(username, None)
    return jsonify({"ok": True})

@app.route("/server/autorestart", methods=["POST"])
@require_user
def server_autorestart():
    username = current_user()
    enabled = request.form.get("enabled", "false").lower() == "true"
    info = PROCS.get(username)
    if not info:
        return jsonify({"ok": False, "msg": "NO PROCESS RUNNING"})
    info["auto_restart"] = enabled
    if enabled:
        info["restart_times"].clear()
        info["restart_count"] = 0
    info["logs"].append(f"[CONFIG] Auto-restart {'ENABLED' if enabled else 'DISABLED'}")
    return jsonify({"ok": True, "enabled": enabled})

@app.route("/logs")
@require_user
def logs_api():
    username = current_user()
    info = PROCS.get(username, {})
    return jsonify({
        "running": is_running(username),
        "file": info.get("file"),
        "logs": get_logs(username),
        "install": get_install_logs(username),
        "auto_restart": info.get("auto_restart", False),
        "restart_count": info.get("restart_count", 0),
        "uptime": int(time.time() - info.get("started_at", time.time())) if info else 0
    })

@app.route("/install", methods=["POST"])
@require_user
def install():
    username = current_user()
    cmd = request.form.get("command", "").strip()
    ok, msg = run_install(username, cmd)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/healthz")
def health():
    return "OK"

# ============================================
#  HTML TEMPLATES - PREMIUM LIGHT THEME
# ============================================
HTML_LANDING = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — PREMIUM</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f8f9fa;--surface:#ffffff;--surface2:#f1f3f5;--text:#1a1a2e;--text2:#4a4a6a;--text3:#8a8aaa;--primary:#6c5ce7;--primary2:#a29bfe;--primary3:#d5ccff;--border:#e9ecef;--shadow:0 8px 32px rgba(0,0,0,0.06);--radius:16px;--radius-sm:10px}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
.glass-nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,0.85);backdrop-filter:blur(20px);border-bottom:2px solid var(--border);padding:0.8rem 2rem;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:12px}
.brand-icon{width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,var(--primary),var(--primary2));display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:800;color:#fff;font-family:'Inter',sans-serif}
.brand-text{font-size:20px;font-weight:800;background:linear-gradient(135deg,var(--primary),var(--primary2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.5px}
.btn{padding:10px 24px;border-radius:var(--radius-sm);border:none;font-size:13px;font-weight:600;cursor:pointer;transition:0.3s;text-decoration:none;display:inline-flex;align-items:center;gap:8px}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--primary2));color:#fff;box-shadow:0 4px 16px rgba(108,92,231,0.25)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(108,92,231,0.35)}
.btn-outline{background:transparent;border:2px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary)}
.hero{max-width:1100px;margin:0 auto;padding:60px 24px 40px;text-align:center}
h1{font-size:clamp(36px,6vw,60px);font-weight:900;line-height:1.1;letter-spacing:-2px;margin-bottom:16px}
h1 .highlight{background:linear-gradient(135deg,var(--primary),var(--primary2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sub{font-size:18px;color:var(--text2);max-width:540px;margin:0 auto 32px;line-height:1.7;font-weight:400}
.pricing-section{max-width:1000px;margin:20px auto 30px}
.pricing-title{font-size:14px;font-weight:700;color:var(--text2);letter-spacing:2px;text-transform:uppercase;margin-bottom:16px}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.plan-card{background:var(--surface);border:2px solid var(--border);border-radius:var(--radius);padding:20px 18px;text-align:center;transition:0.3s}
.plan-card:hover{border-color:var(--primary);transform:translateY(-4px);box-shadow:var(--shadow)}
.plan-card.hot{border-color:var(--primary);background:linear-gradient(135deg,var(--surface),var(--primary3))}
.plan-card .hot-tag{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--primary);margin-bottom:4px}
.plan-card .pname{font-size:13px;font-weight:700;letter-spacing:1px;color:var(--text)}
.plan-card .pprice{font-size:28px;font-weight:900;color:var(--primary);margin:6px 0}
.plan-card .pdur{font-size:11px;color:var(--text3);font-weight:500}
.plan-card .pfeat{font-size:11px;color:var(--text2);margin-top:6px}
.contact-bar{background:var(--surface);border:2px solid var(--border);border-radius:var(--radius-sm);padding:12px 20px;margin-top:16px;font-size:13px;color:var(--text2);display:flex;align-items:center;justify-content:center;gap:8px}
.contact-bar strong{color:var(--primary)}
.features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;max-width:900px;margin:24px auto 0}
.feature-card{background:var(--surface);border:2px solid var(--border);border-radius:var(--radius-sm);padding:20px 16px;text-align:center;transition:0.3s}
.feature-card:hover{border-color:var(--primary);transform:translateY(-3px)}
.feature-card i{font-size:28px;color:var(--primary);margin-bottom:8px}
.feature-card h3{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
.feature-card p{font-size:11px;color:var(--text3)}
.btn-group{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
@media(max-width:768px){.glass-nav{padding:0.6rem 1rem}.hero{padding:40px 16px 30px}.pricing-grid{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.pricing-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span></div>
</nav>
<section class="hero">
<h1>DEPLOY &amp; RUN<br><span class="highlight">YOUR APPLICATIONS</span></h1>
<p class="sub">Upload, manage &amp; monitor Python, Node.js, and Shell scripts with real-time logs &amp; crash recovery</p>
<div class="btn-group">
<a href="/login" class="btn btn-primary"><i class="fas fa-arrow-right"></i> Launch Panel</a>
<a href="#pricing" class="btn btn-outline"><i class="fas fa-tags"></i> View Plans</a>
</div>
<div class="pricing-section" id="pricing">
<div class="pricing-title"><i class="fas fa-crown"></i> Pricing Plans</div>
<div class="pricing-grid">
{% for plan in pricing.plans %}
<div class="plan-card {% if loop.index == 3 %}hot{% endif %}">
{% if loop.index == 3 %}<div class="hot-tag"><i class="fas fa-star"></i> Popular</div>{% endif %}
<div class="pname">{{ plan.name }}</div>
<div class="pprice">{{ pricing.currency }}{{ plan.price }}</div>
<div class="pdur">{{ plan.duration }}</div>
<div class="pfeat">{{ plan.features }}</div>
</div>
{% endfor %}
</div>
<div class="contact-bar"><i class="fas fa-headset"></i> Contact: <strong>{{ pricing.contact }}</strong></div>
</div>
<div class="features-grid">
<div class="feature-card"><i class="fas fa-server"></i><h3>99.9% Uptime</h3><p>Enterprise-grade</p></div>
<div class="feature-card"><i class="fas fa-heartbeat"></i><h3>Auto Recovery</h3><p>Crash restart</p></div>
<div class="feature-card"><i class="fas fa-upload"></i><h3>10MB Upload</h3><p>File limit</p></div>
<div class="feature-card"><i class="fas fa-infinity"></i><h3>Unlimited Storage</h3><p>No file limits</p></div>
</div>
</section>
</body>
</html>"""

HTML_LOGIN = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS — Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f8f9fa;--surface:#ffffff;--text:#1a1a2e;--text2:#4a4a6a;--text3:#8a8aaa;--primary:#6c5ce7;--primary2:#a29bfe;--border:#e9ecef;--radius:16px;--radius-sm:10px;--shadow:0 8px 32px rgba(0,0,0,0.06)}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{width:100%;max-width:400px;padding:20px}
.box-inner{background:var(--surface);border-radius:var(--radius);padding:40px 32px;border:2px solid var(--border);box-shadow:var(--shadow)}
.logo{text-align:center;margin-bottom:32px}
.logo-icon{width:50px;height:50px;border-radius:12px;background:linear-gradient(135deg,var(--primary),var(--primary2));display:inline-flex;align-items:center;justify-content:center;font-size:20px;font-weight:800;color:#fff;margin-bottom:12px}
.logo-text{font-size:24px;font-weight:800;background:linear-gradient(135deg,var(--primary),var(--primary2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-sub{font-size:13px;color:var(--text3);margin-top:4px}
.error{background:#fff5f5;border:2px solid #fee2e2;border-radius:var(--radius-sm);padding:12px 16px;margin-bottom:20px;color:#dc2626;font-size:13px;font-weight:500;text-align:center}
.field{margin-bottom:18px}
.field label{display:block;font-size:12px;font-weight:600;color:var(--text2);margin-bottom:6px;letter-spacing:0.5px}
.field input{width:100%;padding:12px 16px;border-radius:var(--radius-sm);background:var(--bg);border:2px solid var(--border);color:var(--text);font-size:14px;font-family:'Inter',sans-serif;outline:none;transition:0.3s}
.field input:focus{border-color:var(--primary);box-shadow:0 0 0 4px rgba(108,92,231,0.08)}
.btn-submit{width:100%;padding:14px;border:none;border-radius:var(--radius-sm);background:linear-gradient(135deg,var(--primary),var(--primary2));color:#fff;font-size:14px;font-weight:700;cursor:pointer;transition:0.3s;box-shadow:0 4px 16px rgba(108,92,231,0.25)}
.btn-submit:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(108,92,231,0.35)}
.back{text-align:center;margin-top:16px}
.back a{color:var(--text3);text-decoration:none;font-size:13px;font-weight:500;transition:0.3s}
.back a:hover{color:var(--primary)}
</style>
</head>
<body>
<div class="box">
<div class="box-inner">
<div class="logo"><div class="logo-icon">NX</div><div class="logo-text">NEXUS</div><div class="logo-sub">Secure Access Portal</div></div>
{% if error %}<div class="error"><i class="fas fa-exclamation-triangle"></i> {{ error }}</div>{% endif %}
<form method="POST">
<div class="field"><label><i class="fas fa-user"></i> Username</label><input type="text" name="username" placeholder="Enter username" required></div>
<div class="field"><label><i class="fas fa-lock"></i> Password</label><input type="password" name="password" placeholder="Enter password" required></div>
<button type="submit" class="btn-submit"><i class="fas fa-arrow-right"></i> Access</button>
</form>
<div class="back"><a href="/"><i class="fas fa-arrow-left"></i> Back to Home</a></div>
</div>
</div>
</body>
</html>"""

HTML_OWNER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS — Owner Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f8f9fa;--surface:#ffffff;--surface2:#f1f3f5;--text:#1a1a2e;--text2:#4a4a6a;--text3:#8a8aaa;--primary:#6c5ce7;--primary2:#a29bfe;--border:#e9ecef;--radius:16px;--radius-sm:10px;--shadow:0 8px 32px rgba(0,0,0,0.06)}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.glass-nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,0.92);backdrop-filter:blur(20px);border-bottom:2px solid var(--border);padding:0.6rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--primary2));display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:800;color:#fff}
.brand-text{font-size:18px;font-weight:800;background:linear-gradient(135deg,var(--primary),var(--primary2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge-owner{padding:4px 14px;background:var(--primary3);border-radius:50px;font-size:9px;font-weight:700;letter-spacing:1px;color:var(--primary);text-transform:uppercase}
.btn{padding:6px 14px;border-radius:var(--radius-sm);border:none;font-size:11px;font-weight:600;cursor:pointer;transition:0.3s;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn-danger{background:#fee2e2;color:#dc2626;border:2px solid #fecaca}
.btn-danger:hover{background:#fecaca}
.btn-success{background:#dcfce7;color:#16a34a;border:2px solid #bbf7d0}
.btn-success:hover{background:#bbf7d0}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--primary2));color:#fff;box-shadow:0 4px 16px rgba(108,92,231,0.2)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(108,92,231,0.3)}
.btn-sm{padding:4px 10px;font-size:10px}
.wrap{max-width:1260px;margin:0 auto;padding:16px 24px}
.card{background:var(--surface);border-radius:var(--radius);border:2px solid var(--border);padding:16px 20px;margin-bottom:14px;transition:0.3s}
.card:hover{border-color:var(--primary2)}
.card-header{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;margin-bottom:12px}
.card-header i{color:var(--primary)}
.form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center}
.form-row input{padding:8px 14px;border-radius:var(--radius-sm);background:var(--bg);border:2px solid var(--border);color:var(--text);font-size:12px;font-family:'Inter',sans-serif;flex:1;min-width:100px;max-width:200px;outline:none;transition:0.3s}
.form-row input:focus{border-color:var(--primary);box-shadow:0 0 0 4px rgba(108,92,231,0.06)}
.form-row input[type="number"]{max-width:80px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{padding:8px 12px;text-align:left;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--text3);border-bottom:2px solid var(--border);background:var(--bg)}
td{padding:8px 12px;border-bottom:1px solid var(--border)}
tr:hover td{background:var(--surface2)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 12px;border-radius:50px;font-size:8px;font-weight:700;text-transform:uppercase}
.badge-active{background:#dcfce7;color:#16a34a;border:1px solid #bbf7d0}
.badge-expired{background:#fee2e2;color:#dc2626;border:1px solid #fecaca}
.badge-soon{background:#fef3c7;color:#d97706;border:1px solid #fde68a}
.link{font-size:9px;color:var(--text3);font-family:'Inter',monospace;text-decoration:none;padding:2px 6px;border-radius:4px;background:var(--surface2);transition:0.3s;word-break:break-all}
.link:hover{color:var(--primary);background:var(--primary3)}
.actions{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.actions form{display:inline}
.actions input[type="number"]{width:44px;padding:3px 6px;border-radius:4px;background:var(--bg);border:2px solid var(--border);color:var(--text);font-size:10px;text-align:center;outline:none}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:10px}
.plan-card{background:var(--surface2);border:2px solid var(--border);border-radius:var(--radius-sm);padding:12px 10px;text-align:center;transition:0.3s}
.plan-card:hover{border-color:var(--primary)}
.plan-card .pname{font-size:11px;font-weight:700;color:var(--text)}
.plan-card .pprice{font-size:20px;font-weight:900;color:var(--primary);margin:4px 0}
.plan-card .pdur{font-size:9px;color:var(--text3)}
.plan-card .pfeat{font-size:8px;color:var(--text2);margin-top:4px}
.plan-card.hot{border-color:var(--primary);background:linear-gradient(135deg,var(--surface),var(--primary3))}
.plan-card .hot-tag{font-size:7px;font-weight:700;color:var(--primary);letter-spacing:1px;text-transform:uppercase}
.plan-card input{width:100%;padding:3px 8px;border-radius:4px;background:var(--bg);border:1px solid var(--border);color:var(--text);font-size:9px;text-align:center;font-family:'Inter',sans-serif;margin-bottom:3px;outline:none}
.plan-card input:focus{border-color:var(--primary)}
.contact-bar{background:var(--surface2);border:2px solid var(--border);border-radius:var(--radius-sm);padding:8px 16px;margin-top:10px;text-align:center;font-size:12px;color:var(--text2)}
.contact-bar strong{color:var(--primary)}
.row2{display:grid;grid-template-columns:1.4fr 0.6fr;gap:14px}
@media(max-width:900px){.row2{grid-template-columns:1fr}}
@media(max-width:600px){.glass-nav{padding:0.6rem 1rem}.wrap{padding:10px 16px}.form-row{flex-direction:column}.form-row input{max-width:100%}}
</style>
</head>
<body>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span><span class="badge-owner"><i class="fas fa-crown"></i> Owner</span></div>
<div><a href="/logout" class="btn btn-danger btn-sm"><i class="fas fa-sign-out-alt"></i> Logout</a></div>
</nav>
<div class="wrap">
<div class="card">
<div class="card-header" style="justify-content:center"><i class="fas fa-user-plus"></i> Create User</div>
<form method="POST" action="/owner/create" class="form-row">
<input type="text" name="username" placeholder="Username" required>
<input type="text" name="password" placeholder="Password" required>
<input type="number" name="days" placeholder="Days" value="7" min="1">
<button type="submit" class="btn btn-primary"><i class="fas fa-plus"></i> Create</button>
</form>
</div>
<div class="row2">
<div class="card">
<div class="card-header"><i class="fas fa-users"></i> Users <span style="font-weight:400;color:var(--text3);font-size:12px">({{ users|length }})</span></div>
<div class="table-wrap" style="max-height:400px;overflow-y:auto">
<table>
<thead><tr><th>User</th><th>Pass</th><th>Expires</th><th>Status</th><th>Link</th><th>Actions</th></tr></thead>
<tbody>
{% for username, info in users.items() %}
<tr>
<td><strong style="color:var(--primary)">{{ username }}</strong></td>
<td><span style="font-family:monospace;font-size:11px;color:var(--text2)">{{ info.password }}</span></td>
<td style="font-size:11px;color:var(--text2)">
{% if info.expires_at %}{{ time.strftime('%Y-%m-%d', time.localtime(info.expires_at)) }}{% else %}Never{% endif %}
</td>
<td>
{% if info.expires_at and info.expires_at < now %}
<span class="badge badge-expired">Expired</span>
{% elif info.expires_at and info.expires_at < now + 86400*3 %}
<span class="badge badge-soon">Soon</span>
{% else %}
<span class="badge badge-active">Active</span>
{% endif %}
</td>
<td><a href="{{ base_url }}/auto/{{ info.token }}" target="_blank" class="link"><i class="fas fa-link"></i> {{ info.token[:10] }}…</a></td>
<td class="actions">
<form method="POST" action="/owner/extend/{{ username }}">
<input type="number" name="days" value="7" min="1">
<button type="submit" class="btn btn-success btn-sm"><i class="fas fa-clock"></i></button>
</form>
<form method="POST" action="/owner/delete/{{ username }}" onsubmit="return confirm('Delete {{ username }}?')">
<button type="submit" class="btn btn-danger btn-sm"><i class="fas fa-trash"></i></button>
</form>
</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-tags"></i> Pricing</div>
<form method="POST" action="/owner/pricing">
<div class="form-row" style="margin-bottom:10px;justify-content:flex-start">
<input type="text" name="currency" placeholder="Currency" value="{{ pricing.currency }}" style="max-width:60px">
<input type="text" name="contact" placeholder="Contact" value="{{ pricing.contact }}" style="flex:2;font-size:10px">
</div>
<div class="pricing-grid">
{% for plan in pricing.plans %}
<div class="plan-card {% if loop.index == 3 %}hot{% endif %}">
{% if loop.index == 3 %}<div class="hot-tag"><i class="fas fa-star"></i> Popular</div>{% endif %}
<input type="text" name="p_name" value="{{ plan.name }}" placeholder="Name">
<input type="text" name="p_duration" value="{{ plan.duration }}" placeholder="Duration">
<input type="text" name="p_price" value="{{ plan.price }}" placeholder="Price" style="color:var(--primary);font-weight:700">
<input type="text" name="p_features" value="{{ plan.features }}" placeholder="Features" style="font-size:7px">
</div>
{% endfor %}
<div class="plan-card" style="border-style:dashed">
<div style="font-size:8px;color:var(--text3);margin-bottom:4px">New</div>
<input type="text" name="p_name" placeholder="Name">
<input type="text" name="p_duration" placeholder="Duration">
<input type="text" name="p_price" placeholder="Price" style="color:var(--primary);font-weight:700">
<input type="text" name="p_features" placeholder="Features" style="font-size:7px">
</div>
</div>
<div class="contact-bar"><i class="fas fa-headset"></i> <strong>{{ pricing.contact }}</strong></div>
<button type="submit" class="btn btn-primary" style="margin-top:10px;width:100%;justify-content:center"><i class="fas fa-save"></i> Save</button>
</form>
</div>
</div>
</div>
</body>
</html>"""

HTML_USER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f8f9fa;--surface:#ffffff;--surface2:#f1f3f5;--text:#1a1a2e;--text2:#4a4a6a;--text3:#8a8aaa;--primary:#6c5ce7;--primary2:#a29bfe;--primary3:#d5ccff;--border:#e9ecef;--radius:16px;--radius-sm:10px;--shadow:0 8px 32px rgba(0,0,0,0.06)}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.glass-nav{position:sticky;top:0;z-index:50;background:rgba(255,255,255,0.92);backdrop-filter:blur(20px);border-bottom:2px solid var(--border);padding:0.6rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--primary2));display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:800;color:#fff}
.brand-text{font-size:18px;font-weight:800;background:linear-gradient(135deg,var(--primary),var(--primary2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.user-info{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.user-badge{display:flex;align-items:center;gap:8px;background:var(--surface2);padding:4px 14px 4px 12px;border-radius:50px;border:2px solid var(--border)}
.user-badge i{color:var(--primary);font-size:12px}
.user-badge .uname{font-size:13px;font-weight:700;letter-spacing:0.5px}
.user-badge .expiry{font-size:10px;color:var(--text3);font-weight:500}
.btn{padding:6px 16px;border-radius:var(--radius-sm);border:none;font-size:11px;font-weight:600;cursor:pointer;transition:0.3s;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn-danger{background:#fee2e2;color:#dc2626;border:2px solid #fecaca}
.btn-danger:hover{background:#fecaca}
.btn-success{background:#dcfce7;color:#16a34a;border:2px solid #bbf7d0}
.btn-success:hover{background:#bbf7d0}
.btn-warning{background:#fef3c7;color:#d97706;border:2px solid #fde68a}
.btn-warning:hover{background:#fde68a}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--primary2));color:#fff;box-shadow:0 4px 16px rgba(108,92,231,0.2)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 24px rgba(108,92,231,0.3)}
.btn-outline{background:transparent;border:2px solid var(--border);color:var(--text2)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm{padding:4px 10px;font-size:9px}
.wrap{max-width:1260px;margin:0 auto;padding:12px 20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.card{background:var(--surface);border-radius:var(--radius);border:2px solid var(--border);padding:14px 18px;transition:0.3s}
.card:hover{border-color:var(--primary2)}
.card-header{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:700;margin-bottom:10px}
.card-header i{color:var(--primary)}
.status-row{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.status-dot.running{background:#16a34a;box-shadow:0 0 20px rgba(22,163,74,0.3);animation:pulse 2s infinite}
.status-dot.stopped{background:#dc2626;box-shadow:0 0 20px rgba(220,38,38,0.15)}
@keyframes pulse{0%,100%{box-shadow:0 0 20px rgba(22,163,74,0.3)}50%{box-shadow:0 0 40px rgba(22,163,74,0.1)}}
.status-label{font-weight:700;font-size:13px}
.status-label.running{color:#16a34a}
.status-label.stopped{color:#dc2626}
.running-file{font-size:11px;font-family:monospace;color:var(--text2);background:var(--surface2);padding:4px 12px;border-radius:6px;border:2px solid var(--border);display:inline-block;margin-bottom:10px}
.running-file span{color:var(--primary);font-weight:600}
.ctrl-group{display:flex;gap:8px;flex-wrap:wrap}
.file-select{flex:1;min-width:120px;padding:8px 12px;background:var(--bg);border:2px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:12px;font-family:'Inter',sans-serif;outline:none;transition:0.3s}
.file-select:focus{border-color:var(--primary)}
.upload-zone{border:2px dashed var(--border);border-radius:var(--radius-sm);padding:20px;text-align:center;cursor:pointer;transition:0.3s}
.upload-zone:hover{border-color:var(--primary);background:var(--primary3)}
.upload-zone.drag-over{border-color:var(--primary);background:var(--primary3)}
.upload-zone i{font-size:28px;color:var(--primary);display:block;margin-bottom:6px}
.upload-zone p{font-size:11px;color:var(--text3);font-weight:500}
.upload-zone input{display:none}
.file-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;max-height:120px;overflow-y:auto}
.file-chip{background:var(--surface2);border:2px solid var(--border);border-radius:50px;padding:3px 12px 3px 10px;display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:600;font-family:monospace;transition:0.2s}
.file-chip:hover{border-color:var(--primary)}
.file-chip i{color:var(--primary);font-size:10px}
.file-chip .actions{display:flex;gap:2px}
.file-chip .actions a,.file-chip .actions button{background:transparent;border:none;color:var(--text3);cursor:pointer;padding:1px 4px;border-radius:4px;font-size:9px;transition:0.2s}
.file-chip .actions a:hover{color:var(--primary)}
.file-chip .actions button:hover{color:#dc2626}
.terminal{background:var(--bg);border-radius:var(--radius-sm);overflow:hidden;border:2px solid var(--border);margin-top:6px}
.term-bar{display:flex;align-items:center;gap:6px;padding:6px 14px;background:var(--surface2);border-bottom:2px solid var(--border)}
.term-dot{width:8px;height:8px;border-radius:50%}
.term-dot.red{background:#dc2626}
.term-dot.yellow{background:#d97706}
.term-dot.green{background:#16a34a}
.term-title{margin-left:4px;font-size:9px;color:var(--text3);font-weight:600;letter-spacing:1px}
.term-body{padding:10px 14px;max-height:150px;overflow-y:auto;font-family:monospace;font-size:10px;line-height:1.8;white-space:pre-wrap;word-break:break-word;color:#1a1a2e;background:#f8f9fa}
.term-body::-webkit-scrollbar{width:3px}
.term-body::-webkit-scrollbar-track{background:transparent}
.term-body::-webkit-scrollbar-thumb{background:var(--primary2);border-radius:10px}
.install-row{display:flex;gap:8px}
.install-row input{flex:1;padding:8px 12px;border-radius:var(--radius-sm);background:var(--bg);border:2px solid var(--border);color:var(--text);font-size:11px;font-family:'Inter',sans-serif;outline:none;transition:0.3s}
.install-row input:focus{border-color:var(--primary)}
.install-row input::placeholder{color:var(--text3)}
/* Auto-restart toggle */
.recovery-bar{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--surface2);border:2px solid var(--border);border-radius:var(--radius-sm);margin-bottom:10px;transition:0.3s}
.recovery-bar.on{border-color:#bbf7d0;background:#f0fdf4}
.recovery-bar i.heart{color:var(--primary);font-size:14px}
.recovery-bar.on i.heart{color:#16a34a}
.recovery-label{font-size:11px;font-weight:600;flex:1;color:var(--text2)}
.recovery-badge{font-size:9px;color:var(--text3);font-weight:700;letter-spacing:0.5px;padding:2px 8px;border-radius:50px;background:var(--bg);border:1px solid var(--border);text-transform:uppercase}
.recovery-badge.on{color:#16a34a;background:#dcfce7;border-color:#bbf7d0}
.switch{position:relative;width:38px;height:20px;display:inline-block;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#ccc;transition:.3s;border-radius:20px}
.slider:before{content:'';position:absolute;height:14px;width:14px;left:3px;bottom:3px;background:white;transition:.3s;border-radius:50%}
.switch input:checked + .slider{background:linear-gradient(135deg,var(--primary),var(--primary2))}
.switch input:checked + .slider:before{transform:translateX(18px)}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
@media(max-width:600px){.glass-nav{padding:0.6rem 1rem}.wrap{padding:8px 12px}}
</style>
</head>
<body>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span></div>
<div class="user-info">
<div class="user-badge"><i class="fas fa-user-astronaut"></i><span class="uname">{{ username }}</span>{% if expires_at %}<span class="expiry">⚡ {{ expires_at|timestamp_to_date }}</span>{% endif %}</div>
<a href="/logout" class="btn btn-danger btn-sm"><i class="fas fa-sign-out-alt"></i> Logout</a>
</div>
</nav>
<div class="wrap">
<div class="grid">
<div class="card">
<div class="card-header"><i class="fas fa-server"></i> Server</div>
<div class="status-row"><div class="status-dot {% if running %}running{% else %}stopped{% endif %}"></div><span class="status-label {% if running %}running{% else %}stopped{% endif %}">{% if running %}● Running{% else %}● Stopped{% endif %}</span></div>
{% if running_file %}<div class="running-file">Active: <span>{{ running_file }}</span></div>{% endif %}

<!-- AUTO RESTART TOGGLE -->
<div class="recovery-bar {% if auto_restart %}on{% endif %}" id="recoveryBar">
<i class="fas fa-heartbeat heart"></i>
<span class="recovery-label">Crash Recovery</span>
<span class="recovery-badge {% if auto_restart %}on{% endif %}" id="restartBadge">
{% if auto_restart %}ON{% if restart_count %} • {{ restart_count }}×{% endif %}{% else %}OFF{% endif %}
</span>
<label class="switch">
<input type="checkbox" id="autoRestartToggle" {% if auto_restart %}checked{% endif %} onchange="toggleAutoRestart(this)">
<span class="slider"></span>
</label>
</div>

<div class="ctrl-group">
<select class="file-select" id="fileSelect">
<option value="">— Select —</option>
{% for f in files %}<option value="{{ f }}" {% if running_file == f %}selected{% endif %}>{{ f }}</option>{% endfor %}
</select>
</div>
<div class="ctrl-group">
<button class="btn btn-success" onclick="startServer()"><i class="fas fa-play"></i> Start</button>
<button class="btn btn-danger" onclick="stopServer()"><i class="fas fa-stop"></i> Stop</button>
<button class="btn btn-warning" onclick="restartServer()"><i class="fas fa-sync"></i> Restart</button>
<button class="btn btn-outline btn-sm" onclick="deleteServer()"><i class="fas fa-trash"></i></button>
</div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-cloud-upload-alt"></i> Files</div>
<div class="upload-zone" id="uploadZone"><i class="fas fa-cloud-upload-alt"></i><p>Drag &amp; drop or click to upload</p><input type="file" id="fileInput" multiple></div>
<div class="file-list" id="fileList">
{% for f in files %}
<div class="file-chip"><i class="fas fa-file-code"></i> {{ f }}<span class="actions"><a href="/file/view/{{ f }}" target="_blank"><i class="fas fa-eye"></i></a><a href="/download/{{ f }}" target="_blank"><i class="fas fa-download"></i></a><form method="POST" action="/file/delete/{{ f }}" style="display:inline" onsubmit="return confirm('Delete?')"><button type="submit"><i class="fas fa-times"></i></button></form></span></div>
{% endfor %}
</div>
</div>
</div>
<div class="grid">
<div class="card">
<div class="card-header"><i class="fas fa-cubes"></i> Install</div>
<div class="install-row"><input type="text" id="installCmd" placeholder="pip install"><button class="btn btn-primary" onclick="installModule()"><i class="fas fa-download"></i></button></div>
<div class="terminal"><div class="term-bar"><span class="term-dot red"></span><span class="term-dot yellow"></span><span class="term-dot green"></span><span class="term-title">Install Log</span></div><div class="term-body" id="installOutput">— Ready —</div></div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-terminal"></i> Logs <button class="btn btn-outline btn-sm" style="margin-left:auto;padding:2px 10px;font-size:8px" onclick="refreshLogs()"><i class="fas fa-sync"></i></button></div>
<div class="terminal" style="border:none;border-radius:0;margin-top:0"><div class="term-body" id="logOutput" style="max-height:180px">[System] Waiting…</div></div>
</div>
</div>
</div>
<script>
const fileSelect=document.getElementById('fileSelect');
const logOutput=document.getElementById('logOutput');
const installOutput=document.getElementById('installOutput');
const uploadZone=document.getElementById('uploadZone');
const fileInput=document.getElementById('fileInput');

function showToast(msg,type='success'){const el=document.createElement('div');el.style.cssText=`position:fixed;top:70px;right:16px;z-index:9999;padding:12px 20px;border-radius:10px;font-size:12px;font-weight:600;backdrop-filter:blur(20px);border:2px solid ${type==='success'?'#bbf7d0':'#fecaca'};background:${type==='success'?'#dcfce7':'#fee2e2'};color:${type==='success'?'#16a34a':'#dc2626'};animation:slideIn 0.3s ease;box-shadow:0 8px 32px rgba(0,0,0,0.06)`;el.textContent=msg;document.body.appendChild(el);setTimeout(()=>el.remove(),3000)}
const style=document.createElement('style');style.textContent='@keyframes slideIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}';document.head.appendChild(style);

function getFile(){return fileSelect.value||prompt('Filename:')}
function startServer(){const f=getFile();if(!f)return;fetch('/server/start',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(f)}).then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),800)})}
function stopServer(){if(!confirm('Stop process?'))return;fetch('/server/stop',{method:'POST'}).then(()=>location.reload())}
function restartServer(){const f=getFile();if(!f)return;fetch('/server/restart',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(f)}).then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),800)})}
function deleteServer(){if(!confirm('Delete process?'))return;fetch('/server/delete',{method:'POST'}).then(()=>location.reload())}
function installModule(){const cmd=document.getElementById('installCmd').value.trim();if(!cmd)return;installOutput.textContent='Installing…';fetch('/install',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'command='+encodeURIComponent(cmd)}).then(r=>r.json()).then(d=>{installOutput.textContent=d.msg;showToast(d.msg,d.ok)})}

function toggleAutoRestart(el){
    fetch('/server/autorestart',{
        method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'enabled='+(el.checked?'true':'false')
    }).then(r=>r.json()).then(d=>{
        if(d.ok){
            showToast('Crash Recovery: '+(d.enabled?'ON':'OFF'));
            updateRecoveryUI(d.enabled, 0);
        } else {
            showToast(d.msg||'Error','error');
            el.checked = !el.checked;
        }
    });
}
function updateRecoveryUI(on, count){
    const bar = document.getElementById('recoveryBar');
    const badge = document.getElementById('restartBadge');
    const toggle = document.getElementById('autoRestartToggle');
    if(bar) bar.classList.toggle('on', !!on);
    if(badge){
        badge.classList.toggle('on', !!on);
        badge.textContent = on ? ('ON' + (count>0?' • '+count+'×':'')) : 'OFF';
    }
    if(toggle && toggle !== document.activeElement) toggle.checked = !!on;
}

function refreshLogs(){
    fetch('/logs').then(r=>r.json()).then(d=>{
        logOutput.innerHTML = d.logs && d.logs.length ? d.logs.join('\\n') : '[System] No output';
        if(d.install && d.install.length) installOutput.innerHTML = d.install.join('\\n');
        // Auto-scroll to bottom
        logOutput.scrollTop = logOutput.scrollHeight;
        updateRecoveryUI(d.auto_restart, d.restart_count);
    });
}

uploadZone.addEventListener('click',()=>fileInput.click());
uploadZone.addEventListener('dragover',e=>{e.preventDefault();uploadZone.classList.add('drag-over')});
uploadZone.addEventListener('dragleave',()=>uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop',e=>{e.preventDefault();uploadZone.classList.remove('drag-over');if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files)});
fileInput.addEventListener('change',function(){if(this.files.length)uploadFiles(this.files);this.value=''});
function uploadFiles(files){const fd=new FormData();for(let f of files)fd.append('files',f);fetch('/upload',{method:'POST',body:fd}).then(()=>location.reload())}

refreshLogs();setInterval(refreshLogs,4000);
</script>
</body>
</html>"""

# ============================================
#  MAIN
# ============================================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 NEXUS VPS PANEL - PREMIUM LIGHT EDITION")
    print("="*70)
    print(f"📍 LOCAL:  http://127.0.0.1:5000")
    print(f"📍 NETWORK: http://0.0.0.0:5000")
    print(f"👤 OWNER:  {OWNER_USER} / {OWNER_PASS}")
    print(f"📁 MAX UPLOAD: 10MB | STORAGE: UNLIMITED")
    print(f"💚 AUTO-RESTART: Max {MAX_RESTARTS} restarts / {RESTART_WINDOW}s window")
    print("="*70 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)