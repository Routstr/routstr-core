# Routstr node admin UI

A [Next.js](https://nextjs.org) app (App Router, **static export**) that provides the
admin dashboard for a `routstr-core` node: login, settings, providers, balances,
transactions, usage, and logs.

There is no separate web server in production. `next build` produces a fully static
export (`next.config.ts` sets `output: 'export'`), and the FastAPI backend serves it
directly from `../ui_out/` (see `routstr/core/main.py`). So the UI and the API are
served from the **same origin** in production.

## Developing the UI (hot reload)

The everyday loop runs two processes side by side — you do **not** rebuild the static
export while developing:

1. Start the backend on `:8000` — from the repo root: `make docker-up` (or
   `uvicorn routstr.core.main:app --reload`).
2. Start the Next.js dev server on `:3000` — from the repo root: `make ui-dev`
   (or `cd ui && pnpm dev`). Edits hot-reload instantly.

Open http://localhost:3000. With no `NEXT_PUBLIC_API_URL` set, the UI falls back to
`http://127.0.0.1:8000` in development (see `lib/api/services/configuration.ts`), so it
talks to the local backend out of the box.

Because dev is cross-origin (`:3000` → `:8000`), it relies on the backend's CORS
allowing the UI origin. The default `cors_origins` is `["*"]`; if you tighten CORS,
keep `http://localhost:3000` allowed for development.

## Building the integrated/static UI (what production serves)

To produce the bundle that FastAPI serves from `../ui_out/`:

- `make ui-build` — builds with local Node/pnpm (`scripts/build-ui.sh`), then moves
  `ui/out/*` to `../ui_out/`.
- `make ui-build-docker` — same, but inside Docker (no local Node needed).

`NEXT_PUBLIC_*` variables are read from the repo-root `.env` at build time and baked in.
For a same-origin deployment leave `NEXT_PUBLIC_API_URL` empty (relative paths); the UI
uses `window.location.origin` at runtime. After building, start the backend and open
http://localhost:8000 — the dashboard is served at `/` and `/admin`.

If `../ui_out/` does not exist, the backend logs a warning at startup and serves the API
only (hitting a UI route returns a small JSON fallback instead of the dashboard).
