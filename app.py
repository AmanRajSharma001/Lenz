"""LENZ chair occupancy monitor.

Run: python3 app.py
Open: http://localhost:8000

Uses a local YOLO11 COCO model. Video frames are processed in memory and are not saved.
Chair occupancy is an estimate based on the detected chair's seat region and nearby person boxes.
"""
import os
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import re
import threading
import time
import webbrowser
from contextlib import suppress

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request
from ultralytics import YOLO

PORT = int(os.environ.get("LENZ_PORT", "8000"))
MODEL_NAME = os.environ.get("LENZ_MODEL", "yolo11m.pt")
# Apple Silicon can use PyTorch MPS; infer() falls back to CPU if unavailable.
DEVICE = os.environ.get("LENZ_DEVICE", "mps" if __import__("platform").system() == "Darwin" and __import__("platform").machine() == "arm64" else "cpu")
IMGSZ = int(os.environ.get("LENZ_IMGSZ", "960"))
PERSON_CONF = float(os.environ.get("LENZ_PERSON_CONF", "0.22"))
CHAIR_CONF = float(os.environ.get("LENZ_CHAIR_CONF", "0.16"))
PROCESS_INTERVAL = 0.22

# COCO class IDs: person=0, chair=56
print(f"[LENZ] Loading {MODEL_NAME}. First run may download model weights...")
model = YOLO(MODEL_NAME)
model_lock = threading.Lock()
app = Flask(__name__)
state_lock = threading.RLock()
state = {
    "worker": None,
    "count": 0,
    "occupied": 0,
    "empty": 0,
    "chairs": [],
    "people": 0,
    "latency_ms": 0.0,
    "fps": 0.0,
    "last_frame": 0.0,
    "source_status": "No camera connected",
    "source_label": "",
    "peak_chairs": 0,
    "threshold": 0.30,
}


def infer(frame):
    """Return chair boxes and person boxes with confidence scores."""
    global DEVICE
    started = time.perf_counter()
    with model_lock:
        try:
            result = model.predict(
                frame, classes=[0, 56], conf=min(PERSON_CONF, CHAIR_CONF),
                imgsz=IMGSZ, device=DEVICE, verbose=False, iou=0.50, max_det=500
            )[0]
        except Exception as exc:
            if DEVICE == "cpu":
                raise
            print(f"[LENZ] Device {DEVICE!r} failed: {exc}; retrying on CPU")
            DEVICE = "cpu"
            result = model.predict(
                frame, classes=[0, 56], conf=min(PERSON_CONF, CHAIR_CONF),
                imgsz=IMGSZ, device="cpu", verbose=False, iou=0.50, max_det=500
            )[0]

    chairs, people = [], []
    boxes = result.boxes
    if boxes is not None:
        xyxy = boxes.xyxy.cpu().numpy().astype(int)
        cls_ids = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()
        for box, cls_id, conf in zip(xyxy, cls_ids, confs):
            item = {"box": box.tolist(), "confidence": float(conf)}
            if cls_id == 0 and conf >= PERSON_CONF:
                people.append(item)
            elif cls_id == 56 and conf >= CHAIR_CONF:
                chairs.append(item)
    return chairs, people, (time.perf_counter() - started) * 1000


def seat_region(box):
    """Estimate the seat area within a whole-chair box; not the chair's legs."""
    x1, y1, x2, y2 = map(float, box)
    w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)
    # Seat area is usually below the backrest and above the legs.
    return [x1 + 0.16 * w, y1 + 0.30 * h, x2 - 0.12 * w, y1 + 0.68 * h]


