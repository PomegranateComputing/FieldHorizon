# Packaging Model (Phase UI-6 item 7)

FABLE §22: Linux first (AppImage, .deb), document exactly what each
artifact contains, backend as managed sidecar or external, no models
bundled. Windows next if the toolchain permits; document if not.

## What was built

Real Linux artifacts via the Tauri bundler (`npx tauri build`), not a dry
run: `apps/desktop/src-tauri/tauri.conf.json`'s `bundle.targets` was
narrowed from `"all"` to `["deb", "appimage"]` — this machine has no
`rpmbuild`, and FABLE's own scope for this item is explicitly "Linux
first: AppImage and .deb," not every bundle format Tauri happens to
support.

* `src-tauri/target/release/bundle/deb/Field Horizon_0.1.0_amd64.deb`
  (3.4 MB)
* `src-tauri/target/release/bundle/appimage/Field Horizon_0.1.0_amd64.AppImage`
  (83 MB)

Both are gitignored build output (`target/`), not committed — they're
reproducible from `npx tauri build` in `apps/desktop/`.

## Exactly what each artifact contains

**`.deb`** (`dpkg-deb -c`): the Tauri binary itself (`usr/bin/desktop`,
10.2 MB), a `.desktop` entry, and three icon sizes under
`usr/share/icons/hicolor/`. Nothing else. Its control metadata
(`dpkg-deb -I`) declares real runtime dependencies —
`libwebkit2gtk-4.1-0, libgtk-3-0` — resolved against whatever the target
system already has installed; no bundled copies of those libraries.

**`.AppImage`**: the same Tauri binary plus a self-contained copy of the
GTK/WebKit/GStreamer/etc. runtime it links against (~255 MB unpacked,
compressed to 83 MB) — that's the entire point of AppImage: run on a
system that doesn't have those libraries installed, at the cost of
carrying them along. Verified by walking the actual `AppDir` file list
after building, not assumed from Tauri's documentation.

**Neither artifact contains**: Python, any Ollama/model files, the
`fieldhorizon` package, or a database. This isn't an oversight to fix —
it's the existing, deliberate architecture from Phase UI-6 item 1: the
backend is managed as an **external** process (`backend.rs`'s
`launch_backend` takes `python_bin`/`repo_root`/`port`/`token_file` and
spawns whatever real Python installation and Field Horizon checkout the
user already has), never a compiled-in sidecar binary. FABLE offers
"sidecar or external" as an either/or choice; turning the Python backend
(embeddings, sqlite-vec, the whole retrieval/synthesis pipeline) into an
actual Tauri sidecar would mean freezing that entire dependency tree into
a standalone executable — a materially different, much larger undertaking
than this item, and one the existing security/resilience hardening
(Phase UI-6 items 1 and 4) was already built around the external-process
model, not a bundled one. Kept as-is; documented here rather than
silently reinterpreted.

## Live verification

* `dpkg-deb -c`/`-I` on the real `.deb` (not assumed from the bundler's
  log output) — contents and dependency list above are read directly off
  the built artifact.
* The AppImage was actually launched, not just inspected: this sandbox has
  no FUSE (`Cannot mount AppImage, please check your FUSE setup` on a
  plain invocation), so it was run with `--appimage-extract-and-run`
  under `xvfb-run` (a real, if headless, X11 server) instead. It stayed
  up for the full observation window with no crash — only the expected
  `libEGL`/DRI3 software-rendering warnings a GPU-less container always
  produces. `ldd` against the extracted binary confirmed zero unresolved
  shared library dependencies.

## Windows

Not attempted. This build environment has no Windows Rust target
installed (`rustup target list --installed` shows none) and no
MinGW/cross-compilation toolchain (`x86_64-w64-mingw32-gcc` absent) --
producing an NSIS/WiX Windows bundle from here would not be a real build,
just a config change with nothing to verify. Left for a Windows (or
properly cross-compilation-equipped) environment, per FABLE's own
"document if not."
