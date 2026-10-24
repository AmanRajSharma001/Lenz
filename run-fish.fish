#!/usr/bin/env fish
cd (dirname (status --current-filename))
if not test -d .venv
    python3 -m venv .venv
end
source .venv/bin/activate.fish
python -m pip install -r requirements.txt
npm install
npm start
