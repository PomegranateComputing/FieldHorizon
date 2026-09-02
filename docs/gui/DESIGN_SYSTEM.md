# Design System (finalized — Phase UI-3 item 3)

**Source found.** `~/Projects/PomegranateInteractive_Site` — git remote `github.com/PomegranateComputing/pomegranate-interactive-site.git`, a static site built by `source/build_site.py` into plain HTML/CSS (no framework, no build-tool lock-in to worry about). It already has a dedicated page for this project: `public/field-horizon.html` ("FH-01", tagged `Local web UI` among its tech tags — the site *already anticipates this exact work*). This draft extracts real tokens from `public/assets/css/styles.css` (545 lines) and the studio's own internal `CONTENT_GUIDE.md`. FABLE §6's fallback direction was **not needed** — everything below is sourced, not invented.

## Tokens (from `styles.css:1-21`)

```css
--ink: #070706;                 /* obsidienne — primary background */
--ink-soft: #0e0d0d;            /* noir minéral — slightly raised background */
--panel: rgba(19, 17, 17, 0.82);       /* anthracite — panels, translucent */
--panel-strong: #151212;        /* anthracite, opaque */
--bone: #e8e1d2;                /* ivoire d'archive — primary text */
--bone-dim: #bcb4a6;            /* cendre — secondary text */
--ash: #77716a;                 /* tertiary text / meta */
--pomegranate: #8f0d2f;         /* grenat — identity, used sparingly */
--pomegranate-bright: #c31e4e;  /* rouge grenade — selection, danger, contradiction */
--rust: #8d4734;                /* secondary accent, warm */
--terminal: #91f5a8;            /* vert terminal — activity, liveness, success */
--terminal-dim: #4aa061;
--gold: #b6955b;                /* ambre sombre — warning, experimental */
--line: rgba(232, 225, 210, 0.13);        /* hairline borders */
--line-strong: rgba(232, 225, 210, 0.24);
--shadow: 0 24px 80px rgba(0, 0, 0, 0.48);
```

This maps onto FABLE §6.1's palette families exactly, field for field — Obsidienne→`--ink`, Grenat→`--pomegranate`, Vert terminal→`--terminal`, Ambre sombre→`--gold`, etc. **No translation needed; use these values directly.**

## Typography (from `styles.css`, confirmed against FABLE §6.2's two-register split)

```css
--serif: Georgia, 'Times New Roman', serif;   /* literary register: titles, manifestos, long synthesis */
--sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;  /* UI chrome */
--mono: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;  /* machine register: ids, logs, scores, timestamps */
```

Real usage patterns worth carrying over directly:
- Display sizes are large and use `clamp()` (e.g. hero `h1`: `clamp(4rem, 9vw, 8.8rem)`, section `h2`: `clamp(2.7rem, 6vw, 5.7rem)`) with tight `line-height` (~0.95–1) and negative `letter-spacing` (~-0.035em to -0.065em) — literary register reads as monumental, not default-browser.
- Mono labels (kickers, tags, status lines, meta) are consistently **uppercase, wide letter-spacing (0.07–0.16em), small size (0.64–0.76rem)** — this is the recurring "machine register" texture across the whole site, and should be the exact recipe for the Command Interface's own labels/badges/timestamps.
- `.big-quote`, `.about-copy .lead` use serif at large sizes for pull-quotes — maps directly to FABLE §6.2's "citations."

## Shape and texture (from `styles.css`)

- Radii: **near-zero on rectangular surfaces**; the only `border-radius` uses in the whole file are on circles (`50%` — status dots, orbit rings, avatar-style nodes). This matches FABLE §6.3 ("angles francs, rayons faibles") precisely — carry the *absence* of rounded rectangles forward as a hard rule, not just "small radius."
- Borders: hairline (`1px solid var(--line)` / `var(--line-strong)`), never heavy.
- Background texture: a fractal-noise SVG data-URI overlay at `opacity: .035` (`body::before`), a faint scanline gradient at `mix-blend-mode: screen` (`.scanline`), a barely-visible 48px grid (`.grid-overlay`, opacity ~0.022) masked to fade toward the bottom, and radial "ember" gradients in `--pomegranate`/`--rust` behind the body. All are near-invisible individually and additive together — exactly FABLE §6.4's "aucun effet ne doit nuire à la lisibilité."
- Live-status affordance: a small `--terminal` dot with a `pulse` keyframe animation (`box-shadow` ring expanding, 2s loop) — the site's own existing idiom for "this is alive," directly reusable for the app shell's connection indicator and the console's "active" markers.

## Motion (from `styles.css` + `main.js`)

