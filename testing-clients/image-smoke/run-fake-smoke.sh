#!/bin/sh
# No credentials, network pulls, global pruning, or fixed container names.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
CONTEXT="$ROOT/testing-clients/image-smoke"
PYTHON=${PYTHON:-python3}
IMAGE=localhost/routstr-image-fake:smoke
podman build --pull=never -t "$IMAGE" "$CONTEXT"
CID=$(podman run --detach --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --label io.routstr.image-smoke=true \
  -p 127.0.0.1::8080 "$IMAGE")
cleanup() { podman rm --force --time 1 "$CID" >/dev/null; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
PORT=$(podman port "$CID" 8080 | sed 's/.*://')
"$PYTHON" - "$PORT" <<'PY'
import sys,time,urllib.request
url='http://127.0.0.1:'+sys.argv[1]+'/health'
for attempt in range(30):
    try:
        with urllib.request.urlopen(url,timeout=1) as r:
            assert r.status==200
        break
    except OSError:
        time.sleep(.2)
else:
    raise SystemExit('Fake upstream did not become healthy')
PY
"$PYTHON" "$ROOT/scripts/smoke_images.py"
"$PYTHON" "$ROOT/scripts/smoke_images.py" --execute --fake \
  --url "http://127.0.0.1:$PORT/api/v1" --max-quoted-usd 0.01
