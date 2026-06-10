#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v rclone >/dev/null || echo "WARNING: rclone not found - install it and run: rclone config"
command -v rcon-cli >/dev/null || echo "WARNING: rcon-cli not found - https://github.com/itzg/rcon-cli/releases"
command -v zstd >/dev/null || echo "WARNING: zstd not found - config archives need it"

python3 -m venv venv
venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env

echo
echo "Done. Next steps:"
echo "  1. Fill in .env (token, IDs, paths)"
echo "  2. Make sure rclone has a remote: rclone lsd \$(grep RCLONE_REMOTE .env | cut -d= -f2 | cut -d: -f1):"
echo "  3. Test run:  set -a; . ./.env; set +a; venv/bin/python bot.py"
echo "  4. Install service: edit paths in mcbot.service if needed, then"
echo "     cp mcbot.service /etc/systemd/system/ && systemctl enable --now mcbot"
