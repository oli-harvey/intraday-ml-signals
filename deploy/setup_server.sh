#!/usr/bin/env bash
# Idempotent server setup for intraday-ml-signals (run as the deploy user).
# Mirrors the contrafact deployment style: read-only GitHub deploy key,
# clone in $HOME, user-level systemd + cron. Safe to re-run.
set -euo pipefail

REPO_SSH="git@github.com:oli-harvey/intraday-ml-signals.git"
APP="$HOME/intraday-ml-signals"
KEY="$HOME/.ssh/gh_intraday_deploy"

echo "== 1. deploy key =="
if [[ ! -f "$KEY" ]]; then
    ssh-keygen -t ed25519 -N "" -C "intraday-vps (read-only)" -f "$KEY"
    echo "NEW KEY — register the public half on the repo (Settings -> Deploy keys):"
fi
echo "--- public key ---"
cat "$KEY.pub"
echo "------------------"

echo "== 2. uv =="
if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

echo "== 3. clone/update repo =="
if [[ ! -d "$APP/.git" ]]; then
    GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes" git clone "$REPO_SSH" "$APP"
    git -C "$APP" config core.sshCommand "ssh -i $KEY -o IdentitiesOnly=yes"
else
    git -C "$APP" fetch origin main && git -C "$APP" reset --hard origin/main
fi

echo "== 4. venv (uv only, no global installs) =="
cd "$APP"
# System python may be too new for dependency wheels (e.g. 3.14); pin a
# uv-managed 3.12 instead.
[[ -d .venv ]] || uv venv --python 3.12
uv pip install -e ".[dev]"
mkdir -p data logs reports

echo "== 5. .env =="
if [[ ! -f .env ]]; then
    cp .env.example .env
    echo "!! EDIT $APP/.env with the Alpaca PAPER keys before starting the service"
fi

echo "== 6. systemd (user-level) =="
mkdir -p "$HOME/.config/systemd/user"
cp deploy/intraday-pipeline.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
loginctl enable-linger "$USER" 2>/dev/null || echo "(linger may need: sudo loginctl enable-linger $USER)"
echo "start with: systemctl --user enable --now intraday-pipeline"

echo "== 7. cron =="
echo "review deploy/crontab.txt and MERGE into 'crontab -e' by hand"
echo "   (do not blindly 'crontab deploy/crontab.txt' — contrafact crons share this user)"

echo "== done =="
.venv/bin/python -c "import signals, river, duckdb; print('imports OK')"
.venv/bin/pytest -q 2>&1 | tail -1
