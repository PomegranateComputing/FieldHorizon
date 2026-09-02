# Field Horizon // Command Interface

Tauri 2 + React + TypeScript + Vite. One frontend, two delivery targets:

* **Desktop** — `npm run tauri dev` / `npm run tauri build`, packaged with a
  managed or attached local backend (`src-tauri/src/backend.rs`, FABLE §8.4).
* **Web** — `npm run build` output served by the existing FastAPI server at
  `/ui` (see `../../fieldhorizon/server.py`'s static mount, Phase UI-3 item 2).

Both targets share the exact same source tree; `src/platform/` picks the
right implementation of file dialogs / managed-backend controls at runtime
(`isTauri()`), and is the only place that branches on which shell it's
running in — see `src/platform/types.ts`.

## Recommended IDE setup

- [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)

## Commands

```bash
npm install       # frontend deps
npm run dev       # Vite dev server only (browser target, no Tauri)
npm run build     # tsc + vite build -> dist/ (used by both targets)
npm run tauri dev # Tauri dev shell (spawns the Vite dev server itself)
npm run tauri build
```

Repo-root scripts (`./scripts/gui-*`, `./scripts/field-horizon-ui`) wrap these
for one-command dev/build/test — see Phase UI-3 item 2.

## E2E tests

`npm test` (vitest) is fast and hermetic. `npm run test:e2e` (Playwright,
`e2e/full-workflow.spec.ts`) is neither: it drives the real FastAPI backend
against whatever corpus is already ingested on this machine, and genuinely
launches a multi-agent cycle through the real local model provider — there
is no mock mode, and it is not run in CI. Prerequisites:

```bash
field-horizon serve --token-file .fh_token   # from the repo root, left running
npm run test:e2e                             # from apps/desktop/ -- starts its own dev server
```

`FH_TOKEN_FILE` overrides the token file path if you're not using the
repo-root default.

## Security

See `../../docs/gui/SECURITY_MODEL.md` for the full threat model: minimal
Tauri capabilities, no shell/fs/http plugins, argument-validated managed-backend
commands, loopback-only backend binding.
