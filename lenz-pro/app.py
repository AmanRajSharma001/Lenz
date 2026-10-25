"""LENZ backend: Gemma (Gemini API) turns a plain-language goal into a detection spec,
YOLO + ByteTrack produce live detections, a metrics engine computes occupancy, dwell and peaks."""
import os, json, time, threading, re, csv, io, datetime
import cv2, numpy as np, requests
from flask import Flask, request, jsonify, send_from_directory, Response, render_template_string
from ultralytics import YOLO

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('LENZ_PORT', 8000))
DEVICE = os.environ.get('LENZ_DEVICE', 'cpu')
IMGSZ = int(os.environ.get('LENZ_IMGSZ', 640))
MODEL_NAME = os.environ.get('LENZ_MODEL', 'yolo11m.pt')
CFG_PATH = os.path.join(BASE, 'lenz.config.json')
PEAKS_PATH = os.path.join(BASE, 'peaks.json')

app = Flask(__name__, static_folder=os.path.join(BASE, 'static'))
model = YOLO(MODEL_NAME)
custom_model = None
lock = threading.Lock()
SPEC = None
VISITS, TRACKS, PEAKS = [], {}, {}
TRAIN = {'state': 'idle', 'message': ''}
if os.path.exists(PEAKS_PATH):
    PEAKS = json.load(open(PEAKS_PATH))

def cfg():
    c = json.load(open(CFG_PATH)) if os.path.exists(CFG_PATH) else {}
    c['gemini_api_key'] = os.environ.get('LENZ_GEMINI_KEY', c.get('gemini_api_key', ''))
    c['gemini_model'] = os.environ.get('LENZ_GEMINI_MODEL', c.get('gemini_model', 'gemma-4-26b-a4b-it'))
    return c

PROMPT = """You configure a real-time camera analytics tool. Detectable classes (COCO): {names}.
User goal: {goal}
Reply with ONLY a JSON object, no markdown:
{{"title": short name, "summary": one sentence plan,
"count_classes": [classes to count live],
"container_classes": [furniture/spots that can be occupied, e.g. chair, couch],
"occupier_class": class that occupies them or null,
"track_dwell": true if time spent per visitor matters,
"custom_classes": [objects the user needs that are NOT in the COCO list, else empty],
"dataset": which public dataset would help for custom classes or empty,
"insights": [3 specific metrics this business should watch]}}
Only use class names exactly as written in the COCO list for the first three class fields."""

def ask_gemma(goal):
    c = cfg()
    if not c['gemini_api_key']:
        raise RuntimeError('No Gemini API key set. Add it in lenz.config.json.')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{c['gemini_model']}:generateContent"
    body = {'contents': [{'parts': [{'text': PROMPT.format(names=', '.join(model.names.values()), goal=goal)}]}]}
    r = requests.post(url, params={'key': c['gemini_api_key']}, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'Gemini API error {r.status_code}: {r.text[:400]}')
    parts = r.json()['candidates'][0]['content']['parts']
    text = ''.join(x.get('text', '') for x in parts if not x.get('thought'))
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        raise RuntimeError('Gemma did not return a usable plan. Try rewording your prompt.')
    return json.loads(m.group(0))

def ids_for(names):
    inv = {v: k for k, v in model.names.items()}
    return [inv[n] for n in names if n in inv]

@app.post('/analyse')
def analyse():
    global SPEC
    goal = (request.json or {}).get('prompt', '').strip()
    if not goal:
        return jsonify(error='Describe how you want to use LENZ.'), 400
    try:
        s = ask_gemma(goal)
    except Exception as e:
        return jsonify(error=str(e)), 502
    valid = set(model.names.values())
    for k in ('count_classes', 'container_classes'):
        s[k] = [c for c in s.get(k, []) if c in valid]
    if s.get('occupier_class') not in valid:
        s['occupier_class'] = None
    s['custom_classes'] = s.get('custom_classes') or []
    with lock:
        SPEC = s
        model.predictor = None  # reset tracker for new use case
        TRACKS.clear()
    return jsonify(spec=s)

def box_overlap(a, b):  # fraction of box b covered by box a
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / area

