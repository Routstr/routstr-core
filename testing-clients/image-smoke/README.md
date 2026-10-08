# Isolated Images API smoke

This fake upstream returns a fixed valid 1×1 PNG and cost-only usage. It never
contacts OpenRouter, a mint, or any other service, and contains no credentials.
It tests the **fake transport/client contract**, not Routstr's account settlement
or production discovery. Images are initially bearer/account-only; image Cashu
requests are rejected before redemption.

## Run without pulling images

From the repository root, with Podman and the cached project Python runtime:

```sh
PYTHON=../../.venv/bin/python sh testing-clients/image-smoke/run-fake-smoke.sh
```

Adjust `PYTHON` to your interpreter. The script builds only this small directory,
uses an ephemeral loopback port and container ID, and removes only its own
container. It does not install dependencies, pull images, or globally prune.
The default base is `ghcr.io/astral-sh/uv:python3.11-bookworm-slim`; prefetch it
separately if absent, or supply another cached Python base via a manual build.
The local image remains available for reuse.

Alternatively, for interactive checks:

```sh
PROJECT="routstr-image-smoke-$(date +%s)"
podman-compose -p "$PROJECT" -f testing-clients/image-smoke/compose.yml up --build -d
python3 scripts/smoke_images.py --execute --fake \
  --url http://127.0.0.1:18091/api/v1 --max-quoted-usd 0.01
podman-compose -p "$PROJECT" -f testing-clients/image-smoke/compose.yml down
```

`IMAGE_SMOKE_PORT` overrides the loopback-only default port; select an unused
port. There are no fixed container names or volumes. Compose may pull a missing
base image, so prefer the script when offline execution is required.

## Routes

- `GET /health`
- `GET /api/v1/models` (empty text-only default)
- `GET /api/v1/models?output_modalities=text,image`
- `GET /api/v1/embeddings/models`
- `GET /api/v1/images/models`
- `GET /api/v1/images/models/recraft/recraft-v4.1-flash/endpoints`
- `POST /api/v1/images` (fixed USD 0.007 per returned image, zero tokens)

Model/provider names reproduce the schema, not a real generation. No `model` or
`id` is required in the response. Native reference inputs are not implemented by
this particular fake endpoint.

## Full Routstr container coverage

Do not point a production OpenRouter configuration at this fake or seed a real
node database. The production integration intentionally restricts generation to
OpenRouter's configured origin, so this fake-only scaffold does not silently
relax that restriction. Full isolated container account testing needs a
**test-only transport injection and disposable funded account/database**, plus
controlled BTC/USD exchange rate and disabled background networking. Run the
mocked image forwarding/account-settlement pytest coverage for those paths
instead. Cashu image rejection should be tested before redemption; image Cashu
settlement is not enabled.
Never use `tests/run_integration.py` for this scaffold: its cleanup globally
prunes containers/volumes.

Verification performed: fake image built with `--pull=never`, one ephemeral
loopback container started, both dry-run and fake execution passed, all discovery
routes checked, output decoded with Pillow as a valid 1×1 PNG, and that container
removed. No full Routstr container or live OpenRouter generation has been run.
