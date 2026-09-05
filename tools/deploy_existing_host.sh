#!/usr/bin/env bash
# Run on the existing Ubuntu host after pushing a reviewed commit.
# Usage: bash tools/deploy_existing_host.sh <full-commit-sha>
set -euo pipefail
commit=${1:?Pass the full tested commit SHA}
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'A full commit SHA is required.'; exit 1; }
app_dir=/opt/empire-mmr/app
backup_dir=/opt/empire-mmr/backups/companion-$(date -u +%Y%m%dT%H%M%SZ)
python=/opt/empire-mmr/venv/bin/python
cd "$app_dir"
previous=$(git rev-parse HEAD)
git cat-file -e "$commit^{commit}"
if ! git diff --quiet -- . ':!empire.db' || ! git diff --cached --quiet; then
  echo 'Source files have local changes. Review them before deploying.'
  exit 1
fi
if ! git diff --quiet "$previous" "$commit" -- empire.db; then
  echo 'Deployment changes the tracked database; refusing to overwrite live data.'
  exit 1
fi
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
printf '%s\n' "$previous" > "$backup_dir/previous-commit.txt"
rollback() {
  echo 'Deployment check failed. Restoring the previous code revision.'
  runuser -u empire -- git -C "$app_dir" switch --detach "$previous"
  systemctl restart empire-mmr
  echo "Database backup: $backup_dir/empire.db (not automatically restored)."
}
"$python" -c 'from importlib.metadata import version; assert tuple(map(int, version("flask").split(".")[:2])) >= (3, 1); assert version("openskill") == "6.2.0"'
trap rollback ERR
systemctl stop empire-mmr
"$python" - "$backup_dir/empire.db" <<'PY'
import sqlite3, sys
with sqlite3.connect('empire.db') as source, sqlite3.connect(sys.argv[1]) as destination:
    source.backup(destination)
    assert destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
PY
tar -czf "$backup_dir/source.tar.gz" app.py requirements.txt static/style.css
runuser -u empire -- git -C "$app_dir" switch --detach "$commit"
systemctl restart empire-mmr
for attempt in $(seq 1 10); do
  if curl --fail --silent http://127.0.0.1:8089/health > "$backup_dir/health.json"; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:8089/health > "$backup_dir/health.json"
curl --fail --silent http://127.0.0.1:8089/companion > /dev/null
"$python" - "$backup_dir/empire.db" <<'PY'
import sqlite3, sys
before = sqlite3.connect(sys.argv[1]); before.row_factory = sqlite3.Row
after = sqlite3.connect('empire.db'); after.row_factory = sqlite3.Row
for table in ('players', 'matches'):
    for old in before.execute('SELECT * FROM ' + table):
        current = after.execute('SELECT * FROM ' + table + ' WHERE id=?', (old['id'],)).fetchone()
        assert current is not None and all(current[key] == old[key] for key in old.keys()), (table, old['id'])
print('Existing player and match fields preserved.')
PY
trap - ERR
echo "Deployed $commit. Backup: $backup_dir"
