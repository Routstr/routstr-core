#!/usr/bin/env bash
set -euo pipefail

ROOT="${HOME}/proxy"
MARKER="${ROOT}/.minibits-reconciliation-complete"
LOCK="${ROOT}/.minibits-reconciliation.lock"
LOG="${ROOT}/logs/minibits-reconciliation.log"
TAG="ROUTSTR-MINIBITS-RECONCILE"

cd "$ROOT"
[[ -e "$MARKER" ]] && exit 0
exec 9>"$LOCK"
flock -n 9 || exit 0

printf '%s starting reconciliation retry\n' "$(date -Is)" >>"$LOG"
if ! .venv/bin/python scripts/reconcile_reserved_proofs.py --root . --timeout 90 --apply >>"$LOG" 2>&1; then
  printf '%s reconciliation command failed\n' "$(date -Is)" >>"$LOG"
  exit 0
fi

latest=$(ls -1t reconciliation-applied-*.json 2>/dev/null | head -1 || true)
[[ -n "$latest" ]] || exit 0
if .venv/bin/python - "$latest" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
error_keys = report.get("errors", {})
raise SystemExit(any(key.startswith("https://mint.minibits.cash/Bitcoin|") for key in error_keys))
PY
then
  if .venv/bin/python - <<'PY' >>"$LOG" 2>&1
import sqlite3
keys = sqlite3.connect("keys.db")
wallet = sqlite3.connect(".wallet/wallet.sqlite3")
positive_balances = keys.execute(
    "SELECT COALESCE(SUM(CASE WHEN balance > 0 THEN balance ELSE 0 END), 0) FROM api_keys"
).fetchone()[0]
pending_refunds = keys.execute(
    "SELECT COALESCE(SUM(CASE WHEN unit = 'sat' THEN amount * 1000 ELSE amount END), 0) "
    "FROM cashu_transactions WHERE type = 'out' AND collected = 0 AND swept = 0"
).fetchone()[0]
fees = keys.execute("SELECT COALESCE(SUM(accumulated_msats), 0) FROM routstr_fees").fetchone()[0]
liquid_msats = wallet.execute(
    "SELECT COALESCE(SUM(CASE WHEN k.unit = 'msat' THEN p.amount ELSE p.amount * 1000 END), 0) "
    "FROM proofs p JOIN keysets k ON k.id = p.id WHERE COALESCE(p.reserved, 0) = 0"
).fetchone()[0]
obligations = positive_balances + pending_refunds + fees
print(f"liquid_msats={liquid_msats} obligations_msats={obligations} surplus_msats={liquid_msats-obligations}")
keys.close(); wallet.close()
raise SystemExit(0 if liquid_msats >= obligations else 1)
PY
  then
    touch "$MARKER"
    chmod 600 "$MARKER"
    printf '%s Minibits reconciliation completed and solvency verified; starting Routstr\n' "$(date -Is)" >>"$LOG"
    docker compose up -d routstr >>"$LOG" 2>&1
    (crontab -l 2>/dev/null | grep -v "$TAG" || true) | crontab -
  else
    printf '%s reconciliation completed but liquid assets remain below obligations; node stays stopped\n' "$(date -Is)" >>"$LOG"
  fi
else
  printf '%s Minibits remains unavailable; retry retained\n' "$(date -Is)" >>"$LOG"
fi