def chair_occupancy(chair_box, people):
    """Heuristic seat occupancy estimate using person torso/hip position.

    COCO has no dedicated seated-person class, so this combines overlap with
    the chair seat zone and the person's lower-middle region. It is more robust
    than requiring the full person box to overlap the seat.
    """
    sx1, sy1, sx2, sy2 = seat_region(chair_box)
    seat_w = max(1.0, sx2 - sx1)
    seat_h = max(1.0, sy2 - sy1)
    seat_area = seat_w * seat_h
    for person in people:
        px1, py1, px2, py2 = map(float, person["box"])
        pw, ph = max(1.0, px2 - px1), max(1.0, py2 - py1)
        # Approximate torso/hip zone; ignore most of the head and legs.
        hx1, hx2 = px1 + 0.12 * pw, px2 - 0.12 * pw
        hy1, hy2 = py1 + 0.35 * ph, py1 + 0.78 * ph
        ix1, iy1 = max(sx1, hx1), max(sy1, hy1)
        ix2, iy2 = min(sx2, hx2), min(sy2, hy2)
        overlap = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1) / seat_area
        person_cx = (px1 + px2) / 2
        seat_cx = (sx1 + sx2) / 2
        # Accept strong torso/seat overlap or a person centred over the seat
        # with vertical overlap. This remains an estimate, not pose recognition.
        centered = abs(person_cx - seat_cx) <= 0.42 * seat_w
        vertical_overlap = min(sy2, hy2) > max(sy1, hy1)
        if overlap >= 0.08 or (centered and vertical_overlap):
            return True
    return False


def analyse(frame):
    chairs, people, latency = infer(frame)
    results = []
    for chair in chairs:
        box = chair["box"]
        occupied = chair_occupancy(box, people)
        results.append({
            "box": box,
            "seat": [int(v) for v in seat_region(box)],
            "status": "OCCUPIED" if occupied else "EMPTY",
            "confidence": round(chair["confidence"], 3),
        })
    occupied_count = sum(c["status"] == "OCCUPIED" for c in results)
    return results, len(people), occupied_count, latency


def mask_source(source):
    return re.sub(r"//([^:/@]+):[^@]+@", r"//\1:***@", source)


def shrink(frame, max_width=1600):
    h, w = frame.shape[:2]
    if w > max_width:
        frame = cv2.resize(frame, (max_width, int(h * max_width / w)))
    return frame


def reset_stats():
    with state_lock:
        state.update(count=0, occupied=0, empty=0, chairs=[], people=0,
                     latency_ms=0.0, fps=0.0, last_frame=0.0,
                     source_status="No camera connected", source_label="", peak_chairs=0)


class StreamWorker(threading.Thread):
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source_text = source
        self.source = int(source) if source.isdigit() else source
        self.label = mask_source(source)
        self.is_file = isinstance(self.source, str) and os.path.isfile(self.source)
        self.running = True
        self.status = "Connecting"
        self.jpeg = None
        self.seq = 0

    def stop(self):
        self.running = False

    def _open(self):
        if isinstance(self.source, int):
            return cv2.VideoCapture(self.source)
        with suppress(Exception):
            return cv2.VideoCapture(self.source, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])
        return cv2.VideoCapture(self.source)

    def run(self):
        cap = None
        failures = 0
        last_process = 0.0
        file_delay = 0.0
        while self.running:
            if cap is None:
                cap = self._open()
                if not self.running:
                    break
                if not cap.isOpened():
                    cap.release(); cap = None
                    self.status = "Cannot open source; retrying"
                    with state_lock:
                        if state["worker"] is self: state["source_status"] = self.status
                    time.sleep(2)
                    continue
                self.status = "Live"
                failures = 0
                if self.is_file:
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    file_delay = 1 / fps if fps and fps > 0 else 0.033

            if not cap.grab():
                if self.is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    time.sleep(0.05)
                    continue
                failures += 1
                if failures >= 5:
                    cap.release(); cap = None
                    self.status = "Stream lost; reconnecting"
                    time.sleep(1)
                continue
            failures = 0
            if file_delay:
                time.sleep(file_delay)
            now = time.time()
            if now - last_process < PROCESS_INTERVAL:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            last_process = now
            try:
                self.process(shrink(frame))
            except Exception as exc:
                self.status = "Processing error"
                print("[LENZ] Frame error:", exc)
                time.sleep(0.2)
        if cap is not None:
            cap.release()

    def process(self, frame):
        chairs, people_count, occupied_count, latency = analyse(frame)
        with state_lock:
            if state["worker"] is not self or not self.running:
                return
        draw_overlay(frame, chairs)
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if ok:
            self.jpeg = buffer.tobytes()
            self.seq += 1
        now = time.time()
        with state_lock:
            if state["worker"] is not self:
                return
            previous = state["last_frame"]
            state.update(count=len(chairs), occupied=occupied_count,
                         empty=len(chairs) - occupied_count, chairs=chairs,
                         people=people_count, latency_ms=round(latency, 1),
                         fps=round(1 / (now - previous), 1) if previous and now > previous else 0.0,
                         last_frame=now, source_status=self.status,
                         source_label=self.label,
                         peak_chairs=max(state["peak_chairs"], len(chairs)))