- Section reveal: `IntersectionObserver` adds `.is-visible` to `.reveal` elements as they scroll into view (progressive fade/rise, degrades to instantly-visible if `IntersectionObserver` is unsupported) — matches FABLE §6.5's "apparition progressive."
- `@media (prefers-reduced-motion: reduce)` is already respected in the site's own CSS (`styles.css:542`) — carry this discipline into the app exactly, per FABLE §16 and this brief's own accessibility rule.
- No particle effects, no glitch loops, no forced boot sequence beyond a `.reading-progress` bar — consistent with FABLE §6.5's prohibitions.

## Voice and positioning (from `CONTENT_GUIDE.md`)

The studio's own internal content guide states the **exact same reality-first discipline** this brief and FABLE both independently insist on: *"Toujours distinguer : prototype actif ; recherche en cours ; architecture conçue ; concept à long terme ; œuvre achevée. Ne jamais présenter une architecture projetée comme un produit déjà fonctionnel."* This is not a coincidence to reconcile — it's the same standard already governing how Pomegranate Interactive talks about all its projects publicly, including Field Horizon's own page (which already lists its status as "Active prototype," code `FH-01`). The Command Interface should treat this as inherited house style, not an external constraint bolted on.

Field Horizon is already described on the site as: *"a machine for turning fragments into worlds, while preserving provenance, contradiction, rejection and memory"* — vocabulary (provenance, contradiction, rejection, memory) that lines up with this engine's real vocabulary (CANON/HERESY/USEFUL_FRAGMENT/NOISE, provenance edges, council rejection). Reuse this framing in the app's own about/startup copy rather than inventing new marketing language.

## Files (written, Phase UI-3 item 3)

```
apps/desktop/src/styles/tokens.css       — the CSS custom properties above, verbatim palette + fonts,
                                            plus a semantic alias layer (--status-live/--status-danger/
                                            --status-warning/--accent-identity) components reach for
                                            instead of the raw palette names
apps/desktop/src/styles/typography.css   — the two-register scale (.type-literary/.type-mono), mono-label
                                            recipe (.type-label), heading defaults
apps/desktop/src/styles/surfaces.css     — .panel/.panel-strong, hairline dividers, the live-status dot,
                                            opt-in .texture-grain/.texture-scanline/.texture-grid utilities
apps/desktop/src/styles/motion.css       — .reveal, the pulse-dot keyframes, a one-shot contradiction
                                            flash, and the global prefers-reduced-motion guard
apps/desktop/src/styles/index.css        — imports the four in order; the one stylesheet main.tsx loads
```

Verified visually (not just by reading the CSS): started the Vite dev server, loaded it in a real headless
Chromium, and read back computed styles — `body` background `rgb(7, 7, 6)` (`--ink`), text color
`rgb(232, 225, 210)` (`--bone`), heading font resolving to the Georgia/serif stack, inline `<code>`
resolving to the monospace stack. Screenshot matched: dark ground, serif "FIELD HORIZON" heading, ivory
body text, monospace treatment on technical inline text. No console errors.

## No light theme (deliberate)

FABLE §6 commits to one visual world — control room, archive, scriptorium — the same way a real physical
control room doesn't get a daytime edition. `tokens.css` does not redefine anything under
`prefers-color-scheme: light`; the palette holds regardless of OS theme. This mirrors this project's own
design-fundamentals guidance ("a design that deliberately commits to one visual world... may stay
single-theme — make it a choice, not an omission") — it's recorded as one here.

## Amendment (Phase UI-6 item 3): two text colors lightened for WCAG AA

`--ash` and `--pomegranate-bright` above are the site's literal verbatim
values, but this app uses both as real body-sized text everywhere (`--text-
meta` and `--status-danger`, ~30 and ~19 usages respectively) — not the
sparing decorative role `--pomegranate` ("identity, used sparingly") has.
Measured against `--ink`: `--ash` was 4.18:1 and `--pomegranate-bright` was
3.47:1, both short of WCAG AA's 4.5:1 for normal text. `tokens.css` now
carries `#7f7971` and `#df3063` — same hue, lightness nudged just enough to
clear 4.5:1 (4.68:1 / 4.55:1) — see `docs/gui/ACCESSIBILITY_MODEL.md` for
the full contrast audit. The tokens above are left as the historical record
of what was actually sourced from the site; `tokens.css` is the live values.

## Resolved item: font loading has no CDN dependency to fix

Checked `public/index.html` for a Google Fonts `<link>` or any `@font-face`: **none exists.** `Inter` is referenced only as the first name in the `--sans` stack — the site never fetches it over the network; it renders with `Inter` if the visiting OS happens to have it installed, and falls through to `ui-sans-serif, system-ui, -apple-system, ...` otherwise. This already satisfies FABLE §6.2's "fallbacks entièrement locaux" by construction. Nothing to resolve before Phase UI-3 — reuse the exact same stack verbatim, no self-hosting needed.
