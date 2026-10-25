# LENZ Pro

Describe a use case, connect any camera, get live analytics.

## Run
```
python3 -m venv .venv
source .venv/bin/activate.fish   # bash/zsh: source .venv/bin/activate
pip install -r requirements.txt
npm install
npm start
```
`npm start` also creates the venv by itself. Edit `lenz.config.json` to change the Gemini key and model id (set the Gemma 4 id there).
Optional custom training: `pip install -r requirements-train.txt`.
Build installers: `npm run dist` (run on each target OS).
