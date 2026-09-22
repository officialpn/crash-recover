"""
NEXUS VPS PANEL - ULTRA PREMIUM EDITION
Complete Flask Application with Persistent Storage + Auto-Restart Crash Recovery
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
OWNER_PASS = "9644"

DEFAULT_PRICING = {
    "currency": "₹",
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
#  PROCESS MANAGER
# ============================================
PROCS = {}

def start_process(username, filename):
    stop_process(username, manual=False)
    st = _init_auto_state(username)
    st["manual_stop"] = False
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
            bufsize=1
        )
    except FileNotFoundError:
        return False, "RUNTIME NOT INSTALLED"
    
    logs = deque(maxlen=2000)
    logs.append(f"[START] {' '.join(cmd)}")
    PROCS[username] = {"proc": proc, "logs": logs, "file": filename}
    
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

def stop_process(username, manual=True):
    info = PROCS.get(username)
    if not info:
        return False
    if manual:
        st = _init_auto_state(username)
        st["manual_stop"] = True
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

def is_running(username):
    info = PROCS.get(username)
    return bool(info and info["proc"].poll() is None)

def get_logs(username):
    info = PROCS.get(username)
    return list(info["logs"]) if info else []

# ============================================
#  AUTO-RESTART CRASH RECOVERY ENGINE
# ============================================
RESTART_ENGINE_ENABLED = True
RESTART_DELAY = 3
RESTART_CHECK_INTERVAL = 3
MAX_AUTO_RESTARTS = 50

AUTO_STATE = {}

def _init_auto_state(username):
    AUTO_STATE.setdefault(username, {
        "enabled": False,
        "count": 0,
        "crash_count": 0,
        "last_restart": 0,
        "manual_stop": False,
    })
    return AUTO_STATE[username]

def set_auto_restart(username, enabled):
    st = _init_auto_state(username)
    st["enabled"] = bool(enabled)
    if enabled:
        st["manual_stop"] = False
        st["count"] = 0
        st["crash_count"] = 0
    return st

def get_auto_state(username):
    return _init_auto_state(username)

def _engine_loop():
    while True:
        try:
            time.sleep(RESTART_CHECK_INTERVAL)
            if not RESTART_ENGINE_ENABLED:
                continue

            for username in list(PROCS.keys()):
                info = PROCS.get(username)
                if not info:
                    continue
                st = _init_auto_state(username)
                if not st["enabled"]:
                    continue
                if st["manual_stop"]:
                    continue

                proc = info["proc"]
                if proc.poll() is None:
                    continue

                st["crash_count"] += 1
                info["logs"].append(
                    f"[ENGINE] CRASH DETECTED (code={proc.returncode}) — "
                    f"RESTARTING IN {RESTART_DELAY}s…"
                )

                if st["count"] >= MAX_AUTO_RESTARTS:
                    info["logs"].append(
                        f"[ENGINE] MAX AUTO-RESTARTS ({MAX_AUTO_RESTARTS}) REACHED — DISABLING"
                    )
                    st["enabled"] = False
                    continue

                time.sleep(RESTART_DELAY)

                if st["manual_stop"] or not st["enabled"]:
                    continue

                filename = info.get("file")
                if not filename:
                    continue

                ok, msg = start_process(username, filename)
                if ok:
                    st["count"] += 1
                    st["last_restart"] = time.time()
                    PROCS[username]["logs"].append(
                        f"[ENGINE] AUTO-RESTART #{st['count']} SUCCESSFUL"
                    )
                else:
                    PROCS[username]["logs"].append(
                        f"[ENGINE] AUTO-RESTART FAILED: {msg}"
                    )
        except Exception as e:
            try:
                print(f"[ENGINE ERROR] {e}")
            except Exception:
                pass

threading.Thread(target=_engine_loop, daemon=True).start()

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
        stop_process(username)
        AUTO_STATE.pop(username, None)
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
        pricing["currency"] = request.form.get("currency", "₹").strip() or "₹"
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
    st = get_auto_state(username)
    
    return render_template_string(
        HTML_USER,
        username=username,
        info=info,
        files=files,
        running=is_running(username),
        running_file=PROCS.get(username, {}).get("file") if is_running(username) else None,
        expires_at=info.get("expires_at", 0),
        now=time.time(),
        pricing=pricing,
        auto_enabled=st["enabled"],
        auto_count=st["count"],
        auto_crashes=st["crash_count"],
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
    stop_process(username)
    time.sleep(0.3)
    ok, msg = start_process(username, filename)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/server/delete", methods=["POST"])
@require_user
def server_delete():
    username = current_user()
    stop_process(username)
    PROCS.pop(username, None)
    AUTO_STATE.pop(username, None)
    return jsonify({"ok": True})

@app.route("/server/auto", methods=["POST"])
@require_user
def server_auto():
    username = current_user()
    enabled = request.form.get("enabled", "0") in ("1", "true", "on", "yes")
    if enabled and not is_running(username):
        return jsonify({"ok": False, "msg": "START SERVER FIRST"})
    st = set_auto_restart(username, enabled)
    return jsonify({
        "ok": True,
        "enabled": st["enabled"],
        "count": st["count"],
        "crash_count": st["crash_count"],
    })

@app.route("/server/auto/status")
@require_user
def server_auto_status():
    username = current_user()
    st = get_auto_state(username)
    return jsonify({
        "enabled": st["enabled"],
        "count": st["count"],
        "crash_count": st["crash_count"],
        "last_restart": st["last_restart"],
        "running": is_running(username),
    })

@app.route("/logs")
@require_user
def logs_api():
    username = current_user()
    st = get_auto_state(username)
    return jsonify({
        "running": is_running(username),
        "file": PROCS.get(username, {}).get("file"),
        "logs": get_logs(username),
        "install": get_install_logs(username),
        "auto": {
            "enabled": st["enabled"],
            "count": st["count"],
            "crash_count": st["crash_count"],
        },
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
#  HTML TEMPLATES - PREMIUM RED THEME
# ============================================
HTML_LANDING = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — ULTRA PREMIUM</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--primary:#FF1744;--secondary:#D50000;--accent:#FF5252;--dark:#0A0000;--card:rgba(20,0,0,0.85);--brd:rgba(255,23,68,0.15);--gold:#FFD700;--txt:#FFFFFF;--mt:rgba(255,255,255,0.5)}
body{font-family:'Rajdhani',sans-serif;background:var(--dark);color:var(--txt);min-height:100vh;overflow-x:hidden;text-transform:uppercase;letter-spacing:1px}
canvas#bg{position:fixed;inset:0;z-index:0;opacity:0.3;pointer-events:none}
.mesh{position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse 70% 55% at 80% -10%,rgba(255,23,68,0.08),transparent),radial-gradient(ellipse 60% 50% at -10% 90%,rgba(213,0,0,0.08),transparent)}
.wrap{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:0 24px}
.glass-nav{position:sticky;top:0;z-index:50;backdrop-filter:blur(28px);background:rgba(10,0,0,0.8);border-bottom:2px solid var(--primary);padding:0.8rem 2rem;display:flex;align-items:center;justify-content:center}
.brand{display:flex;align-items:center;gap:12px}
.brand-icon{width:44px;height:44px;border-radius:12px;background:linear-gradient(135deg,var(--primary),var(--secondary));display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:900;color:#fff;box-shadow:0 0 30px rgba(255,23,68,0.4);font-family:'Orbitron',sans-serif}
.brand-text{font-size:22px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Orbitron',sans-serif;letter-spacing:3px}
.hero{min-height:80vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:40px 20px}
.eyebrow{display:inline-flex;align-items:center;gap:10px;padding:8px 24px;background:rgba(255,23,68,0.08);border:1px solid rgba(255,23,68,0.2);border-radius:50px;margin-bottom:30px}
.eyebrow .dot{width:8px;height:8px;border-radius:50%;background:var(--primary);animation:pulse 2s infinite;box-shadow:0 0 10px var(--primary)}
.eyebrow span{font-size:11px;font-weight:700;letter-spacing:4px;text-transform:uppercase;color:var(--primary);font-family:'Orbitron',sans-serif}
h1{font-size:clamp(40px,8vw,80px);font-weight:900;line-height:1;letter-spacing:-2px;margin-bottom:18px;font-family:'Orbitron',sans-serif}
h1 .highlight{background:linear-gradient(135deg,var(--primary),var(--gold),var(--accent));background-size:300% 300%;-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:gradient 4s ease infinite}
.sub{font-size:16px;color:var(--mt);max-width:560px;margin:0 auto 30px;line-height:1.8;font-weight:500;letter-spacing:2px}
.btn-main{padding:16px 40px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;text-decoration:none;font-weight:700;font-size:13px;letter-spacing:2px;text-transform:uppercase;transition:0.3s;box-shadow:0 4px 24px rgba(255,23,68,0.3);font-family:'Orbitron',sans-serif;display:inline-block}
.btn-main:hover{transform:translateY(-3px) scale(1.05);box-shadow:0 12px 48px rgba(255,23,68,0.5);color:#fff}
.pricing-section{width:100%;max-width:1000px;margin:20px auto 30px}
.pricing-title{font-family:'Orbitron',sans-serif;font-size:18px;font-weight:700;letter-spacing:4px;color:var(--gold);margin-bottom:16px;text-align:center}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px}
.plan-card{background:var(--card);border:2px solid var(--brd);border-radius:12px;padding:18px 16px;text-align:center;transition:0.3s;backdrop-filter:blur(10px)}
.plan-card:hover{border-color:var(--primary);transform:translateY(-4px);box-shadow:0 12px 40px rgba(255,23,68,0.1)}
.plan-card.hot{border-color:var(--gold);background:rgba(255,215,0,0.03)}
.plan-card .hot-tag{font-size:8px;color:var(--gold);letter-spacing:2px;font-family:'Orbitron',sans-serif;margin-bottom:4px}
.plan-card .pname{font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;letter-spacing:2px;color:var(--gold)}
.plan-card .pprice{font-family:'Orbitron',sans-serif;font-size:26px;font-weight:900;color:var(--primary);margin:4px 0}
.plan-card .pdur{font-size:10px;color:var(--mt);letter-spacing:2px}
.plan-card .pfeat{font-size:9px;color:var(--mt);letter-spacing:1px;margin-top:4px}
.contact-bar{padding:10px 20px;background:rgba(255,215,0,0.03);border:2px solid rgba(255,215,0,0.08);border-radius:10px;margin-top:14px;text-align:center;font-size:12px;color:var(--mt);letter-spacing:2px}
.contact-bar strong{color:var(--gold)}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;width:100%;max-width:1000px;margin-top:20px}
.feature{background:var(--card);border:1px solid var(--brd);border-radius:12px;padding:24px 18px;text-align:center;backdrop-filter:blur(20px);transition:0.3s}
.feature:hover{border-color:var(--primary);transform:translateY(-4px)}
.feature i{font-size:32px;color:var(--primary);margin-bottom:10px}
.feature h3{font-size:13px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;font-family:'Orbitron',sans-serif}
.feature p{font-size:11px;color:var(--mt);letter-spacing:1px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
@keyframes gradient{0%,100%{background-position:0% 50%}50%{background-position:100% 50%}}
@media(max-width:768px){.glass-nav{padding:0.8rem 1rem}.wrap{padding:0 16px}h1{font-size:clamp(30px,7vw,50px)}.pricing-grid{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.pricing-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<canvas id="bg"></canvas>
<div class="mesh"></div>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span></div>
</nav>
<div class="wrap">
<section class="hero">
<div class="eyebrow"><div class="dot"></div><span>NEXT-GEN VPS INFRASTRUCTURE</span></div>
<h1>DEPLOY &amp; RUN<br><span class="highlight">YOUR APPS</span></h1>
<p class="sub">UPLOAD, MANAGE &amp; MONITOR PYTHON, NODE.JS, AND SHELL SCRIPTS WITH REAL-TIME LOGS</p>
<a href="/login" class="btn-main"><i class="fas fa-arrow-right"></i> LAUNCH PANEL</a>
<div class="pricing-section">
<div class="pricing-title"><i class="fas fa-tags"></i> PRICING PLANS</div>
<div class="pricing-grid">
{% for plan in pricing.plans %}
<div class="plan-card {% if loop.index == 3 %}hot{% endif %}">
{% if loop.index == 3 %}<div class="hot-tag"><i class="fas fa-star"></i> POPULAR</div>{% endif %}
<div class="pname">{{ plan.name }}</div>
<div class="pprice">{{ pricing.currency }}{{ plan.price }}</div>
<div class="pdur">{{ plan.duration }}</div>
<div class="pfeat">{{ plan.features }}</div>
</div>
{% endfor %}
</div>
<div class="contact-bar"><i class="fas fa-headset"></i> CONTACT: <strong>{{ pricing.contact }}</strong></div>
</div>
<div class="features">
<div class="feature"><i class="fas fa-server"></i><h3>99.9% UPTIME</h3><p>ENTERPRISE-GRADE</p></div>
<div class="feature"><i class="fas fa-bolt"></i><h3>INSTANT DEPLOY</h3><p>UNDER 1 SECOND</p></div>
<div class="feature"><i class="fas fa-upload"></i><h3>200MB UPLOAD</h3><p>LARGE FILES</p></div>
<div class="feature"><i class="fas fa-code"></i><h3>5+ LANGUAGES</h3><p>PYTHON, NODE, SHELL</p></div>
<div class="feature"><i class="fas fa-heartbeat"></i><h3>AUTO-RESTART</h3><p>CRASH RECOVERY</p></div>
</div>
</section>
</div>
<script>
const c=document.getElementById('bg'),ctx=c.getContext('2d');
let W,H,p=[];
function resize(){W=c.width=innerWidth;H=c.height=innerHeight}
class P{constructor(){this.reset()}reset(){this.x=Math.random()*W;this.y=Math.random()*H;this.vx=(Math.random()-.5)*0.3;this.vy=(Math.random()-.5)*0.3;this.r=Math.random()*1.6+0.4;this.a=Math.random()*0.25+0.06;this.col='255,23,68'}update(){this.x+=this.vx;this.y+=this.vy;if(this.x<0||this.x>W||this.y<0||this.y>H)this.reset()}draw(){ctx.beginPath();ctx.arc(this.x,this.y,this.r,0,Math.PI*2);ctx.fillStyle=`rgba(${this.col},${this.a})`;ctx.fill()}}
function init(){p=[];for(let i=0;i<80;i++)p.push(new P())}
function loop(){ctx.clearRect(0,0,W,H);p.forEach(d=>{d.update();d.draw()});requestAnimationFrame(loop)}
window.addEventListener('resize',()=>{resize();init()});resize();init();loop();
</script>
</body>
</html>"""

