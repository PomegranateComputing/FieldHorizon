# Packaging (Phase UI-7)

Linux first (FABLE §22). See `docs/gui/PACKAGING_MODEL.md` for the full
narrative — why the bundle targets were narrowed, the exact live
verification performed, and the reasoning behind the external-backend
architecture. This doc is the concise "how to build it, what you get."

## Building

```bash
cd apps/desktop
npm run build          # produces dist/ -- required before tauri build
npx tauri build         # produces the artifacts below
```

Requires (Linux): `webkit2gtk-4.1`, `javascriptcoregtk-4.1`, `gtk+-3.0`,
`libsoup-3.0` (via `pkg-config`), `dpkg-deb` for the `.deb`, and network
access the first time an AppImage is built (the Tauri bundler downloads
`linuxdeploy` and its plugins on demand). No `ayatana-appindicator3` is
needed — this app has no system tray icon.

## What gets produced

`apps/desktop/src-tauri/target/release/bundle/` (gitignored — rebuild
locally, never committed):

* **`deb/Field Horizon_0.1.0_amd64.deb`** (~3.4 MB) — the Tauri binary, a
  `.desktop` entry, and three icon sizes. Declares real runtime
  dependencies (`libwebkit2gtk-4.1-0`, `libgtk-3-0`) resolved against
  whatever the installing system already has.
* **`appimage/Field Horizon_0.1.0_amd64.AppImage`** (~83 MB) — the same
  binary plus a bundled copy of its GTK/WebKit/GStreamer runtime, so it
  runs on a system that doesn't have those libraries installed.

## What is never bundled

Python, Ollama, any model weights, the `fieldhorizon` package, or a
database. The backend is managed as an **external** process — the
built app spawns a real Python + Field Horizon checkout the user already
has (`src-tauri/src/backend.rs`'s `launch_backend`), never a compiled-in
sidecar. This is a deliberate architectural choice (FABLE offers "sidecar
or external" as an either/or), not a gap — see `PACKAGING_MODEL.md` for
why turning the Python backend into an actual frozen sidecar binary would
be a materially larger, different undertaking.

## Installing

```bash
# .deb (Debian/Ubuntu-family)
sudo apt install ./"Field Horizon_0.1.0_amd64.deb"

# AppImage (any modern Linux, no installation step)
chmod +x "Field Horizon_0.1.0_amd64.AppImage"
./"Field Horizon_0.1.0_amd64.AppImage"
```

Either way, the app itself still needs a Field Horizon backend to talk to
— see `docs/gui/RUNBOOK.md`'s "Connect an existing backend" section; the
built app's own startup screen walks through launching or attaching to
one.

## Windows

Not attempted in this environment — no Windows Rust target and no
MinGW/cross-compilation toolchain available. See `PACKAGING_MODEL.md` for
exactly what was checked. Left for a Windows-equipped (or properly
cross-compilation-equipped) environment.
