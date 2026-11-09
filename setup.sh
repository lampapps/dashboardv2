#!/usr/bin/env bash
# First-time setup: venv, dependencies, config files from the examples, empty database.
# Safe to re-run; never overwrites existing files.
# Usage: ./setup.sh [--dev]     (--dev also installs the test dependencies)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
DEV=0
[[ ${1:-} == "--dev" ]] && DEV=1

die() { echo "error: $*" >&2; exit 1; }

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 3.11 or newer is required"
python3 -c 'import ensurepip, venv' 2>/dev/null \
    || die "python3-venv is missing (sudo apt install python3-venv)"

[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
if ((DEV)); then
    .venv/bin/pip install --quiet -r requirements-dev.txt
fi

copy_if_missing() {  # copy_if_missing SRC DEST
    if [[ ! -e $2 ]]; then
        cp "$1" "$2"
        echo "created $2 from $1"
    fi
}
copy_if_missing config/config.example.yaml config/config.yaml
copy_if_missing config/agents.example.yaml config/agents.yaml
copy_if_missing .env.example .env
chmod 600 .env

mkdir -p data
.venv/bin/python -m app config
.venv/bin/python -m app initdb

cat <<EOF

Next steps:
  1. Edit config/config.yaml (timezone, drives, badges) and .env (secrets).
  2. Register an agent:   .venv/bin/python -m app agent add NAME
  3. Try it:              .venv/bin/python -m app web --dev
  4. Install services:    sudo deploy/install.sh
EOF
