"""
LENZ - AI-assisted stampede prevention for existing CCTV cameras.

Run:   python3 app.py        (opens http://localhost:8000 automatically)

Camera sources (all chosen inside the web page):
  1. "Use this device's camera"  -> browser camera (browser asks permission)
  2. "Connect"                   -> IP / CCTV / phone stream (rtsp:// or http://)
                                    or a video file, read by the backend via OpenCV

Pipeline: Camera -> Frames -> AI detection -> Event analysis -> Alert -> Human verification
Privacy:  video is analysed in memory and never saved. Only event metadata is stored.
"""

import os

# Must be set BEFORE importing cv2: RTSP over TCP is far more reliable than UDP.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import csv
import io
import math
import re
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request
from ultralytics import YOLO

# ---------------------------------------------------------------- settings ---
PORT = 8000                                  # not 5000: macOS AirPlay uses it
MODEL_NAME = os.environ.get("LENZ_MODEL", "yolo11s.pt")   # s = accurate + fast enough
DEVICE = os.environ.get("LENZ_DEVICE", "cpu")             # try LENZ_DEVICE=mps for speed
CONF = 0.30                                  # detection confidence (lower = finds more people)
IMGSZ = 960                                  # inference size (higher = better for small people)
EVENT_COOLDOWN_S = 10                        # min seconds between logged alerts
PROCESS_INTERVAL_S = 0.1                     # stream mode: analyse at most ~10 frames/second
KEEP_HISTORY = False                         # False = every launch starts with a clean event log
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lenz_events.db")

# ------------------------------------------------------------------- model ---
print("[lenz] Loading AI model " + MODEL_NAME + " (first run downloads it)...")
model = YOLO(MODEL_NAME)
model_lock = threading.Lock()

app = Flask(__name__)
state_lock = threading.Lock()
state = {
    "threshold": 5,
    "camera_name": "Classroom Camera 1",
    "count": 0,
    "status": "OK",            # OK | WATCH | ALERT
    "peak": 0,
    "alert_active": False,
    "last_event_ts": 0.0,
    "last_eval_ts": 0.0,
    "fps": 0.0,
    "latency_ms": 0.0,
    "worker": None,
}


# ---------------------------------------------------------------- database ---
def db_run(sql, args=(), fetch=False):
    """Tiny SQLite helper. Returns list of dicts if fetch, else lastrowid."""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        cur = c.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()] if fetch else None
        c.commit()
        return rows if fetch else cur.lastrowid


def init_db():
    if not KEEP_HISTORY:
        db_run("DROP TABLE IF EXISTS events")
    db_run("""CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT    NOT NULL,
                camera    TEXT    NOT NULL,
                type      TEXT    NOT NULL,
                people    INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                review    TEXT    NOT NULL DEFAULT 'pending'
              )""")


# ------------------------------------------------------------- core logic ---
def infer(frame):
    """Detect people. Returns ([[x1,y1,x2,y2], ...], milliseconds)."""
    global DEVICE
    t0 = time.time()
    with model_lock:
        try:
            res = model.predict(frame, classes=[0], conf=CONF, imgsz=IMGSZ,
                                device=DEVICE, verbose=False)[0]
        except Exception as e:
            if DEVICE == "cpu":
                raise
            print("[lenz] device '" + str(DEVICE) + "' failed (" + str(e) + "), falling back to CPU")
            DEVICE = "cpu"
            res = model.predict(frame, classes=[0], conf=CONF, imgsz=IMGSZ,
                                device="cpu", verbose=False)[0]
    boxes = res.boxes.xyxy.cpu().numpy().astype(int).tolist()
    return boxes, (time.time() - t0) * 1000.0


def level_for(count, threshold):
    if count >= threshold:
        return "ALERT"
    if count >= max(1, math.ceil(threshold * 0.6)):
        return "WATCH"
    return "OK"


