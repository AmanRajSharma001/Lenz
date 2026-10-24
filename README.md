# LENZ Desktop

A minimal Electron desktop wrapper for LENZ's local YOLO chair-occupancy detector. The app opens directly into the camera monitor; the slow Gemma prompt-generation screen and its API dependency have been removed.

## macOS (fish)

```fish
cd lenz-desktop
python3 -m venv .venv
source .venv/bin/activate.fish
python -m pip install -r requirements.txt
npm install
npm start
```

The first launch downloads YOLO weights if they are not already present. The default model is `yolo11m.pt` at 960px inference size for better person/chair detection recall. This is heavier and may run slower, but is a better accuracy baseline. On Apple Silicon, LENZ tries PyTorch MPS and falls back to CPU if MPS is unavailable. For CPU-only mode, set `LENZ_DEVICE=cpu` in your environment before `npm start`.

## Windows

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
npm install
npm start
```

## Performance notes

- LENZ uses the medium YOLO11 model at 960px for stronger detections; set `LENZ_MODEL=yolo11n.pt` and `LENZ_IMGSZ=640` if speed matters more than accuracy.
- `npm start` creates the project `.venv` and installs missing Python dependencies automatically. This fixes the `ModuleNotFoundError: cv2` issue caused by using system Python.
- The extra model warm-up pass has been removed.
- The app opens directly to the monitor; no network request to a generative AI API is required.
- Chair occupancy is still an estimate and depends on camera angle and visibility.