@app.post('/infer')
def infer():
    img = cv2.imdecode(np.frombuffer(request.data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify(error='bad frame'), 400
    now = time.time()
    s = SPEC
    wanted = set()
    if s:
        wanted = set(s['count_classes'] + s['container_classes'] + ([s['occupier_class']] if s['occupier_class'] else []))
    with lock:
        r = model.track(img, persist=True, imgsz=IMGSZ, device=DEVICE, conf=0.3, verbose=False,
                        classes=ids_for(wanted) if wanted else None, tracker='bytetrack.yaml')[0]
        dets = []
        for i, b in enumerate(r.boxes):
            tid = int(b.id[0]) if b.id is not None else None
            dets.append({'name': model.names[int(b.cls[0])], 'conf': float(b.conf[0]), 'id': tid,
                         'box': [float(v) for v in b.xyxyn[0]]})
        if custom_model is not None:
            for b in custom_model.predict(img, imgsz=IMGSZ, device=DEVICE, conf=0.35, verbose=False)[0].boxes:
                dets.append({'name': custom_model.names[int(b.cls[0])], 'conf': float(b.conf[0]), 'id': None,
                             'box': [float(v) for v in b.xyxyn[0]]})
        counts = {}
        for d in dets:
            counts[d['name']] = counts.get(d['name'], 0) + 1
        out = {'dets': dets, 'counts': counts, 'ts': now}
        if s:
            occ = s['occupier_class']
            conts = [d for d in dets if d['name'] in s['container_classes']]
            people = [d for d in dets if d['name'] == occ] if occ else []
            used = sum(1 for c in conts if any(box_overlap(p['box'], c['box']) > 0.25 for p in people))
            out['containers'] = {'total': len(conts), 'occupied': used}
            if s.get('track_dwell') and occ:
                for p in people:
                    if p['id'] is None: continue
                    t = TRACKS.setdefault(p['id'], {'first': now, 'last': now}); t['last'] = now
                for tid in [k for k, v in TRACKS.items() if now - v['last'] > 5]:
                    v = TRACKS.pop(tid)
                    if v['last'] - v['first'] > 3:
                        VISITS.append({'start': v['first'], 'end': v['last'], 'dwell': v['last'] - v['first']})
                live = [now - v['first'] for v in TRACKS.values()]
                done = [v['dwell'] for v in VISITS]
                out['dwell'] = {'active': len(live), 'avg_done': sum(done) / len(done) if done else 0,
                                'longest_live': max(live) if live else 0, 'visits_done': len(done)}
            n = len(people) if occ else sum(counts.get(c, 0) for c in s['count_classes'])
            dt = datetime.datetime.fromtimestamp(now); key = f'{dt.strftime("%a")}-{dt.hour:02d}'
            PEAKS[key] = max(PEAKS.get(key, 0), n)
            today = dt.strftime('%a')
            out['peaks'] = [PEAKS.get(f'{today}-{h:02d}', 0) for h in range(24)]
            out['hour'] = dt.hour
    return jsonify(out)

@app.get('/export.csv')
def export():
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(['start', 'end', 'dwell_seconds'])
    for v in VISITS:
        w.writerow([datetime.datetime.fromtimestamp(v['start']).isoformat(), datetime.datetime.fromtimestamp(v['end']).isoformat(), round(v['dwell'], 1)])
    w.writerow([]); w.writerow(['weekday-hour', 'peak_count'])
    for k, v in sorted(PEAKS.items()): w.writerow([k, v])
    return Response(buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=lenz-report.csv'})

@app.post('/reset')
def reset():
    VISITS.clear(); TRACKS.clear(); PEAKS.clear()
    return jsonify(ok=True)

def train_job(classes):
    global custom_model
    try:
        import fiftyone as fo, fiftyone.zoo as foz
        out = os.path.join(BASE, 'datasets', 'custom')
        TRAIN.update(state='running', message='Downloading Open Images samples for: ' + ', '.join(classes))
        for split, n in (('train', 600), ('validation', 150)):
            ds = foz.load_zoo_dataset('open-images-v7', split=split, label_types=['detections'], classes=classes,
                                      max_samples=n, dataset_name=f'lenz-{split}-{int(time.time())}')
            ds.export(export_dir=out, dataset_type=fo.types.YOLOv5Dataset, label_field='ground_truth',
                      classes=classes, split='val' if split == 'validation' else 'train')
        TRAIN['message'] = 'Training a custom detector (this takes a while)...'
        res = YOLO('yolo11n.pt').train(data=os.path.join(out, 'dataset.yaml'), epochs=30, imgsz=640, device=DEVICE,
                                       project=os.path.join(BASE, 'runs'), name='custom', exist_ok=True, verbose=False)
        custom_model = YOLO(os.path.join(res.save_dir, 'weights', 'best.pt'))
        TRAIN.update(state='done', message='Custom model is live.')
    except ImportError:
        TRAIN.update(state='error', message='Install training extras: pip install -r requirements-train.txt')
    except Exception as e:
        TRAIN.update(state='error', message=str(e)[:200])

@app.post('/train')
def train():
    if not SPEC or not SPEC['custom_classes']:
        return jsonify(error='This use case needs no custom training.'), 400
    if TRAIN['state'] == 'running':
        return jsonify(error='Training already running.'), 409
    threading.Thread(target=train_job, args=(SPEC['custom_classes'],), daemon=True).start()
    return jsonify(ok=True)

@app.get('/train/status')
def train_status():
    return jsonify(TRAIN)

@app.get('/stats')
def stats():
    return jsonify(ok=True, spec=SPEC, model=MODEL_NAME, device=DEVICE)

def persist_peaks():
    while True:
        time.sleep(30)
        json.dump(PEAKS, open(PEAKS_PATH, 'w'))
threading.Thread(target=persist_peaks, daemon=True).start()

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{t}} | LENZ</title><meta name="description" content="{{d}}"><link rel="stylesheet" href="/static/style.css">
<body class="doc"><nav class="crumbs" aria-label="Breadcrumb"><a href="/">Monitor</a> / <span>{{t}}</span></nav><main>{{body|safe}}</main></body></html>"""
def page(t, d, body, code=200):
    return render_template_string(PAGE, t=t, d=d, body=body), code

@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

@app.get('/help')
def help_page():
    faqs = [('Which cameras work?', 'Any camera your computer can open: built-in webcams, USB cameras, capture cards and phones used as webcams.'),
            ('Does video leave my computer?', 'Frames are analysed locally. Only your typed use case text is sent to the Gemini API.'),
            ('How accurate are the numbers?', 'Counts depend on camera angle, light and distance. Place the camera above and in front of the area for best results.'),
            ('What does training do?', 'If your goal needs objects the base model does not know, LENZ downloads matching Open Images data and trains a small detector.'),
            ('How fast does support reply?', 'We reply to every support email within one business day.')]
    body = '<h1>Help</h1>' + ''.join(f'<h2>{q}</h2><p>{a}</p>' for q, a in faqs)
    return page('Help', 'Answers to common LENZ questions.', body)

@app.get('/privacy')
def privacy():
    return page('Privacy policy', 'How LENZ handles camera and usage data.',
        '<h1>Privacy policy</h1><p>Camera frames are processed on your device and are not uploaded. The text you type into the prompt box is sent to the Google Gemini API to build your configuration. Reports are stored locally and you can delete them at any time. You are responsible for lawful use of cameras, including posting notices where required.</p>')

@app.get('/terms')
def terms():
    return page('Terms of service', 'Terms for using LENZ.',
        '<h1>Terms of service</h1><p>LENZ is provided as is. Analytics are estimates and must not be used for safety critical decisions. You must have the right to monitor any space you point a camera at and must follow local privacy law. We may update these terms and will note the date of change in the app.</p>')

@app.errorhandler(404)
def nf(e):
    return page('Page not found', 'This page does not exist.', '<h1>Page not found</h1><p>That address does not exist in LENZ. <a href="/">Return to the monitor</a> or read the <a href="/help">help page</a>.</p>', 404)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=PORT, threaded=True)
