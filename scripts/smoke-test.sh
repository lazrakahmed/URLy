#!/usr/bin/env bash
# Smoke test against a running deployment. Used after the first deploy and by the pipeline.
# Usage: scripts/smoke-test.sh http://<alb-dns-name>
set -euo pipefail
BASE="${1:?usage: smoke-test.sh <base-url>}"
BASE="${BASE%/}"

fail() { echo "SMOKE TEST FAILED: $*" >&2; exit 1; }
code() { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true; }

# A fresh deploy needs a moment: retry /readyz for up to 2 minutes.
for i in $(seq 1 24); do
  [ "$(code "$BASE/readyz")" = "200" ] && break
  [ "$i" = 24 ] && fail "/readyz never returned 200"
  sleep 5
done
echo "ok  /readyz 200"

[ "$(code "$BASE/")" = "200" ] || fail "UI page / is not 200"
echo "ok  / 200"

RESPONSE=$(curl -s --max-time 10 -w '\n%{http_code}' -X POST "$BASE/api/links" \
  -H 'content-type: application/json' -d '{"url":"https://example.com/smoke-test"}')
[ "$(echo "$RESPONSE" | tail -1)" = "201" ] || fail "create link did not return 201: $RESPONSE"
SHORT=$(echo "$RESPONSE" | head -1 | python3 -c 'import sys,json; print(json.load(sys.stdin)["short_code"])')
echo "ok  create link 201 ($SHORT)"

LOCATION=$(curl -s -o /dev/null --max-time 10 -w '%{http_code} %{redirect_url}' "$BASE/$SHORT")
[ "$LOCATION" = "302 https://example.com/smoke-test" ] || fail "redirect gave: $LOCATION"
echo "ok  /$SHORT -> 302 https://example.com/smoke-test"

[ "$(code "$BASE/zzzzzzz")" = "404" ] || fail "unknown code is not 404"
echo "ok  unknown code 404"
echo "SMOKE TEST PASSED"
