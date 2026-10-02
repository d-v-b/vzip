#!/usr/bin/env bash
# Runs every fixture through the proxy (port 8802) and prints the exit status.
D="$(cd "$(dirname "$0")" && pwd)"
FIX="$D/../../../web/test/fixtures"
mkdir -p "$D/out"
for f in "$FIX"/*.tif "$FIX"/*.nd2; do
  b=$(basename "$f")
  rm -f "$D/out/$b.json"
  err=$("$D/virtualize" "http://127.0.0.1:8802/f/$b" "$D/out/$b.json" 2>&1 >/dev/null)
  st=$?
  case "$b" in unsupported_*|edge_reject_*|nd2_reject_*) exp=3;; *) exp=0;; esac
  mark=ok; [ $st -ne $exp ] && mark=MISMATCH
  echo "$mark $st $b ${err:0:150}"
done