def evaluate(count, ms=None):
    """Update live state; log an event to the database when an alert begins."""
    with state_lock:
        now = time.time()
        thr = state["threshold"]
        status = level_for(count, thr)
        if (status == "ALERT" and not state["alert_active"]
                and now - state["last_event_ts"] >= EVENT_COOLDOWN_S):
            db_run("INSERT INTO events (ts, camera, type, people, threshold) VALUES (?,?,?,?,?)",
                   (time.strftime("%Y-%m-%d %H:%M:%S"), state["camera_name"],
                    "Stampede risk", count, thr))
            state["last_event_ts"] = now
        state["alert_active"] = status == "ALERT"
        state["count"], state["status"] = count, status
        state["peak"] = max(state["peak"], count)
        if state["last_eval_ts"]:
            dt = now - state["last_eval_ts"]
            if dt > 0:
                inst = 1.0 / dt
                state["fps"] = inst if not state["fps"] else 0.8 * state["fps"] + 0.2 * inst
        state["last_eval_ts"] = now
        if ms is not None:
            state["latency_ms"] = ms if not state["latency_ms"] else 0.8 * state["latency_ms"] + 0.2 * ms
        return status


def mask(source):
    """Hide the password in rtsp://user:pass@host URLs."""
    return re.sub(r"//([^:/@]+):[^@]+@", r"//\1:***@", source)


def shrink(frame, max_w=1280):
    h, w = frame.shape[:2]
    if w > max_w:
        frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
    return frame


def reset_stats():
    with state_lock:
        state.update(count=0, status="OK", alert_active=False,
                     fps=0.0, latency_ms=0.0, last_eval_ts=0.0)


def stop_worker():
    with state_lock:
        w, state["worker"] = state["worker"], None
    if w:
        w.stop()


# ---------------------------------------------- stream mode (RTSP/HTTP/file) ---
BGR = {"OK": (94, 197, 34), "WATCH": (11, 158, 245), "ALERT": (68, 68, 239)}


class StreamWorker(threading.Thread):
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = int(source) if source.isdigit() else source
        self.label = mask(source)
        self.is_file = isinstance(self.source, str) and os.path.isfile(self.source)
        self.running = True
        self.status = "connecting..."
        self.jpeg = None
        self.seq = 0

    def stop(self):
        self.running = False

    def _open(self):
        if isinstance(self.source, int):
            return cv2.VideoCapture(self.source)
        try:
            return cv2.VideoCapture(
                self.source, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000,
                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])
        except Exception:
            return cv2.VideoCapture(self.source)

    def run(self):
        cap = None
        fails = 0
        last_proc = 0.0
        delay = 0.0

        while self.running:
            if cap is None:
                cap = self._open()
                if not self.running:
                    break
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.status = "cannot open source - retrying..."
                    time.sleep(2)
                    continue
                self.status = "live"
                fails = 0
                if self.is_file:
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    delay = 1.0 / fps if fps and fps > 0 else 0.033

            if not cap.grab():
                if self.is_file:                       # loop the demo video
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    time.sleep(0.05)
                    continue
                fails += 1
                if fails >= 5:
                    cap.release()
                    cap = None
                    self.status = "stream lost - reconnecting..."
                    time.sleep(2)
                continue
            fails = 0

            if delay:                                  # play files at real speed
                time.sleep(delay)
            now = time.time()
            if now - last_proc < PROCESS_INTERVAL_S:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            last_proc = now
            try:
                self._process(shrink(frame))
            except Exception as e:
                self.status = "error: " + str(e)
                time.sleep(1)

        if cap is not None:
            cap.release()

    def _process(self, frame):
        boxes, ms = infer(frame)
        if state["worker"] is not self:                # stopped / replaced meanwhile
            return
        status = evaluate(len(boxes), ms)
        color = BGR[status]
        for x1, y1, x2, y2 in boxes:
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            self.jpeg = buf.tobytes()
            self.seq += 1