def draw_overlay(frame, chairs):
    for chair in chairs:
        x1, y1, x2, y2 = chair["box"]
        occupied = chair["status"] == "OCCUPIED"
        color = (50, 70, 225) if occupied else (70, 190, 105)
        label = chair["status"] + f" {chair['confidence']:.0%}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        tx1, ty1, tx2, ty2 = chair["seat"]
        cv2.rectangle(frame, (tx1, ty1), (tx2, ty2), color, 1)
        text_y = max(22, y1 - 8)
        cv2.putText(frame, label, (x1, text_y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, color, 2, cv2.LINE_AA)


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


@app.errorhandler(404)
def not_found(_error):
    return Response("""<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Page not found | LENZ</title><style>body{margin:0;background:#101214;color:#e8e9eb;font:16px system-ui;padding:10vh 8vw}a{color:#9eb8ff}h1{font-size:2rem}</style><h1>Page not found</h1><p>The address does not exist in this local application.</p><a href='/'>Return to camera monitor</a></html>""", 404, mimetype="text/html")


@app.post("/detect")
def detect():
    frame = cv2.imdecode(np.frombuffer(request.get_data(), np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify(error="Invalid image frame"), 400
    try:
        chairs, people, occupied, latency = analyse(frame)
    except Exception as exc:
        return jsonify(error="Detection failed", detail=str(exc)), 500
    with state_lock:
        previous = state["last_frame"]
        now = time.time()
        state.update(count=len(chairs), occupied=occupied, empty=len(chairs)-occupied,
                     chairs=chairs, people=people, latency_ms=round(latency, 1),
                     fps=round(1/(now-previous), 1) if previous and now > previous else 0,
                     last_frame=now, source_status="Live browser camera", source_label="Browser camera",
                     peak_chairs=max(state["peak_chairs"], len(chairs)))
    return jsonify(w=frame.shape[1], h=frame.shape[0], chairs=chairs, people=people,
                   occupied=occupied, empty=len(chairs)-occupied, latency_ms=round(latency, 1))


@app.post("/connect")
def connect():
    data = request.get_json(silent=True) or {}
    source = str(data.get("source", "")).strip()
    if not source:
        return jsonify(ok=False, error="Enter a camera index, stream URL, or video file path."), 400
    if not (source.isdigit() or re.match(r"^(rtsps?|https?)://", source, re.I) or os.path.isfile(source)):
        return jsonify(ok=False, error="Use a camera index, rtsp/http URL, or existing video file path."), 400
    stop_worker()
    reset_stats()
    worker = StreamWorker(source)
    with state_lock:
        state["worker"] = worker
        state["source_status"] = "Connecting"
        state["source_label"] = worker.label
    worker.start()
    return jsonify(ok=True)


def stop_worker():
    with state_lock:
        worker = state["worker"]
        state["worker"] = None
    if worker:
        worker.stop()


@app.post("/reset")
def reset():
    stop_worker()
    reset_stats()
    return jsonify(ok=True)


@app.post("/config")
def config():
    data = request.get_json(silent=True) or {}
    global PERSON_CONF, CHAIR_CONF
    try:
        if "person_conf" in data:
            PERSON_CONF = max(0.10, min(0.90, float(data["person_conf"])))
        if "chair_conf" in data:
            CHAIR_CONF = max(0.10, min(0.90, float(data["chair_conf"])))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Confidence must be a number from 0.10 to 0.90."), 400
    return jsonify(ok=True, person_conf=PERSON_CONF, chair_conf=CHAIR_CONF)


@app.get("/stats")
def stats():
    with state_lock:
        now = time.time()
        return jsonify(count=state["count"], occupied=state["occupied"], empty=state["empty"],
                       chairs=state["chairs"], people=state["people"], latency_ms=state["latency_ms"],
                       fps=state["fps"], live=bool(state["last_frame"] and now-state["last_frame"] < 3),
                       source_status=state["source_status"], source_label=state["source_label"],
                       peak_chairs=state["peak_chairs"], chair_conf=CHAIR_CONF, person_conf=PERSON_CONF)


@app.get("/stream.mjpg")
def stream():
    with state_lock:
        worker = state["worker"]
    if worker is None:
        return Response("No stream connected", status=404)
    def generate():
        last = -1
        while worker.running:
            with state_lock:
                if state["worker"] is not worker:
                    break
            if worker.jpeg is not None and worker.seq != last:
                last = worker.seq
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + worker.jpeg + b"\r\n"
            time.sleep(0.03)
    response = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    response.headers["Cache-Control"] = "no-store"
    return response


INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>LENZ | Chair Occupancy Monitor</title>
<meta name="description" content="Local camera-based chair occupancy monitoring with per-chair occupied and empty labels.">
<meta name="robots" content="noindex,nofollow">
<style>
:root{color-scheme:dark;--bg:#101214;--panel:#171a1e;--line:#30353c;--text:#e8e9eb;--muted:#a1a7b0;--blue:#9eb8ff;--green:#69c58b;--red:#ed7777;--amber:#e8bc6c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.45}.wrap{max-width:1440px;margin:auto;padding:0 28px}header{min-height:76px;display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line)}.brand{font-size:20px;font-weight:800;letter-spacing:.14em}.brand span{color:var(--blue)}.subbrand{font-size:13px;color:var(--muted);margin-top:2px}.status{font-size:12px;letter-spacing:.08em;color:var(--muted)}.status:before{content:"";display:inline-block;width:8px;height:8px;background:#777;border-radius:50%;margin-right:8px}.status.on:before{background:var(--green)}.intro{padding:28px 0 22px}.eyebrow{font-size:12px;color:var(--blue);letter-spacing:.13em;font-weight:700}.intro h1{font-size:clamp(27px,3vw,42px);letter-spacing:-.035em;line-height:1.08;margin:9px 0}.intro p{color:var(--muted);margin:0}.layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);gap:18px;padding-bottom:28px}.panel{border:1px solid var(--line);background:var(--panel);padding:18px;min-width:0}.panelhead{display:flex;justify-content:space-between;gap:12px;align-items:start;margin-bottom:14px}.panel h2{font-size:15px;margin:0;font-weight:700}.muted{color:var(--muted);font-size:13px}.stage{position:relative;background:#08090a;border:1px solid var(--line);overflow:hidden;min-height:220px}.stage img,.stage video{display:block;width:100%;height:auto;max-height:72vh;object-fit:contain}.stage canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}.placeholder{min-height:320px;display:flex;align-items:center;justify-content:center;text-align:center;padding:28px;color:var(--muted)}.controls{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:14px}.controls input{min-width:0}.row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px}input,select,button{font:inherit;color:var(--text);background:#101214;border:1px solid #414750;border-radius:2px;padding:10px 12px}input:focus,select:focus,button:focus-visible{outline:2px solid var(--blue);outline-offset:2px}button{cursor:pointer;background:#293342;border-color:#485a73}button.secondary{background:transparent}button:hover{border-color:var(--blue)}label{font-size:13px;color:var(--muted)}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-top:1px solid var(--line);border-left:1px solid var(--line);margin-bottom:18px}.metric{padding:15px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.metric .value{font-size:34px;font-weight:700;letter-spacing:-.04em}.metric .label{font-size:12px;color:var(--muted);margin-top:2px}.section{margin-top:20px}.section h2{margin-bottom:12px}.chair-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.chair{padding:11px;border:1px solid var(--line);display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:13px}.chair b{font-size:11px;letter-spacing:.05em}.occupied{color:var(--red)}.empty{color:var(--green)}.notice{border-top:1px solid var(--line);margin-top:18px;padding-top:14px;color:var(--muted);font-size:12px}.confidence{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;margin-top:12px}.confidence input{width:100%;padding:0;accent-color:var(--blue)}footer{border-top:1px solid var(--line);padding:16px 0 24px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.error{color:var(--red);font-size:13px;margin-top:10px;min-height:1.3em}.hidden{display:none!important}@media(max-width:900px){.layout{grid-template-columns:1fr}.wrap{padding:0 16px}.intro{padding-top:22px}.placeholder{min-height:220px}}@media(max-width:520px){.chair-list{grid-template-columns:1fr}.controls{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.metric .value{font-size:29px}header{align-items:flex-start;padding:15px 0;flex-direction:column;gap:7px}}
</style></head><body><div class="wrap">
<header><div><div class="brand">LEN<span>Z</span></div><div class="subbrand">Chair occupancy monitor</div></div><div id="liveStatus" class="status">OFFLINE</div></header>
<section class="intro"><div class="eyebrow">LIVE MONITORING</div><h1>See which chairs are occupied.</h1><p>Connect a camera to label detected chairs individually and count occupied versus empty seats.</p></section>
<main class="layout"><section class="panel"><div class="panelhead"><div><h2>Camera feed</h2><div class="muted" id="sourceText">Choose a camera source to begin.</div></div><div class="muted" id="perf">Not processing</div></div>
<div class="stage" id="stage"><div id="placeholder" class="placeholder">No camera connected.<br>Start your device camera or connect a CCTV stream.</div><video id="video" autoplay muted playsinline class="hidden"></video><img id="stream" alt="Live camera feed with occupied and empty chair annotations" class="hidden"><canvas id="overlay"></canvas></div>
<div class="row"><button id="btnCam">Use this device's camera</button><button id="btnStop" class="secondary">Stop camera</button></div>
<div class="controls"><input id="src" aria-label="Camera source" placeholder="Camera index, rtsp/http URL, or video file path"><button id="btnConnect">Connect source</button></div><div class="error" id="error" role="status" aria-live="polite"></div>
<div class="section"><h2>Detection sensitivity</h2><div class="confidence"><label for="chairConf">Chair confidence</label><output id="chairConfOut">16%</output></div><input id="chairConf" type="range" min="10" max="70" value="16"><div class="confidence"><label for="personConf">Person confidence</label><output id="personConfOut">22%</output></div><input id="personConf" type="range" min="10" max="80" value="22"><p class="muted">Lower chair confidence may find more chairs but can add false detections. Change one setting at a time.</p></div></section>
<aside><section class="panel"><h2>Occupancy</h2><div class="metrics"><div class="metric"><div class="value" id="occupied">0</div><div class="label">Occupied chairs</div></div><div class="metric"><div class="value" id="empty">0</div><div class="label">Empty chairs</div></div><div class="metric"><div class="value" id="total">0</div><div class="label">Chairs detected</div></div><div class="metric"><div class="value" id="people">0</div><div class="label">People detected</div></div></div><div class="muted" id="summary">Waiting for a camera frame.</div>
<div class="section"><h2>Chair-by-chair status</h2><div class="chair-list" id="chairList"><div class="muted">No chairs detected yet.</div></div></div><div class="notice">Green boxes indicate chairs classified as empty. Red boxes indicate chairs classified as occupied. The inner outline estimates the seat area used for classification.</div></section>
<section class="panel section"><h2>Local processing</h2><p class="muted">Frames are analysed on this computer and are not saved by this app. Detection depends on camera angle, lighting, chair visibility, and the model.</p><div class="muted" id="streamStatus">No source connected.</div></section></aside></main>
<footer><span>LENZ · Local computer vision</span><span>Detection is an estimate, not a safety-certified occupancy system.</span></footer></div>
<script>
const $=id=>document.getElementById(id);const video=$('video'),img=$('stream'),canvas=$('overlay'),ctx=canvas.getContext('2d');let token=0,mediaStream=null,lastConfig='',pollTimer=null;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function showError(msg){$('error').textContent=msg||''}
function stopOverlay(){canvas.width=1;canvas.height=1;ctx.clearRect(0,0,1,1)}
async function stopAll(){token++;if(mediaStream){mediaStream.getTracks().forEach(t=>t.stop());mediaStream=null}video.pause();video.srcObject=null;video.classList.add('hidden');img.removeAttribute('src');img.classList.add('hidden');stopOverlay();$('placeholder').classList.remove('hidden');try{await fetch('/reset',{method:'POST'})}catch(e){};updateOffline()}
function updateOffline(){$('liveStatus').textContent='OFFLINE';$('liveStatus').classList.remove('on');$('sourceText').textContent='Choose a camera source to begin.';$('perf').textContent='Not processing'}
async function startCam(){showError('');await stopAll();if(!navigator.mediaDevices?.getUserMedia){showError('Camera access requires localhost or HTTPS and a supported browser.');return}try{mediaStream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1280},height:{ideal:720}},audio:false})}catch(e){showError('Camera access failed: '+e.name+'. Allow camera permission in browser settings.');return}video.srcObject=mediaStream;try{await video.play()}catch(e){}video.classList.remove('hidden');$('placeholder').classList.add('hidden');const my=++token;detectLoop(my)}
async function detectLoop(my){const c=document.createElement('canvas'),cx=c.getContext('2d');while(my===token){if(video.videoWidth&&video.readyState>=2){const scale=Math.min(1,640/video.videoWidth);c.width=Math.round(video.videoWidth*scale);c.height=Math.round(video.videoHeight*scale);cx.drawImage(video,0,0,c.width,c.height);const blob=await new Promise(resolve=>c.toBlob(resolve,'image/jpeg',.86));if(!blob){await sleep(100);continue}try{const r=await fetch('/detect',{method:'POST',headers:{'Content-Type':'image/jpeg'},body:blob});const d=await r.json();if(!r.ok)throw Error(d.error||'Detection failed');if(my===token){drawBoxes(d.chairs,d.w,d.h);render(d)}}catch(e){showError('Detection request failed: '+e.message);await sleep(400)}}await sleep(100)}}
function drawBoxes(chairs,w,h){if(!w||!h)return;canvas.width=w;canvas.height=h;ctx.clearRect(0,0,w,h);ctx.lineWidth=Math.max(2,w/500);ctx.font=`${Math.max(13,w/75)}px Arial`;for(const c of chairs){const [x1,y1,x2,y2]=c.box,[sx1,sy1,sx2,sy2]=c.seat;const yes=c.status==='OCCUPIED',color=yes?'#ed7777':'#69c58b';ctx.strokeStyle=color;ctx.strokeRect(x1,y1,x2-x1,y2-y1);ctx.lineWidth=1;ctx.strokeRect(sx1,sy1,sx2-sx1,sy2-sy1);ctx.lineWidth=Math.max(2,w/500);const label=(yes?'OCCUPIED':'EMPTY')+' '+Math.round(c.confidence*100)+'%';const tw=ctx.measureText(label).width;ctx.fillStyle='#101214';ctx.fillRect(x1,Math.max(0,y1-24),tw+12,22);ctx.fillStyle=color;ctx.fillText(label,x1+6,Math.max(15,y1-8))}}
function render(d){$('occupied').textContent=d.occupied;$('empty').textContent=d.empty;$('total').textContent=d.chairs.length;$('people').textContent=d.people;$('summary').textContent=`${d.occupied} occupied · ${d.empty} empty · ${d.chairs.length} chairs detected`;$('perf').textContent=`AI ${Math.round(d.latency_ms)} ms`;$('liveStatus').textContent='LIVE';$('liveStatus').classList.add('on');$('sourceText').textContent='Browser camera';$('streamStatus').textContent='Browser camera active';renderChairList(d.chairs)}
function renderChairList(chairs){const list=$('chairList');list.replaceChildren();if(!chairs.length){const p=document.createElement('div');p.className='muted';p.textContent='No chairs detected. Try lowering chair confidence or changing the camera angle.';list.append(p);return}chairs.forEach((c,i)=>{const item=document.createElement('div');item.className='chair';const name=document.createElement('span');name.textContent=`Chair ${i+1}`;const status=document.createElement('b');status.className=c.status==='OCCUPIED'?'occupied':'empty';status.textContent=c.status;item.append(name,status);list.append(item)})}
async function connectStream(){showError('');const source=$('src').value.trim();if(!source){showError('Enter a camera index, stream URL, or video file path.');return}await stopAll();try{const r=await fetch('/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source})});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.error||'Could not connect');img.src='/stream.mjpg?t='+Date.now();img.classList.remove('hidden');$('placeholder').classList.add('hidden');$('sourceText').textContent='Connecting to stream';$('streamStatus').textContent='Opening source: '+source;pollStats()}catch(e){showError(e.message)}}
async function pollStats(){if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(async()=>{if(img.classList.contains('hidden'))return;try{const s=await(await fetch('/stats')).json();$('liveStatus').textContent=s.live?'LIVE':'CONNECTING';$('liveStatus').classList.toggle('on',s.live);$('sourceText').textContent=s.source_label||s.source_status;$('streamStatus').textContent=s.source_status||'Waiting for source';$('perf').textContent=s.live?`AI ${Math.round(s.latency_ms)} ms · ${s.fps} FPS`:'Waiting for frames';$('occupied').textContent=s.occupied;$('empty').textContent=s.empty;$('total').textContent=s.count;$('people').textContent=s.people;$('summary').textContent=`${s.occupied} occupied · ${s.empty} empty · ${s.count} chairs detected`;$('chairList').replaceChildren();if(s.chairs.length)renderChairList(s.chairs);else{$('chairList').textContent=s.live?'No chairs detected. Try lowering chair confidence or changing the camera angle.':'Waiting for a camera frame.'}}catch(e){}},500)}
async function saveConfig(){const chair=+$('chairConf').value/100,person=+$('personConf').value/100;$('chairConfOut').textContent=Math.round(chair*100)+'%';$('personConfOut').textContent=Math.round(person*100)+'%';const sig=chair+':'+person;if(sig===lastConfig)return;lastConfig=sig;try{await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chair_conf:chair,person_conf:person})})}catch(e){}}
$('btnCam').onclick=startCam;$('btnStop').onclick=async()=>{await stopAll();if(pollTimer)clearInterval(pollTimer);showError('')};$('btnConnect').onclick=connectStream;$('chairConf').onchange=saveConfig;$('personConf').onchange=saveConfig;window.addEventListener('beforeunload',()=>{if(navigator.sendBeacon)navigator.sendBeacon('/reset')});saveConfig();
</script></body></html>'''


if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"[LENZ] Ready at {url} (Ctrl+C to stop)")
    if os.environ.get("LENZ_DESKTOP") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)