HTML_LOGIN = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — SECURE ACCESS</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--primary:#FF1744;--secondary:#D50000;--gold:#FFD700;--dark:#0A0000;--card:rgba(20,0,0,0.9);--brd:rgba(255,23,68,0.15);--mt:rgba(255,255,255,0.4)}
body{font-family:'Rajdhani',sans-serif;background:var(--dark);color:#fff;min-height:100vh;display:flex;align-items:center;justify-content:center;overflow:hidden;text-transform:uppercase;letter-spacing:1px}
canvas#bg{position:fixed;inset:0;z-index:0;opacity:0.3;pointer-events:none}
.orb{position:fixed;border-radius:50%;filter:blur(120px);pointer-events:none;z-index:0}
.o1{width:400px;height:400px;top:-150px;right:-100px;background:rgba(255,23,68,0.1)}
.o2{width:400px;height:400px;bottom:-150px;left:-100px;background:rgba(213,0,0,0.1)}
.box{position:relative;z-index:1;width:100%;max-width:420px;margin:0 20px;animation:rise 0.6s ease}
.box-inner{background:var(--card);backdrop-filter:blur(36px);border-radius:16px;padding:48px 36px 40px;border:2px solid var(--brd);box-shadow:0 32px 80px rgba(0,0,0,0.6),inset 0 0 60px rgba(255,23,68,0.03)}
.logo{text-align:center;margin-bottom:36px}
.logo-icon{width:60px;height:60px;border-radius:14px;background:linear-gradient(135deg,var(--primary),var(--secondary));display:inline-flex;align-items:center;justify-content:center;font-size:26px;font-weight:900;color:#fff;box-shadow:0 0 40px rgba(255,23,68,0.3);margin-bottom:14px;font-family:'Orbitron',sans-serif}
.logo-text{font-size:28px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Orbitron',sans-serif;letter-spacing:3px}
.logo-sub{font-size:11px;color:var(--mt);letter-spacing:4px;text-transform:uppercase;margin-top:4px}
.error{background:rgba(255,23,68,0.08);border:2px solid rgba(255,23,68,0.2);border-radius:10px;padding:12px 16px;margin-bottom:24px;color:#FF1744;font-size:12px;font-weight:700;text-align:center;letter-spacing:2px}
.field{margin-bottom:20px}
.field label{display:block;font-size:10px;font-weight:700;color:var(--mt);text-transform:uppercase;letter-spacing:3px;margin-bottom:8px;font-family:'Orbitron',sans-serif}
.field input{width:100%;padding:14px 16px;border-radius:10px;background:rgba(255,255,255,0.03);border:2px solid var(--brd);color:#fff;font-size:14px;font-family:'Rajdhani',sans-serif;outline:none;transition:0.3s;text-transform:uppercase;letter-spacing:1px}
.field input:focus{border-color:var(--primary);background:rgba(255,23,68,0.03);box-shadow:0 0 0 4px rgba(255,23,68,0.05)}
.btn-submit{width:100%;padding:16px;border:none;border-radius:10px;background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;font-size:14px;font-weight:800;letter-spacing:3px;text-transform:uppercase;cursor:pointer;transition:0.3s;font-family:'Orbitron',sans-serif;box-shadow:0 4px 24px rgba(255,23,68,0.3)}
.btn-submit:hover{transform:translateY(-2px);box-shadow:0 8px 40px rgba(255,23,68,0.5)}
.back{text-align:center;margin-top:20px}
.back a{color:var(--mt);text-decoration:none;font-size:11px;font-weight:600;letter-spacing:3px;text-transform:uppercase;transition:0.3s;font-family:'Orbitron',sans-serif}
.back a:hover{color:var(--primary)}
@keyframes rise{from{opacity:0;transform:translateY(30px) scale(0.97)}to{opacity:1;transform:translateY(0) scale(1)}}
</style>
</head>
<body>
<canvas id="bg"></canvas>
<div class="orb o1"></div><div class="orb o2"></div>
<div class="box">
<div class="box-inner">
<div class="logo"><div class="logo-icon">NX</div><div class="logo-text">NEXUS VPS</div><div class="logo-sub">SECURE ACCESS PORTAL</div></div>
{% if error %}<div class="error"><i class="fas fa-exclamation-triangle"></i> {{ error }}</div>{% endif %}
<form method="POST">
<div class="field"><label><i class="fas fa-user"></i> USERNAME</label><input type="text" name="username" placeholder="ENTER USERNAME" required></div>
<div class="field"><label><i class="fas fa-lock"></i> PASSWORD</label><input type="password" name="password" placeholder="ENTER PASSWORD" required></div>
<button type="submit" class="btn-submit"><i class="fas fa-arrow-right-to-bracket"></i> ACCESS</button>
</form>
<div class="back"><a href="/"><i class="fas fa-arrow-left"></i> BACK TO HOME</a></div>
</div>
</div>
<script>
const c=document.getElementById('bg'),ctx=c.getContext('2d');
let W,H,p=[];
function resize(){W=c.width=innerWidth;H=c.height=innerHeight}
class P{constructor(){this.reset()}reset(){this.x=Math.random()*W;this.y=Math.random()*H;this.vx=(Math.random()-.5)*0.3;this.vy=(Math.random()-.5)*0.3;this.r=Math.random()*1.4+0.4;this.a=Math.random()*0.25+0.05}update(){this.x+=this.vx;this.y+=this.vy;if(this.x<0||this.x>W||this.y<0||this.y>H)this.reset()}draw(){ctx.beginPath();ctx.arc(this.x,this.y,this.r,0,Math.PI*2);ctx.fillStyle=`rgba(255,23,68,${this.a})`;ctx.fill()}}
function init(){p=[];for(let i=0;i<60;i++)p.push(new P())}
function loop(){ctx.clearRect(0,0,W,H);p.forEach(d=>{d.update();d.draw()});requestAnimationFrame(loop)}
window.addEventListener('resize',()=>{resize();init()});resize();init();loop();
</script>
</body>
</html>"""

HTML_OWNER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — OWNER PANEL</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--primary:#FF1744;--secondary:#D50000;--gold:#FFD700;--dark:#0A0000;--card:rgba(20,0,0,0.88);--brd:rgba(255,23,68,0.15);--mt:rgba(255,255,255,0.4);--txt:#FFFFFF}
body{font-family:'Rajdhani',sans-serif;background:var(--dark);color:var(--txt);min-height:100vh;text-transform:uppercase;letter-spacing:1px;overflow-y:auto}
canvas#bg{position:fixed;inset:0;z-index:0;opacity:0.25;pointer-events:none}
.mesh{position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse 60% 50% at 80% 0%,rgba(255,23,68,0.05),transparent),radial-gradient(ellipse 50% 40% at 0% 100%,rgba(213,0,0,0.05),transparent)}
.glass-nav{position:sticky;top:0;z-index:50;backdrop-filter:blur(28px);background:rgba(10,0,0,0.9);border-bottom:2px solid var(--primary);padding:0.6rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--secondary));display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:900;color:#fff;box-shadow:0 0 20px rgba(255,23,68,0.3);font-family:'Orbitron',sans-serif}
.brand-text{font-size:18px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Orbitron',sans-serif;letter-spacing:3px}
.badge-owner{padding:3px 14px;background:rgba(255,215,0,0.1);border:1px solid rgba(255,215,0,0.2);border-radius:50px;font-size:9px;font-weight:700;letter-spacing:3px;color:var(--gold);font-family:'Orbitron',sans-serif}
.nav-actions{display:flex;align-items:center;gap:10px}
.btn{padding:6px 16px;border-radius:6px;border:none;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;cursor:pointer;transition:0.3s;font-family:'Orbitron',sans-serif;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
.btn-danger{background:rgba(255,23,68,0.12);color:#FF5252;border:2px solid rgba(255,23,68,0.2)}
.btn-danger:hover{background:rgba(255,23,68,0.2)}
.btn-success{background:rgba(255,215,0,0.08);color:#FFD700;border:2px solid rgba(255,215,0,0.15)}
.btn-success:hover{background:rgba(255,215,0,0.15)}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;box-shadow:0 4px 16px rgba(255,23,68,0.2)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(255,23,68,0.4)}
.btn-sm{padding:4px 10px;font-size:9px}
.wrap{position:relative;z-index:1;max-width:1260px;margin:0 auto;padding:14px 24px}
.card{background:var(--card);backdrop-filter:blur(20px);border-radius:12px;border:2px solid var(--brd);padding:16px 20px;margin-bottom:14px;transition:0.3s}
.card:hover{border-color:rgba(255,23,68,0.25)}
.card-header{display:flex;align-items:center;gap:10px;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:3px;margin-bottom:12px;font-family:'Orbitron',sans-serif}
.card-header i{color:var(--primary)}
.card-header .badge-count{font-weight:400;color:var(--mt);font-size:9px;letter-spacing:2px}
.form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center}
.form-row input{padding:8px 14px;border-radius:6px;background:rgba(255,255,255,0.03);border:2px solid var(--brd);color:#fff;font-size:11px;font-family:'Rajdhani',sans-serif;flex:1;min-width:100px;max-width:200px;outline:none;transition:0.3s;text-transform:uppercase;letter-spacing:1px}
.form-row input:focus{border-color:var(--primary);background:rgba(255,23,68,0.03)}
.form-row input::placeholder{color:rgba(255,255,255,0.15)}
.form-row input[type="number"]{max-width:80px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12px}
th{padding:8px 12px;text-align:left;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:3px;color:var(--mt);border-bottom:2px solid var(--brd);font-family:'Orbitron',sans-serif;position:sticky;top:0;background:rgba(10,0,0,0.95);z-index:2}
td{padding:8px 12px;border-bottom:1px solid rgba(255,255,255,0.02)}
tr:hover td{background:rgba(255,23,68,0.02)}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 12px;border-radius:50px;font-size:8px;font-weight:700;letter-spacing:2px;text-transform:uppercase;font-family:'Orbitron',sans-serif}
.badge-active{background:rgba(255,215,0,0.08);color:#FFD700;border:1px solid rgba(255,215,0,0.15)}
.badge-expired{background:rgba(255,23,68,0.08);color:#FF5252;border:1px solid rgba(255,23,68,0.15)}
.badge-soon{background:rgba(255,23,68,0.05);color:#FF8A80;border:1px solid rgba(255,23,68,0.1)}
.link{font-size:9px;color:rgba(255,255,255,0.2);font-family:'Orbitron',monospace;text-decoration:none;padding:2px 6px;border-radius:4px;background:rgba(255,255,255,0.02);transition:0.3s;word-break:break-all}
.link:hover{color:var(--primary);background:rgba(255,23,68,0.05)}
.actions{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.actions form{display:inline}
.actions input[type="number"]{width:44px;padding:3px 6px;border-radius:4px;background:rgba(255,255,255,0.03);border:2px solid var(--brd);color:#fff;font-size:10px;text-align:center;outline:none}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-top:10px}
.plan-card{background:rgba(255,255,255,0.02);border:2px solid var(--brd);border-radius:10px;padding:14px 12px;text-align:center;transition:0.3s}
.plan-card:hover{border-color:var(--primary)}
.plan-card .pname{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:700;letter-spacing:2px;color:var(--gold)}
.plan-card .pprice{font-family:'Orbitron',sans-serif;font-size:22px;font-weight:900;color:var(--primary);margin:4px 0}
.plan-card .pdur{font-size:9px;color:var(--mt);letter-spacing:2px}
.plan-card .pfeat{font-size:8px;color:var(--mt);letter-spacing:1px;margin-top:4px}
.plan-card.hot{border-color:var(--gold);background:rgba(255,215,0,0.03)}
.plan-card .hot-tag{font-size:7px;color:var(--gold);letter-spacing:2px;font-family:'Orbitron',sans-serif}
.plan-card input{width:100%;padding:4px 8px;border-radius:4px;background:rgba(255,255,255,0.02);border:1px solid var(--brd);color:#fff;font-size:9px;text-align:center;font-family:'Rajdhani',sans-serif;margin-bottom:3px;outline:none}
.plan-card input:focus{border-color:var(--primary)}
.contact-bar{padding:8px 16px;background:rgba(255,215,0,0.03);border:2px solid rgba(255,215,0,0.08);border-radius:8px;margin-top:10px;text-align:center;font-size:11px;color:var(--mt);letter-spacing:2px}
.contact-bar strong{color:var(--gold)}
.row2{display:grid;grid-template-columns:1.4fr 0.6fr;gap:14px}
@media(max-width:900px){.row2{grid-template-columns:1fr}}
@media(max-width:600px){.glass-nav{padding:0.6rem 1rem}.wrap{padding:10px 16px}.form-row{flex-direction:column}.form-row input{max-width:100%}}
</style>
</head>
<body>
<canvas id="bg"></canvas><div class="mesh"></div>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span><span class="badge-owner"><i class="fas fa-crown"></i> OWNER</span></div>
<div class="nav-actions"><a href="/logout" class="btn btn-danger btn-sm"><i class="fas fa-sign-out-alt"></i> LOGOUT</a></div>
</nav>
<div class="wrap">
<div class="card">
<div class="card-header" style="justify-content:center"><i class="fas fa-user-plus"></i> CREATE USER</div>
<form method="POST" action="/owner/create" class="form-row">
<input type="text" name="username" placeholder="USERNAME" required>
<input type="text" name="password" placeholder="PASSWORD" required>
<input type="number" name="days" placeholder="DAYS" value="7" min="1">
<button type="submit" class="btn btn-primary"><i class="fas fa-plus"></i> CREATE</button>
</form>
</div>

<div class="row2">
<div class="card" style="overflow:visible">
<div class="card-header"><i class="fas fa-users"></i> USERS <span class="badge-count">({{ users|length }})</span></div>
<div class="table-wrap" style="max-height:400px;overflow-y:auto">
<table>
<thead><tr><th>USER</th><th>PASS</th><th>EXPIRES</th><th>STATUS</th><th>LINK</th><th>ACTIONS</th></tr></thead>
<tbody>
{% for username, info in users.items() %}
<tr>
<td><strong style="color:var(--primary);font-size:11px">{{ username }}</strong></td>
<td><span style="font-family:'Orbitron',monospace;font-size:10px;color:var(--gold)">{{ info.password }}</span></td>
<td style="font-size:10px;color:var(--mt)">
{% if info.expires_at %}{{ time.strftime('%Y-%m-%d', time.localtime(info.expires_at)) }}{% else %}NEVER{% endif %}
</td>
<td>
{% if info.expires_at and info.expires_at < now %}
<span class="badge badge-expired">EXPIRED</span>
{% elif info.expires_at and info.expires_at < now + 86400*3 %}
<span class="badge badge-soon">SOON</span>
{% else %}
<span class="badge badge-active">ACTIVE</span>
{% endif %}
</td>
<td><a href="{{ base_url }}/auto/{{ info.token }}" target="_blank" class="link"><i class="fas fa-link"></i> {{ info.token[:10] }}…</a></td>
<td class="actions">
<form method="POST" action="/owner/extend/{{ username }}">
<input type="number" name="days" value="7" min="1">
<button type="submit" class="btn btn-success btn-sm"><i class="fas fa-clock"></i></button>
</form>
<form method="POST" action="/owner/delete/{{ username }}" onsubmit="return confirm('DELETE {{ username }}?')">
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
<div class="card-header"><i class="fas fa-tags"></i> PRICING</div>
<form method="POST" action="/owner/pricing">
<div class="form-row" style="margin-bottom:10px;justify-content:flex-start">
<input type="text" name="currency" placeholder="CURRENCY" value="{{ pricing.currency }}" style="max-width:70px">
<input type="text" name="contact" placeholder="CONTACT" value="{{ pricing.contact }}" style="flex:2;font-size:9px">
</div>
<div class="pricing-grid">
{% for plan in pricing.plans %}
<div class="plan-card {% if loop.index == 3 %}hot{% endif %}">
{% if loop.index == 3 %}<div class="hot-tag"><i class="fas fa-star"></i> POPULAR</div>{% endif %}
<input type="text" name="p_name" value="{{ plan.name }}" placeholder="NAME">
<input type="text" name="p_duration" value="{{ plan.duration }}" placeholder="DURATION">
<input type="text" name="p_price" value="{{ plan.price }}" placeholder="PRICE" style="color:var(--primary);font-weight:700">
<input type="text" name="p_features" value="{{ plan.features }}" placeholder="FEATURES" style="font-size:7px">
</div>
{% endfor %}
<div class="plan-card" style="border-style:dashed">
<div style="font-size:8px;color:var(--mt);letter-spacing:2px;margin-bottom:4px">NEW</div>
<input type="text" name="p_name" placeholder="NAME">
<input type="text" name="p_duration" placeholder="DURATION">
<input type="text" name="p_price" placeholder="PRICE" style="color:var(--primary);font-weight:700">
<input type="text" name="p_features" placeholder="FEATURES" style="font-size:7px">
</div>
</div>
<div class="contact-bar"><i class="fas fa-headset"></i> <strong>{{ pricing.contact }}</strong></div>
<button type="submit" class="btn btn-primary" style="margin-top:10px;width:100%;justify-content:center"><i class="fas fa-save"></i> SAVE</button>
</form>
</div>
</div>
</div>
<script>
const c=document.getElementById('bg'),ctx=c.getContext('2d');
let W,H,p=[];
function resize(){W=c.width=innerWidth;H=c.height=innerHeight}
class P{constructor(){this.reset()}reset(){this.x=Math.random()*W;this.y=Math.random()*H;this.vx=(Math.random()-.5)*0.3;this.vy=(Math.random()-.5)*0.3;this.r=Math.random()*1.6+0.4;this.a=Math.random()*0.25+0.06;this.col='255,23,68'}update(){this.x+=this.vx;this.y+=this.vy;if(this.x<0||this.x>W||this.y<0||this.y>H)this.reset()}draw(){ctx.beginPath();ctx.arc(this.x,this.y,this.r,0,Math.PI*2);ctx.fillStyle=`rgba(${this.col},${this.a})`;ctx.fill()}}
function init(){p=[];for(let i=0;i<70;i++)p.push(new P())}
function loop(){ctx.clearRect(0,0,W,H);p.forEach(d=>{d.update();d.draw()});requestAnimationFrame(loop)}
window.addEventListener('resize',()=>{resize();init()});resize();init();loop();
</script>
</body>
</html>"""

HTML_USER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXUS VPS — DASHBOARD</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;600;700;800;900&family=Rajdhani:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--primary:#FF1744;--secondary:#D50000;--gold:#FFD700;--dark:#0A0000;--card:rgba(20,0,0,0.85);--brd:rgba(255,23,68,0.12);--mt:rgba(255,255,255,0.4);--txt:#FFFFFF}
body{font-family:'Rajdhani',sans-serif;background:var(--dark);color:var(--txt);min-height:100vh;text-transform:uppercase;letter-spacing:1px}
canvas#bg{position:fixed;inset:0;z-index:0;opacity:0.25;pointer-events:none}
.mesh{position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse 60% 50% at 80% 0%,rgba(255,23,68,0.05),transparent),radial-gradient(ellipse 50% 40% at 0% 100%,rgba(213,0,0,0.05),transparent)}
.glass-nav{position:sticky;top:0;z-index:50;backdrop-filter:blur(28px);background:rgba(10,0,0,0.8);border-bottom:2px solid var(--primary);padding:0.6rem 2rem;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,var(--primary),var(--secondary));display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:900;color:#fff;box-shadow:0 0 20px rgba(255,23,68,0.3);font-family:'Orbitron',sans-serif}
.brand-text{font-size:18px;font-weight:900;background:linear-gradient(135deg,var(--primary),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-family:'Orbitron',sans-serif;letter-spacing:3px}
.user-info{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.user-badge{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,0.03);padding:4px 14px 4px 10px;border-radius:50px;border:2px solid var(--brd)}
.user-badge i{color:var(--primary);font-size:12px}
.user-badge .uname{font-size:12px;font-weight:700;font-family:'Orbitron',sans-serif;letter-spacing:2px}
.user-badge .expiry{font-size:9px;color:var(--mt);letter-spacing:2px}
.btn{padding:6px 16px;border-radius:6px;border:none;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;cursor:pointer;transition:0.3s;font-family:'Orbitron',sans-serif;text-decoration:none;display:inline-flex;align-items:center;gap:5px}
.btn-danger{background:rgba(255,23,68,0.12);color:#FF5252;border:2px solid rgba(255,23,68,0.2)}
.btn-danger:hover{background:rgba(255,23,68,0.2)}
.btn-success{background:rgba(255,215,0,0.08);color:#FFD700;border:2px solid rgba(255,215,0,0.15)}
.btn-success:hover{background:rgba(255,215,0,0.15)}
.btn-warning{background:rgba(255,23,68,0.08);color:#FF8A80;border:2px solid rgba(255,23,68,0.15)}
.btn-warning:hover{background:rgba(255,23,68,0.15)}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--secondary));color:#fff;box-shadow:0 4px 16px rgba(255,23,68,0.2)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(255,23,68,0.4)}
.btn-outline{background:transparent;border:2px solid var(--brd);color:var(--mt)}
.btn-outline:hover{border-color:var(--primary);color:var(--primary)}
.btn-sm{padding:4px 10px;font-size:9px}
.wrap{position:relative;z-index:1;max-width:1260px;margin:0 auto;padding:12px 20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.card{background:var(--card);backdrop-filter:blur(20px);border-radius:12px;border:2px solid var(--brd);padding:14px 18px;transition:0.3s}
.card:hover{border-color:rgba(255,23,68,0.2)}
.card-header{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:3px;margin-bottom:10px;font-family:'Orbitron',sans-serif}
.card-header i{color:var(--primary)}
.status-row{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.status-dot.running{background:var(--gold);box-shadow:0 0 20px rgba(255,215,0,0.4);animation:pulse 2s infinite}
.status-dot.stopped{background:var(--primary);box-shadow:0 0 20px rgba(255,23,68,0.3)}
@keyframes pulse{0%,100%{box-shadow:0 0 20px rgba(255,215,0,0.4)}50%{box-shadow:0 0 40px rgba(255,215,0,0.1)}}
.status-label{font-weight:700;font-size:12px;font-family:'Orbitron',sans-serif;letter-spacing:2px}
.status-label.running{color:var(--gold)}
.status-label.stopped{color:#FF5252}
.running-file{font-size:10px;font-family:'Orbitron',monospace;color:var(--mt);background:rgba(255,255,255,0.02);padding:4px 12px;border-radius:6px;border:2px solid var(--brd);display:inline-block;margin-bottom:10px}
.running-file span{color:var(--primary)}
.ctrl-group{display:flex;gap:8px;flex-wrap:wrap}
.file-select{flex:1;min-width:120px;padding:8px 12px;background:rgba(255,255,255,0.03);border:2px solid var(--brd);border-radius:6px;color:#fff;font-size:11px;font-family:'Rajdhani',sans-serif;outline:none;cursor:pointer;transition:0.3s;text-transform:uppercase;letter-spacing:1px}
.file-select:focus{border-color:var(--primary)}
.file-select option{background:#0A0000}
.upload-zone{border:2px dashed var(--brd);border-radius:10px;padding:20px;text-align:center;cursor:pointer;transition:0.3s}
.upload-zone:hover{border-color:var(--primary);background:rgba(255,23,68,0.02)}
.upload-zone.drag-over{border-color:var(--primary);background:rgba(255,23,68,0.05)}
.upload-zone i{font-size:28px;color:rgba(255,23,68,0.2);display:block;margin-bottom:6px}
.upload-zone p{font-size:10px;color:var(--mt);font-weight:600;letter-spacing:2px}
.upload-zone input{display:none}
.file-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.file-chip{background:rgba(255,255,255,0.03);border:2px solid var(--brd);border-radius:50px;padding:3px 12px 3px 10px;display:inline-flex;align-items:center;gap:6px;font-size:10px;font-weight:600;font-family:'Orbitron',monospace;transition:0.2s}
.file-chip:hover{border-color:rgba(255,23,68,0.3)}
.file-chip i{color:var(--primary);font-size:10px}
.file-chip .actions{display:flex;gap:2px}
.file-chip .actions a,.file-chip .actions button{background:transparent;border:none;color:var(--mt);cursor:pointer;padding:1px 4px;border-radius:4px;font-size:9px;transition:0.2s}
.file-chip .actions a:hover{color:var(--gold)}
.file-chip .actions button:hover{color:var(--primary)}
.terminal{background:#0A0000;border-radius:10px;overflow:hidden;border:2px solid rgba(255,23,68,0.08);margin-top:6px}
.term-bar{display:flex;align-items:center;gap:6px;padding:6px 14px;background:rgba(255,23,68,0.03);border-bottom:2px solid rgba(255,23,68,0.05)}
.term-dot{width:8px;height:8px;border-radius:50%}
.term-dot.red{background:#FF1744}
.term-dot.yellow{background:#FFD700}
.term-dot.green{background:#00E676}
.term-title{margin-left:4px;font-size:9px;color:var(--mt);letter-spacing:3px;font-family:'Orbitron',monospace}
.term-body{padding:10px 14px;max-height:150px;overflow-y:auto;font-family:'Orbitron',monospace;font-size:9px;line-height:1.8;white-space:pre-wrap;word-break:break-word;color:#A5F3E0;letter-spacing:0.5px}
.term-body::-webkit-scrollbar{width:3px}
.term-body::-webkit-scrollbar-track{background:transparent}
.term-body::-webkit-scrollbar-thumb{background:rgba(255,23,68,0.2);border-radius:10px}
.install-row{display:flex;gap:8px}
.install-row input{flex:1;padding:8px 12px;border-radius:6px;background:rgba(255,255,255,0.03);border:2px solid var(--brd);color:#fff;font-size:10px;font-family:'Orbitron',monospace;outline:none;transition:0.3s;text-transform:uppercase;letter-spacing:1px}
.install-row input:focus{border-color:var(--primary)}
.install-row input::placeholder{color:rgba(255,255,255,0.12);font-family:'Rajdhani',sans-serif;font-size:10px;letter-spacing:2px}
.auto-badge{display:inline-flex;align-items:center;gap:5px;font-size:9px;color:var(--mt);letter-spacing:2px;font-family:'Orbitron',sans-serif;align-self:center}
.auto-badge.on{color:var(--gold)}
.auto-badge .heart{animation:pulse 1.5s infinite}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
@media(max-width:600px){.glass-nav{padding:0.6rem 1rem}.wrap{padding:8px 12px}}
</style>
</head>
<body>
<canvas id="bg"></canvas><div class="mesh"></div>
<nav class="glass-nav">
<div class="brand"><div class="brand-icon">NX</div><span class="brand-text">NEXUS</span></div>
<div class="user-info">
<div class="user-badge"><i class="fas fa-user-astronaut"></i><span class="uname">{{ username }}</span>{% if expires_at %}<span class="expiry">⚡ {{ expires_at|timestamp_to_date }}</span>{% endif %}</div>
<a href="/logout" class="btn btn-danger btn-sm"><i class="fas fa-sign-out-alt"></i> LOGOUT</a>
</div>
</nav>
<div class="wrap">
<div class="grid">
<div class="card">
<div class="card-header"><i class="fas fa-server"></i> SERVER</div>
<div class="status-row"><div class="status-dot {% if running %}running{% else %}stopped{% endif %}"></div><span class="status-label {% if running %}running{% else %}stopped{% endif %}">{% if running %}● RUNNING{% else %}● STOPPED{% endif %}</span></div>
{% if running_file %}<div class="running-file">ACTIVE: <span>{{ running_file }}</span></div>{% endif %}
<div class="ctrl-group">
<select class="file-select" id="fileSelect">
<option value="">— SELECT —</option>
{% for f in files %}<option value="{{ f }}">{{ f }}</option>{% endfor %}
</select>
</div>
<div class="ctrl-group" style="margin-top:8px">
<button class="btn btn-success" onclick="startServer()"><i class="fas fa-play"></i> START</button>
<button class="btn btn-danger" onclick="stopServer()"><i class="fas fa-stop"></i> STOP</button>
<button class="btn btn-warning" onclick="restartServer()"><i class="fas fa-sync"></i> RESTART</button>
<button class="btn btn-outline btn-sm" onclick="deleteServer()"><i class="fas fa-trash"></i></button>
</div>
<div class="ctrl-group" style="margin-top:8px;align-items:center">
<button class="btn {% if auto_enabled %}btn-success{% else %}btn-outline{% endif %}" id="autoBtn" onclick="toggleAuto()">
<i class="fas fa-heartbeat"></i> AUTO-RESTART: {% if auto_enabled %}ON{% else %}OFF{% endif %}
</button>
<span id="autoStats" class="auto-badge {% if auto_enabled %}on{% endif %}">
{% if auto_enabled %}<i class="fas fa-heart heart"></i>{% endif %}♻ {{ auto_count }} · 💥 {{ auto_crashes }}
</span>
</div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-cloud-upload-alt"></i> FILES</div>
<div class="upload-zone" id="uploadZone"><i class="fas fa-cloud-upload-alt"></i><p>DRAG OR CLICK</p><input type="file" id="fileInput" multiple></div>
<div class="file-list" id="fileList">
{% for f in files %}
<div class="file-chip"><i class="fas fa-file-code"></i> {{ f }}<span class="actions"><a href="/file/view/{{ f }}" target="_blank"><i class="fas fa-eye"></i></a><a href="/download/{{ f }}" target="_blank"><i class="fas fa-download"></i></a><form method="POST" action="/file/delete/{{ f }}" style="display:inline" onsubmit="return confirm('DELETE?')"><button type="submit"><i class="fas fa-times"></i></button></form></span></div>
{% endfor %}
</div>
</div>
</div>
<div class="grid">
<div class="card">
<div class="card-header"><i class="fas fa-cubes"></i> INSTALL</div>
<div class="install-row"><input type="text" id="installCmd" placeholder="PIP INSTALL"><button class="btn btn-primary" onclick="installModule()"><i class="fas fa-download"></i></button></div>
<div class="terminal"><div class="term-bar"><span class="term-dot red"></span><span class="term-dot yellow"></span><span class="term-dot green"></span><span class="term-title">LOG</span></div><div class="term-body" id="installOutput">— READY —</div></div>
</div>
<div class="card">
<div class="card-header"><i class="fas fa-terminal"></i> LOGS <button class="btn btn-outline btn-sm" style="margin-left:auto;padding:2px 10px;font-size:8px" onclick="refreshLogs()"><i class="fas fa-sync"></i></button></div>
<div class="terminal" style="border:none;border-radius:0;margin-top:0"><div class="term-body" id="logOutput" style="max-height:180px">[SYSTEM] WAITING…</div></div>
</div>
</div>
</div>
<script>
const fileSelect=document.getElementById('fileSelect');
const logOutput=document.getElementById('logOutput');
const installOutput=document.getElementById('installOutput');
const uploadZone=document.getElementById('uploadZone');
const fileInput=document.getElementById('fileInput');

function showToast(msg,type='success'){const el=document.createElement('div');el.style.cssText=`position:fixed;top:70px;right:16px;z-index:9999;padding:10px 18px;border-radius:6px;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;backdrop-filter:blur(20px);border:2px solid ${type==='success'?'rgba(255,215,0,0.3)':'rgba(255,23,68,0.3)'};background:${type==='success'?'rgba(255,215,0,0.08)':'rgba(255,23,68,0.08)'};color:${type==='success'?'#FFD700':'#FF5252'};animation:slideIn 0.3s ease`;el.textContent=msg;document.body.appendChild(el);setTimeout(()=>el.remove(),3000)}
const style=document.createElement('style');style.textContent='@keyframes slideIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}';document.head.appendChild(style);

function getFile(){return fileSelect.value||prompt('FILENAME:')}
function startServer(){const f=getFile();if(!f)return;fetch('/server/start',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(f)}).then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),800)})}
function stopServer(){if(!confirm('STOP PROCESS?'))return;fetch('/server/stop',{method:'POST'}).then(()=>location.reload())}
function restartServer(){const f=getFile();if(!f)return;fetch('/server/restart',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'file='+encodeURIComponent(f)}).then(r=>r.json()).then(d=>{showToast(d.msg,d.ok);if(d.ok)setTimeout(()=>location.reload(),800)})}
function deleteServer(){if(!confirm('DELETE PROCESS?'))return;fetch('/server/delete',{method:'POST'}).then(()=>location.reload())}

function toggleAuto(){
  fetch('/server/auto/status').then(r=>r.json()).then(s=>{
    const next = s.enabled ? 0 : 1;
    fetch('/server/auto',{
      method:'POST',
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:'enabled='+next
    }).then(r=>r.json()).then(d=>{
      showToast(d.ok ? (d.enabled?'AUTO-RESTART ENABLED':'AUTO-RESTART DISABLED') : d.msg, d.ok);
      if(d.ok) setTimeout(()=>location.reload(), 600);
    });
  });
}

function refreshLogs(){fetch('/logs').then(r=>r.json()).then(d=>{
  logOutput.innerHTML=d.logs&&d.logs.length?d.logs.join('\\n'):'[SYSTEM] NO OUTPUT';
  if(d.install&&d.install.length)installOutput.innerHTML=d.install.join('\\n');
  if(d.auto){
    const el=document.getElementById('autoStats');
    if(el) el.innerHTML = (d.auto.enabled?'<i class="fas fa-heart heart"></i> ':'')+'♻ '+d.auto.count+' · 💥 '+d.auto.crash_count;
    el.className = 'auto-badge ' + (d.auto.enabled?'on':'');
  }
})}
function installModule(){const cmd=document.getElementById('installCmd').value.trim();if(!cmd)return;installOutput.textContent='INSTALLING…';fetch('/install',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'command='+encodeURIComponent(cmd)}).then(r=>r.json()).then(d=>{installOutput.textContent=d.msg;showToast(d.msg,d.ok)})}

uploadZone.addEventListener('click',()=>fileInput.click());
uploadZone.addEventListener('dragover',e=>{e.preventDefault();uploadZone.classList.add('drag-over')});
uploadZone.addEventListener('dragleave',()=>uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop',e=>{e.preventDefault();uploadZone.classList.remove('drag-over');if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files)});
fileInput.addEventListener('change',function(){if(this.files.length)uploadFiles(this.files);this.value=''});
function uploadFiles(files){const fd=new FormData();for(let f of files)fd.append('files',f);fetch('/upload',{method:'POST',body:fd}).then(()=>location.reload())}

refreshLogs();setInterval(refreshLogs,4000);

const c=document.getElementById('bg'),ctx=c.getContext('2d');
let W,H,p=[];
function resize(){W=c.width=innerWidth;H=c.height=innerHeight}
class P{constructor(){this.reset()}reset(){this.x=Math.random()*W;this.y=Math.random()*H;this.vx=(Math.random()-.5)*0.3;this.vy=(Math.random()-.5)*0.3;this.r=Math.random()*1.6+0.4;this.a=Math.random()*0.25+0.06;this.col='255,23,68'}update(){this.x+=this.vx;this.y+=this.vy;if(this.x<0||this.x>W||this.y<0||this.y>H)this.reset()}draw(){ctx.beginPath();ctx.arc(this.x,this.y,this.r,0,Math.PI*2);ctx.fillStyle=`rgba(${this.col},${this.a})`;ctx.fill()}}
function init(){p=[];for(let i=0;i<70;i++)p.push(new P())}
function loop(){ctx.clearRect(0,0,W,H);p.forEach(d=>{d.update();d.draw()});requestAnimationFrame(loop)}
window.addEventListener('resize',()=>{resize();init()});resize();init();loop();
</script>
</body>
</html>"""

# ============================================
#  MAIN
# ============================================
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🚀 NEXUS VPS PANEL - ULTRA PREMIUM EDITION")
    print("   + AUTO-RESTART CRASH RECOVERY ENGINE")
    print("="*70)
    print(f"📍 LOCAL:  http://127.0.0.1:5000")
    print(f"📍 NETWORK: http://0.0.0.0:5000")
    print(f"👤 OWNER:  {OWNER_USER} / {OWNER_PASS}")
    print(f"♻ AUTO-RESTART: ENABLED (MAX {MAX_AUTO_RESTARTS} per session)")
    print("="*70 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)