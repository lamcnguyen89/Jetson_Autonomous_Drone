#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
python_bin="$project_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    echo "Create the project .venv and install simulation/requirements.txt first." >&2
    exit 1
fi
exec "$python_bin" -m pytest -c "$project_root/pytest.ini" \
    "$project_root/simulation/tests" -rx -W ignore::DeprecationWarning \
    --log-level=INFO "$@"
