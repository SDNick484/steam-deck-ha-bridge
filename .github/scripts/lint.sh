#!/bin/sh
# Lint: shellcheck on every shell script, py_compile on every Python file.
# (No systemd-analyze step: the service unit is written by install.sh at install time, not stored here.)
set -eu
cd "$(dirname "$0")/../.."

status=0
echo "== shellcheck"
# SC1090/SC1091: sourced file names are only known at run time.
# shellcheck disable=SC2046
shellcheck --exclude=SC1090,SC1091 $(git ls-files '*.sh') || status=1

echo "== py_compile"
for f in $(git ls-files '*.py'); do
  python3 -m py_compile "$f" || status=1
done
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

[ "$status" = 0 ] && echo "lint passed"
exit "$status"
