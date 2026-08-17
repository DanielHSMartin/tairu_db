#!/usr/bin/env bash
#
# Build AND verify the TairuDB QGIS plugin release zip.
#
# Runs the same gate checks as plugins.qgis.org (secrets, hidden/suspicious
# files, metadata parsing) BEFORE producing the zip, so an upload is never
# rejected after the fact. The packaged file list mirrors pb_tool.cfg.
#
# Usage:  bash build_release.sh
# Output: ../tairu_db-<version>.zip  (version read from metadata.txt)
#
set -euo pipefail

PLUGIN=tairu_db
SRC="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3}"

fail() { printf '\xe2\x9d\x8c %b\n' "$*" >&2; exit 1; }

version=$(grep -E '^version=' "$SRC/metadata.txt" | head -1 | cut -d= -f2 | tr -d '[:space:]')
[ -n "$version" ] || fail "version not found in metadata.txt"
out="$SRC/../${PLUGIN}-${version}.zip"

stage_parent=$(mktemp -d)
stage="$stage_parent/$PLUGIN"
trap 'rm -rf "$stage_parent"' EXIT
mkdir -p "$stage"

# ---- stage files (mirror pb_tool.cfg) ----
flat_files="__init__.py tairu_db.py tairu_db_algorithm.py tairu_db_provider.py geopdf_converter.py compat.py qgis_proxy.py
            metadata.txt README.html TAIRUDB_SCHEMA.txt icon.png LICENSE"
package_dirs="tairu_core tairu_ui tairu_firebase tairu_sync"

for f in $flat_files; do
  [ -e "$SRC/$f" ] || fail "missing file listed in build: $f"
  cp "$SRC/$f" "$stage/"
done

# Exclusions kept in sync with the QGIS gate checks below:
#   __pycache__/*.pyc  -> never ship caches
#   .*                 -> hidden files (e.g. Sphinx .buildinfo) -> "Suspicious Files"
#   *.example          -> dev-only templates (config.py.example) -> "Secrets Detection"
excludes=(--exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo'
          --exclude '.*' --exclude '*.example' --exclude '.DS_Store')
for d in $package_dirs; do
  [ -d "$SRC/$d" ] || fail "missing dir listed in build: $d"
  rsync -a "${excludes[@]}" "$SRC/$d" "$stage/"
done
mkdir -p "$stage/help"
rsync -a "${excludes[@]}" "$SRC/help/build/html/" "$stage/help/"
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "staged $(find "$stage" -type f | wc -l | tr -d ' ') files into $PLUGIN/"

# ---- CHECK 1: no hidden files (QGIS "Suspicious Files: Hidden file detected") ----
hidden=$(find "$stage" -name '.*' -type f || true)
[ -z "$hidden" ] || fail "hidden files in package:\n$hidden"

# ---- CHECK 2: no caches / dev templates ----
junk=$(find "$stage" \( -name '*.pyc' -o -name '*.pyo' -o -name '*.example' -o -name '__pycache__' \) || true)
[ -z "$junk" ] || fail "disallowed files in package:\n$junk"

# ---- CHECK 3: metadata.txt parses with ConfigParser interpolation (QGIS "metadata") ----
# The repo reads metadata.txt with configparser (BasicInterpolation): a raw '%'
# in any value (e.g. "80%") breaks the upload. Accessing each value forces it.
"$PYTHON" - "$stage/metadata.txt" <<'PY' || fail "metadata.txt failed ConfigParser validation (stray '%'? escape as '%%' or reword)"
import configparser, sys
p = configparser.ConfigParser()
p.read(sys.argv[1], encoding='utf-8')
for key in p['general']:
    p.get('general', key)
assert p.get('general', 'version'), 'no version'
print('metadata OK: version', p.get('general', 'version'))
PY