# ------------------------------------------------------------------ routes ---
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.post("/detect")
def detect():
    """Browser-camera mode: the page posts a JPEG frame, we return boxes."""
    frame = cv2.imdecode(np.frombuffer(request.get_data(), np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify(error="bad image"), 400
    boxes, ms = infer(frame)
    status = evaluate(len(boxes), ms)
    h, w = frame.shape[:2]
    return jsonify(count=len(boxes), status=status, w=w, h=h, boxes=boxes)


@app.post("/connect")
def connect():
    src = ((request.get_json(silent=True) or {}).get("source") or "").strip()
    if not src:
        return jsonify(ok=False, error="Enter a stream URL or a video file path."), 400
    if not (src.isdigit() or re.match(r"^(rtsps?|https?)://", src, re.I) or os.path.isfile(src)):
        return jsonify(ok=False, error="Not an rtsp:// or http:// URL, and no such file: " + src), 400
    stop_worker()
    reset_stats()
    w = StreamWorker(src)
    with state_lock:
        state["worker"] = w
    w.start()
    return jsonify(ok=True)


@app.post("/reset")
def reset():
    stop_worker()
    reset_stats()
    return jsonify(ok=True)


@app.post("/config")
def config():
    d = request.get_json(silent=True) or {}
    with state_lock:
        try:
            if "threshold" in d:
                state["threshold"] = max(1, min(int(d["threshold"]), 1000))
        except (TypeError, ValueError):
            pass
        name = str(d.get("camera_name", "")).strip()[:40]
        if name:
            state["camera_name"] = name
    return jsonify(ok=True)


@app.post("/review")
def review():
    d = request.get_json(silent=True) or {}
    try:
        eid = int(d.get("id"))
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    verdict = d.get("review")
    if verdict not in ("confirmed", "false_alarm", "pending"):
        return jsonify(ok=False), 400
    db_run("UPDATE events SET review = ? WHERE id = ?", (verdict, eid))
    return jsonify(ok=True)


@app.post("/clear")
def clear():
    db_run("DELETE FROM events")
    with state_lock:
        state["peak"] = 0
        state["last_event_ts"] = 0.0
    return jsonify(ok=True)


@app.get("/events.csv")
def events_csv():
    keys = ["id", "ts", "camera", "type", "people", "threshold", "review"]
    rows = db_run("SELECT id, ts, camera, type, people, threshold, review "
                  "FROM events ORDER BY id DESC", fetch=True)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(keys)
    for r in rows:
        w.writerow([r[k] for k in keys])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=lenz_events.csv"})


@app.get("/stats")
def stats():
    events = db_run("SELECT id, ts, camera, type, people, review FROM events "
                    "ORDER BY id DESC LIMIT 15", fetch=True)
    counts = {r["review"]: r["n"] for r in
              db_run("SELECT review, COUNT(*) AS n FROM events GROUP BY review", fetch=True)}
    with state_lock:
        w = state["worker"]
        live = bool(state["last_eval_ts"]) and (time.time() - state["last_eval_ts"] < 2.5)
        return jsonify(
            count=state["count"], status=state["status"], threshold=state["threshold"],
            camera_name=state["camera_name"], peak=state["peak"], live=live,
            fps=state["fps"], latency_ms=state["latency_ms"],
            source_status=(w.status + " - " + w.label) if w else None,
            total=sum(counts.values()), pending=counts.get("pending", 0),
            confirmed=counts.get("confirmed", 0), false_alarms=counts.get("false_alarm", 0),
            events=events,
        )


@app.get("/stream.mjpg")
def stream():
    w = state["worker"]
    if w is None:
        return Response("no stream", status=404)

    def gen():
        last = -1
        while w.running and state["worker"] is w:
            if w.jpeg is not None and w.seq != last:
                last = w.seq
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + w.jpeg + b"\r\n"
            time.sleep(0.03)

    resp = Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- web page ---
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lenz - Stampede Prevention</title>
<style>
  :root { --bg:#0a0e17; --panel:#121927; --panel2:#18213a; --line:#202b44; --text:#eaf0fb; --muted:#8793ab;
          --ok:#22c55e; --watch:#f59e0b; --bad:#ef4444; --accent:#5b8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:radial-gradient(1200px 600px at 20% -10%, #14203a 0%, var(--bg) 60%) fixed;
         color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:1280px; margin:0 auto; padding:0 24px; }
  header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 0 10px; flex-wrap:wrap; }
  .brand { display:flex; align-items:baseline; gap:12px; }
  .logo { font-size:26px; font-weight:800; letter-spacing:.14em; }
  .logo b { color:var(--accent); }
  .tag { color:var(--muted); font-size:14px; }
  .right { display:flex; align-items:center; gap:14px; color:var(--muted); font-size:13px; }
  .live { display:flex; align-items:center; gap:7px; font-weight:700; letter-spacing:.06em; }
  .live i { width:9px; height:9px; border-radius:50%; background:#4b566e; display:inline-block; }
  .live.on { color:var(--ok); } .live.on i { background:var(--ok); box-shadow:0 0 10px var(--ok); animation:blink 1.4s infinite; }
  @keyframes blink { 50% { opacity:.35; } }

  .pipe { display:flex; align-items:stretch; gap:6px; margin:8px 0 16px; overflow-x:auto; padding-bottom:4px; }
  .node { flex:1; min-width:120px; background:var(--panel); border:1px solid var(--line); border-radius:12px;
          padding:10px 12px; transition:all .25s; }
  .node b { display:block; font-size:12px; letter-spacing:.05em; }
  .node span { display:block; color:var(--muted); font-size:11px; margin-top:2px; }
  .node.on { border-color:#2c4a86; background:#14213d; } .node.on b { color:#9dbcff; }
  .node.alert { border-color:var(--bad); background:#2a1218; animation:pulse 1s infinite; } .node.alert b { color:#ff8a8a; }
  .node.warn { border-color:var(--watch); background:#2a2010; } .node.warn b { color:#ffc766; }
  .arrow { align-self:center; color:#3a4766; font-size:16px; }

  .kpis { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }
  .kpi { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px 16px; }
  .kpi .n { font-size:28px; font-weight:800; line-height:1.1; } .kpi .l { color:var(--muted); font-size:12px; margin-top:2px; }

  main { display:grid; grid-template-columns:1.7fr 1fr; gap:16px; padding-bottom:16px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; }
  h2 { margin:0 0 12px; font-size:12px; color:var(--muted); font-weight:700; letter-spacing:.1em; text-transform:uppercase; }
  .col { display:flex; flex-direction:column; gap:16px; min-width:0; }

  .stage { position:relative; background:#000; border-radius:12px; overflow:hidden; border:2px solid transparent; transition:border-color .2s; }
  .stage.alert { border-color:var(--bad); animation:pulse 1s infinite; }
  @keyframes pulse { 50% { box-shadow:0 0 28px rgba(239,68,68,.55); } }
  .stage video, .stage img { width:100%; display:block; }
  #overlay { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
  .placeholder { aspect-ratio:16/9; display:flex; flex-direction:column; align-items:center; justify-content:center;
                 gap:6px; color:var(--muted); text-align:center; padding:20px; }
  .placeholder big { font-size:18px; color:var(--text); font-weight:600; }
  .hud { position:absolute; left:12px; top:12px; display:none; gap:8px; align-items:center; }
  .hud .pill { background:rgba(10,14,23,.75); backdrop-filter:blur(6px); border-radius:999px; padding:6px 12px; font-weight:700; font-size:13px; }

  .row { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; align-items:center; }
  button { background:var(--accent); color:#fff; border:0; border-radius:9px; padding:10px 14px; font-size:14px; font-weight:600; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  button.secondary { background:#26314d; }
  button.mini { padding:5px 9px; font-size:12px; background:#26314d; margin-right:4px; }
  button.mini.ok { background:#1b4d33; }
  input { background:#0a0e17; color:var(--text); border:1px solid var(--line); border-radius:9px; padding:10px 12px; font-size:14px; }
  #src { flex:1; min-width:220px; } #thr { width:74px; } #camname { width:190px; }
  label { color:var(--muted); font-size:13px; }

  .badge { display:inline-block; padding:7px 14px; border-radius:999px; font-weight:800; font-size:13px; letter-spacing:.05em; }
  .badge.OK { background:rgba(34,197,94,.15); color:var(--ok); }
  .badge.WATCH { background:rgba(245,158,11,.16); color:var(--watch); }
  .badge.ALERT { background:rgba(239,68,68,.2); color:#ff7b7b; }
  .big { font-size:64px; font-weight:800; line-height:1; margin:14px 0 2px; }
  .gauge { height:10px; background:#0a0e17; border-radius:999px; overflow:hidden; margin-top:12px; border:1px solid var(--line); }
  .gauge div { height:100%; width:0; background:var(--ok); transition:width .3s, background .3s; }
  #chart { width:100%; height:90px; display:block; }
  .muted { color:var(--muted); font-size:13px; }

  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--muted); font-weight:600; padding:6px 4px; font-size:12px; }
  td { padding:8px 4px; border-top:1px solid var(--line); vertical-align:middle; }
  .chip { padding:3px 9px; border-radius:999px; font-size:12px; font-weight:700; }
  .chip.confirmed { background:rgba(34,197,94,.15); color:var(--ok); }
  .chip.false_alarm { background:rgba(139,151,171,.18); color:#aab4c8; }
  .links { display:flex; gap:14px; margin-top:10px; font-size:13px; }
  .links a, .links span { color:var(--accent); cursor:pointer; text-decoration:none; }
  footer { color:var(--muted); font-size:12px; padding:4px 0 28px; }
  @media (max-width:960px) { main { grid-template-columns:1fr; } .kpis { grid-template-columns:repeat(2,1fr); } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand"><div class="logo">LEN<b>Z</b></div><div class="tag">Turning every camera into an intelligent guardian</div></div>
    <div class="right"><span id="perf"></span><div id="live" class="live"><i></i><span id="livetxt">OFFLINE</span></div></div>
  </header>

  <div class="pipe">
    <div class="node" id="p1"><b>1 &middot; CAMERA</b><span>CCTV / IP stream</span></div><div class="arrow">&rsaquo;</div>
    <div class="node" id="p2"><b>2 &middot; FRAMES</b><span>Extract &amp; preprocess</span></div><div class="arrow">&rsaquo;</div>
    <div class="node" id="p3"><b>3 &middot; AI DETECTION</b><span>People detection</span></div><div class="arrow">&rsaquo;</div>
    <div class="node" id="p4"><b>4 &middot; EVENT ANALYSIS</b><span>Density vs threshold</span></div><div class="arrow">&rsaquo;</div>
    <div class="node" id="p5"><b>5 &middot; ALERT</b><span>Notify staff</span></div><div class="arrow">&rsaquo;</div>
    <div class="node" id="p6"><b>6 &middot; HUMAN VERIFY</b><span>Confirm or dismiss</span></div>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="n" id="k_total">0</div><div class="l">Alerts raised</div></div>
    <div class="kpi"><div class="n" id="k_conf" style="color:var(--ok)">0</div><div class="l">Confirmed by staff</div></div>
    <div class="kpi"><div class="n" id="k_false">0</div><div class="l">False alarms</div></div>
    <div class="kpi"><div class="n" id="k_peak">0</div><div class="l">Peak people in view</div></div>
  </div>

  <main>
    <div class="col">
      <section class="panel">
        <h2>Live camera</h2>
        <div class="stage" id="stage">
          <div id="placeholder" class="placeholder"><big>No camera connected</big>
            <span>Use this device's camera, or connect an IP / CCTV stream below</span></div>
          <video id="video" autoplay muted playsinline style="display:none"></video>
          <img id="stream" alt="live stream" style="display:none">
          <canvas id="overlay"></canvas>
          <div class="hud" id="hud"><span class="pill"><span id="hudbadge" class="badge OK" style="padding:2px 8px">NORMAL</span></span><span class="pill" id="hudcount">0 people</span></div>
        </div>
        <div class="row">
          <button id="btnCam">Use this device's camera</button>
          <button id="btnStop" class="secondary">Stop</button>
        </div>
        <div class="row">
          <input id="src" placeholder="Stream URL (rtsp:// or http://...) or video file e.g. demo.mp4">
          <button id="btnConnect">Connect</button>
        </div>
        <div class="row">
          <label>Camera name</label><input id="camname" value="Classroom Camera 1">
          <label>Alert at</label><input id="thr" type="number" min="1" value="5"><label>people</label>
          <button id="btnSound" class="secondary">Sound: On</button>
        </div>
        <p class="muted" id="srcstatus" style="margin:10px 0 0"></p>
      </section>
    </div>

    <div class="col">
      <section class="panel">
        <h2>Crowd status</h2>
        <span id="badge" class="badge OK">NORMAL</span>
        <div class="big" id="count">0</div>
        <div class="muted" id="countlabel">people in view</div>
        <div class="gauge"><div id="gaugefill"></div></div>
      </section>
      <section class="panel">
        <h2>People in view - last 45 seconds</h2>
        <canvas id="chart"></canvas>
      </section>
      <section class="panel">
        <h2>Events - human verification</h2>
        <table>
          <thead><tr><th>Time</th><th>Camera</th><th>People</th><th>Review</th></tr></thead>
          <tbody id="events"></tbody>
        </table>
        <div class="links"><a href="/events.csv">Export CSV</a><span id="btnClear">Clear events</span></div>
      </section>
    </div>
  </main>
  <footer>Privacy by design: video is analysed in memory and never saved. Only event metadata (time, camera, people count) is stored. AI assists - a human always verifies.</footer>
</div>

<script>
const $ = id => document.getElementById(id);
const video = $('video'), img = $('stream'), overlay = $('overlay'), stage = $('stage');
const COLORS = { OK: '#22c55e', WATCH: '#f59e0b', ALERT: '#ef4444' };
const LABELS = { OK: 'NORMAL', WATCH: 'WATCH', ALERT: 'STAMPEDE RISK' };
let runToken = 0, mediaStream = null, cfgInit = false, audioCtx = null, soundOn = true;
let prevStatus = 'OK', lastSig = '';
const hist = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));

/* ---------- sound ---------- */
function ensureAudio() {
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) {}
}
function beep() {
  if (!soundOn || !audioCtx) return;
  try {
    [0, 0.25].forEach(delay => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = 'sine'; o.frequency.value = 880; g.gain.value = 0.08;
      o.connect(g); g.connect(audioCtx.destination);
      o.start(audioCtx.currentTime + delay); o.stop(audioCtx.currentTime + delay + 0.18);
    });
  } catch (e) {}
}

/* ---------- camera sources ---------- */
function clearOverlay() { overlay.width = 1; overlay.height = 1; }

async function stopAll() {
  runToken++;
  if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
  video.srcObject = null; video.style.display = 'none';
  img.removeAttribute('src'); img.style.display = 'none';
  clearOverlay();
  $('placeholder').style.display = 'flex';
  try { await fetch('/reset', { method: 'POST' }); } catch (e) {}
}

async function startCam() {
  await stopAll();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    alert('This browser cannot access the camera. Open http://localhost:8000 in Chrome or Safari.');
    return;
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
  } catch (e) {
    alert('Camera blocked (' + e.name + '). Click the camera icon in the address bar, allow it, then try again.');
    return;
  }
  video.srcObject = mediaStream;
  try { await video.play(); } catch (e) {}
  video.style.display = 'block';
  $('placeholder').style.display = 'none';
  detectLoop(++runToken);
}

async function detectLoop(token) {
  const c = document.createElement('canvas');
  while (token === runToken) {
    if (video.videoWidth) {
      const scale = Math.min(1, 960 / video.videoWidth);
      c.width = Math.round(video.videoWidth * scale);
      c.height = Math.round(video.videoHeight * scale);
      c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
      const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.85));
      try {
        const res = await fetch('/detect', { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob });
        const d = await res.json();
        if (token === runToken && d.boxes) draw(d);
      } catch (e) { await sleep(500); }
    }
    await sleep(30);
  }
}

function draw(d) {
  overlay.width = d.w; overlay.height = d.h;
  const ctx = overlay.getContext('2d');
  ctx.clearRect(0, 0, d.w, d.h);
  ctx.lineWidth = Math.max(2, d.w / 320);
  ctx.strokeStyle = COLORS[d.status] || COLORS.OK;
  d.boxes.forEach(b => ctx.strokeRect(b[0], b[1], b[2] - b[0], b[3] - b[1]));
}

async function startStream() {
  const src = $('src').value.trim();
  if (!src) { alert('Enter a stream URL or a video file name first.'); return; }
  await stopAll();
  const r = await fetch('/connect', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                      body: JSON.stringify({ source: src }) });
  const d = await r.json();
  if (!d.ok) { alert(d.error); return; }
  img.src = '/stream.mjpg?t=' + Date.now();
  img.style.display = 'block';
  $('placeholder').style.display = 'none';
}

/* ---------- events table ---------- */
function mk(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
async function review(id, verdict) {
  await fetch('/review', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                           body: JSON.stringify({ id: id, review: verdict }) });
  lastSig = '';
}
function renderEvents(ev) {
  const sig = JSON.stringify(ev);
  if (sig === lastSig) return;          /* only rebuild when data changed, so buttons stay clickable */
  lastSig = sig;
  const tb = $('events');
  tb.innerHTML = '';
  if (!ev.length) {
    const tr = mk('tr'), td = mk('td', 'muted', 'No events yet');
    td.colSpan = 4; tr.appendChild(td); tb.appendChild(tr);
    return;
  }
  ev.forEach(e => {
    const tr = mk('tr');
    tr.appendChild(mk('td', '', e.ts.split(' ')[1]));
    tr.appendChild(mk('td', '', e.camera));
    tr.appendChild(mk('td', '', String(e.people)));
    const td = mk('td');
    if (e.review === 'pending') {
      const a = mk('button', 'mini ok', 'Confirm'); a.onclick = () => review(e.id, 'confirmed');
      const f = mk('button', 'mini', 'False alarm'); f.onclick = () => review(e.id, 'false_alarm');
      td.appendChild(a); td.appendChild(f);
    } else {
      td.appendChild(mk('span', 'chip ' + e.review, e.review === 'confirmed' ? 'Confirmed' : 'False alarm'));
    }
    tr.appendChild(td);
    tb.appendChild(tr);
  });
}

/* ---------- chart ---------- */
function drawChart(thr) {
  const c = $('chart'), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const max = Math.max(thr + 1, 3, Math.max.apply(null, hist.concat([0])) + 1);
  const y = v => h - 4 - (v / max) * (h - 12);
  ctx.setLineDash([5, 4]); ctx.strokeStyle = '#ef4444'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, y(thr)); ctx.lineTo(w, y(thr)); ctx.stroke();
  ctx.setLineDash([]);
  if (hist.length < 2) return;
  ctx.strokeStyle = '#5b8cff'; ctx.lineWidth = 2; ctx.beginPath();
  hist.forEach((v, i) => {
    const x = (i / 89) * w;
    if (i === 0) ctx.moveTo(x, y(v)); else ctx.lineTo(x, y(v));
  });
  ctx.stroke();
}

/* ---------- polling ---------- */
async function poll() {
  try {
    const s = await (await fetch('/stats')).json();
    if (!cfgInit) { $('thr').value = s.threshold; $('camname').value = s.camera_name; cfgInit = true; }
    const lvl = s.status, live = s.live;
    const word = s.count === 1 ? 'person' : 'people';

    $('count').textContent = s.count;
    $('countlabel').textContent = word + ' in view';
    $('badge').textContent = LABELS[lvl]; $('badge').className = 'badge ' + lvl;
    $('gaugefill').style.width = Math.min(100, Math.round(100 * s.count / s.threshold)) + '%';
    $('gaugefill').style.background = COLORS[lvl];
    stage.classList.toggle('alert', lvl === 'ALERT' && live);

    $('hud').style.display = live ? 'flex' : 'none';
    $('hudbadge').textContent = LABELS[lvl]; $('hudbadge').className = 'badge ' + lvl;
    $('hudcount').textContent = s.count + ' ' + word;

    $('live').className = 'live' + (live ? ' on' : '');
    $('livetxt').textContent = live ? 'LIVE' : 'OFFLINE';
    $('perf').textContent = live ? ('AI ' + Math.round(s.latency_ms) + ' ms \\u00b7 ' + s.fps.toFixed(1) + ' FPS') : '';

    ['p1', 'p2', 'p3', 'p4'].forEach(id => { $(id).className = 'node' + (live ? ' on' : ''); });
    $('p5').className = 'node' + (live && lvl === 'ALERT' ? ' alert' : '');
    $('p6').className = 'node' + (s.pending > 0 ? ' warn' : '');

    $('k_total').textContent = s.total; $('k_conf').textContent = s.confirmed;
    $('k_false').textContent = s.false_alarms; $('k_peak').textContent = s.peak;

    if (lvl === 'ALERT' && prevStatus !== 'ALERT' && live) beep();
    prevStatus = lvl;

    hist.push(live ? s.count : 0);
    if (hist.length > 90) hist.shift();
    drawChart(s.threshold);

    $('srcstatus').textContent = s.source_status ? ('Stream: ' + s.source_status) : '';
    renderEvents(s.events);
  } catch (e) {}
  setTimeout(poll, 500);
}

/* ---------- wiring ---------- */
function pushConfig() {
  fetch('/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                     body: JSON.stringify({ threshold: parseInt($('thr').value, 10), camera_name: $('camname').value }) });
}
$('btnCam').onclick = () => { ensureAudio(); startCam(); };
$('btnStop').onclick = stopAll;
$('btnConnect').onclick = () => { ensureAudio(); startStream(); };
$('btnSound').onclick = () => {
  ensureAudio(); soundOn = !soundOn;
  $('btnSound').textContent = 'Sound: ' + (soundOn ? 'On' : 'Off');
  if (soundOn) beep();
};
$('btnClear').onclick = async () => {
  if (!confirm('Clear all logged events?')) return;
  await fetch('/clear', { method: 'POST' }); lastSig = '';
};
$('thr').onchange = pushConfig;
$('camname').onchange = pushConfig;
window.addEventListener('beforeunload', () => { navigator.sendBeacon && navigator.sendBeacon('/reset'); });
pushConfig();
poll();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    init_db()
    print("[lenz] Warming up the AI model...")
    try:
        infer(np.zeros((540, 960, 3), np.uint8))
    except Exception as e:
        print("[lenz] warm-up skipped: " + str(e))
    url = "http://localhost:" + str(PORT)
    print("[lenz] Ready -> " + url + "   (Ctrl+C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
