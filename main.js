const { app, BrowserWindow, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const fs = require('fs');

let win;
let backend;
let quitting = false;
const PORT = 8000;
const DASHBOARD_URL = `http://127.0.0.1:${PORT}`;
const VENV_PYTHON = process.platform === 'win32'
  ? path.join(__dirname, '.venv', 'Scripts', 'python.exe')
  : path.join(__dirname, '.venv', 'bin', 'python');

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: __dirname, stdio: 'inherit', ...options });
    child.once('error', reject);
    child.once('exit', code => code === 0 ? resolve() : reject(new Error(`${command} exited with code ${code}`)));
  });
}

function setStatus(message) {
  if (win && !win.isDestroyed()) {
    win.webContents.executeJavaScript(
      `document.getElementById('status').textContent = ${JSON.stringify(message)}`
    ).catch(() => {});
  }
}

async function ensurePythonEnvironment() {
  const systemPython = process.platform === 'win32' ? 'py' : 'python3';
  if (!fs.existsSync(VENV_PYTHON)) {
    setStatus('Creating Python environment…');
    await run(systemPython, process.platform === 'win32'
      ? ['-3', '-m', 'venv', '.venv']
      : ['-m', 'venv', '.venv']);
  }

  // Check imports in the project venv. If a previous setup was interrupted,
  // install requirements automatically instead of launching the wrong Python.
  const check = spawn(VENV_PYTHON, ['-c', 'import cv2, flask, numpy, ultralytics'], {
    cwd: __dirname, stdio: 'ignore'
  });
  const installed = await new Promise(resolve => {
    check.once('error', () => resolve(false));
    check.once('exit', code => resolve(code === 0));
  });
  if (!installed) {
    setStatus('Installing vision dependencies (first launch only)…');
    await run(VENV_PYTHON, ['-m', 'pip', 'install', '--upgrade', 'pip']);
    await run(VENV_PYTHON, ['-m', 'pip', 'install', '-r', path.join(__dirname, 'requirements.txt')]);
  }
  return VENV_PYTHON;
}

function startBackend(python) {
  backend = spawn(python, [path.join(__dirname, 'app.py')], {
    cwd: __dirname,
    env: {
      ...process.env,
      LENZ_DESKTOP: '1',
      LENZ_PORT: String(PORT),
      LENZ_MODEL: process.env.LENZ_MODEL || 'yolo11m.pt',
      LENZ_IMGSZ: process.env.LENZ_IMGSZ || '960',
      LENZ_DEVICE: process.env.LENZ_DEVICE || (process.platform === 'darwin' && process.arch === 'arm64' ? 'mps' : 'cpu'),
      OPENCV_FFMPEG_CAPTURE_OPTIONS: 'rtsp_transport;tcp'
    },
    stdio: 'inherit'
  });
  backend.on('error', err => setStatus(`Could not start Python: ${err.message}`));
  backend.on('exit', code => {
    if (code && !quitting) setStatus(`Vision engine exited (${code}). Check the terminal output.`);
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1320, height: 900, minWidth: 900, minHeight: 650,
    backgroundColor: '#101214',
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true }
  });
  const loading = `<!doctype html><html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>LENZ</title><style>body{margin:0;background:#101214;color:#e8e9eb;font:15px Arial;display:grid;place-items:center;min-height:100vh}.wrap{max-width:500px;padding:28px}.brand{font-weight:800;letter-spacing:.14em;font-size:22px}.brand span{color:#9eb8ff}p{color:#a1a7b0;line-height:1.5}#status{font-size:13px;color:#9eb8ff;margin-top:22px}</style><div class="wrap"><div class="brand">LEN<span>Z</span></div><p>Starting LENZ's local vision engine. First launch may install dependencies and download model weights.</p><div id="status">Preparing Python environment…</div></div></html>`;
  win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(loading));
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url);
    return { action: 'deny' };
  });
}

async function waitForBackend() {
  const deadline = Date.now() + 300000;
  while (!quitting && Date.now() < deadline) {
    try {
      const res = await fetch(`${DASHBOARD_URL}/stats`);
      if (res.ok) {
        if (win && !win.isDestroyed()) await win.loadURL(DASHBOARD_URL);
        return;
      }
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  setStatus('Vision engine did not start. Check the terminal output.');
}

app.whenReady().then(async () => {
  createWindow();
  try {
    const python = await ensurePythonEnvironment();
    if (quitting) return;
    startBackend(python);
    waitForBackend();
  } catch (err) {
    setStatus(`Python setup failed: ${err.message}. Check Python 3 and internet access.`);
    console.error('[LENZ] Python setup failed:', err);
  }
});
app.on('before-quit', () => {
  quitting = true;
  if (backend && !backend.killed) backend.kill();
});
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