# ---- CHECK 4: detect-secrets (QGIS "Secrets Detection") ----
# Mark intentional public values (e.g. the Firebase web API key) with a trailing
# '# pragma: allowlist secret' comment, which detect-secrets honours.
if command -v detect-secrets >/dev/null 2>&1; then
  findings=$( (cd "$stage" && detect-secrets scan --all-files \
      --exclude-files 'metadata\.txt' --exclude-files '\.secrets\.baseline') \
    | "$PYTHON" -c 'import json,sys; print(sum(len(v) for v in json.load(sys.stdin).get("results",{}).values()))' )
  if [ "${findings:-0}" != "0" ]; then
    (cd "$stage" && detect-secrets scan --all-files | "$PYTHON" -m json.tool | grep -E '"filename"|"type"' || true)
    fail "detect-secrets found $findings potential secret(s) — add '# pragma: allowlist secret' or exclude the file"
  fi
  echo "detect-secrets OK (0 findings)"
else
  echo "WARNING: detect-secrets not installed (pip install detect-secrets) — secret scan skipped"
fi

# ---- CHECK 5: bandit BLOCKING rules (plugins.qgis.org/docs/security-scanning/rules) ----
# NOT "--severity-level medium". The repo classifies PER RULE, not by severity:
# B105 (hardcoded password) is Bandit-LOW and blocks publication, so a severity
# filter hides exactly the findings that get a plugin rejected. That mistake
# shipped a blocked upload once — do not reintroduce it.
# B110/B112 (try/except/pass) are Warning-level there and are NOT checked here.
blockers="B102,B105,B106,B107,B304,B305,B307,B506,B602,B613"
if "$PYTHON" -m bandit --version >/dev/null 2>&1; then
  # `|| true` on BOTH: bandit exits 1 when it finds something, and with
  # `set -e -o pipefail` that killed the script right here — aborting the build
  # with no message at all. The report has to say WHY it refused.
  report=$("$PYTHON" -m bandit -r "$stage" -f json -t "$blockers" 2>/dev/null || true)
  hits=$(printf '%s' "$report" \
    | "$PYTHON" -c 'import json,sys; print(len(json.load(sys.stdin)["results"]))' || echo 0)
  if [ "${hits:-0}" != "0" ]; then
    printf '%s' "$report" | "$PYTHON" -c '
import json, sys
for r in json.load(sys.stdin)["results"]:
    print("  {} {}:{}  {}".format(
        r["test_id"], r["filename"], r["line_number"], r["issue_text"]))
' || true
    fail "bandit found $hits BLOCKING finding(s) — plugins.qgis.org would refuse this upload"
  fi
  echo "bandit OK (0 blocking findings)"
else
  echo "WARNING: bandit not installed (pip install bandit) — blocking-rule scan skipped"
fi

# ---- CHECK 6: no executable/binary extensions (QGIS "FILE_SUSPICIOUS") ----
suspicious=$(find "$stage" \( -name '*.exe' -o -name '*.dll' -o -name '*.so' \
                              -o -name '*.sh' -o -name '*.bat' -o -name '*.cmd' \) || true)
[ -z "$suspicious" ] || fail "suspicious file types in package:\n$suspicious"
echo "file types OK"

# ---- CHECK 7: flake8, com os MESMOS parametros do servidor ----
# `--max-line-length=120`, lido de qgis-app/plugins/security_scanner.py. Rodar
# com um limite maior esconde E501 que o servidor reporta: aconteceu, com 200.
if "$PYTHON" -m flake8 --version >/dev/null 2>&1; then
  f8=$("$PYTHON" -m flake8 --max-line-length=120 "$stage" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${f8:-0}" != "0" ]; then
    "$PYTHON" -m flake8 --max-line-length=120 "$stage" 2>/dev/null | head -20
    echo "WARNING: flake8 reportou $f8 achado(s) — nao bloqueiam a publicacao,"
    echo "         mas contam no relatorio e podem levar a revisao manual."
  else
    echo "flake8 OK (0 achados)"
  fi
else
  echo "WARNING: flake8 nao instalado (pip install flake8) — scan de qualidade pulado"
fi

# ---- package ----
rm -f "$out"
( cd "$stage_parent" && zip -9rq "$out" "$PLUGIN" )
printf '\xe2\x9c\x85 built %s (%s)\n' "$out" "$(du -h "$out" | cut -f1)"
