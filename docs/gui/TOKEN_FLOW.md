# Local API token flow (Phase UI-3 item 2)

FABLE §14 requires a local session token where necessary and forbids
bundling secrets into the build. The Field Horizon API already requires a
bearer token on every route (`fieldhorizon/server.py`'s `require_token`); this
doc covers how each delivery target actually gets one.

## Where the token comes from

`field-horizon serve` (`fieldhorizon/cli.py:cmd_serve`) creates the token file
if it doesn't exist (`chmod 600`) and prints its path on every start:

```text
Token file: .fh_token (created with chmod 600 if new)
Serving on http://127.0.0.1:8777 (Ctrl-C to stop)
```

The token itself is whatever `load_or_create_token` wrote to that file —
never derived from anything predictable, never logged in full elsewhere.

## Browser build (`/ui`)

The static assets served at `/ui` (`fieldhorizon/server.py`'s `UI_DIST_DIR`
mount) carry no secrets and need no auth to load — only the real API routes
are token-gated. On first load with no token in `sessionStorage`,
`apps/desktop/src/auth/TokenGate.tsx` blocks the app behind a paste-token
screen:

1. User runs `field-horizon serve` (or it's already running).
2. User reads the printed token file's contents, e.g. `cat .fh_token`.
3. User pastes it into the token field and submits.
4. `tokenStorage.ts` writes it to `sessionStorage` under
   `field-horizon-api-token` — cleared automatically when the tab closes,
   never written to disk by the frontend, never present in any built JS/CSS
   asset (`grep` the `dist/` output for the literal token to confirm — there
   is nothing to find, since it never exists at build time).

Future API calls (later phases) read the token from `tokenStorage.ts` to set
the `Authorization: Bearer <token>` header — not built yet, since there are no
real API-calling screens until Phase UI-4.

## Desktop build (Tauri)

Out of scope for this item — the connection manager (Phase UI-3 item 5)
owns token acquisition there, since a managed backend launch already knows
`token_file`'s path and an attach-to-existing-backend flow can reuse this
same `TokenGate` component. Not wired yet.

## Why sessionStorage, not localStorage

A token surviving across browser restarts is a bigger blast radius for no
real benefit in a local-only tool the user launches deliberately each time —
sessionStorage's automatic per-tab-lifetime expiry is the safer default here,
and re-pasting a token from a file already open in a terminal is a low-cost
action.
