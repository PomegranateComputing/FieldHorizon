# Design system

The FieldHorizon desktop interface uses a dark archival palette, serif headings,
monospace technical information, and restrained texture. The implementation is
in `apps/desktop/src/styles/`; the interface requirements are recorded in
[COMMAND_INTERFACE_SPEC.md](COMMAND_INTERFACE_SPEC.md), section 6.

## Palette and semantic tokens

`tokens.css` defines the palette and its semantic aliases. Components should use
`--surface-*`, `--text-*`, `--accent-*`, and `--status-*` rather than assigning
meaning directly to a raw color.

| Token | Current value | Role |
| --- | --- | --- |
| `--ink` | `#070706` | Base background |
| `--ink-soft` | `#0e0d0d` | Raised background |
| `--panel` | `rgba(19, 17, 17, 0.82)` | Translucent panel |
| `--panel-strong` | `#151212` | Opaque panel |
| `--bone` | `#e8e1d2` | Primary text |
| `--bone-dim` | `#bcb4a6` | Secondary text |
| `--ash` | `#7f7971` | Metadata text |
| `--pomegranate` | `#8f0d2f` | Identity accent, used sparingly |
| `--pomegranate-bright` | `#df3063` | Danger, contradiction, negative verdict |
| `--rust` | `#8d4734` | Secondary warm accent |
| `--terminal` | `#91f5a8` | Activity, connection, validation |
| `--terminal-dim` | `#4aa061` | Muted activity indicator |
| `--gold` | `#b6955b` | Warning and experimental features |
| `--line` | `rgba(232, 225, 210, 0.13)` | Hairline border |
| `--line-strong` | `rgba(232, 225, 210, 0.24)` | Stronger hairline border |

The panel shadow is `0 24px 80px rgba(0, 0, 0, 0.48)`. The shared rectangular
radius is zero and the border width is one pixel.

The Phase UI-6 contrast audit changed `--ash` from `#77716a` to `#7f7971` and
`--pomegranate-bright` from `#c31e4e` to `#df3063`. These colors are used for
body-sized text through `--text-meta` and `--status-danger`. The recorded ratios
against `--ink` are 4.68:1 and 4.55:1 respectively; see
[ACCESSIBILITY_MODEL.md](ACCESSIBILITY_MODEL.md) for the audit. The CSS file is
the authority for current values.

## Typography

The three font stacks are local fallback chains:

```css
--font-literary: Georgia, "Times New Roman", serif;
--font-machine: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
--font-ui: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
```

The literary register covers headings, quotations, and long syntheses. The
machine register covers identifiers, logs, scores, paths, events, and timestamps.
Navigation, buttons, and ordinary interface text use the sans-serif stack.
`Inter` is a stack entry, not a network font import.

`typography.css` implements the following scale:

- `h1`: `clamp(2.4rem, 5vw, 4.2rem)`, line height `0.95`, letter spacing `-0.05em`.
- `h2`: `clamp(1.6rem, 3vw, 2.4rem)`.
- `h3`: `clamp(1.2rem, 2vw, 1.6rem)`.
- Shared literary headings: weight `500`, line height `1`, letter spacing `-0.035em`.
- `.type-label`: monospace, uppercase, `0.7rem`, letter spacing `0.1em`.
- Body: the UI font stack, primary text color, line height `1.5`.

## Surfaces, texture, and focus

`surfaces.css` provides `.panel`, `.panel-strong`, `.panel-shadow`, dividers, and
a seven-pixel circular status dot. Rectangular panels use the shared zero-radius
token. Keyboard focus uses a two-pixel gold outline with a two-pixel offset.

Texture is opt-in through utility classes:

- `.texture-grain`: fractal-noise SVG overlay, opacity `0.035`.
- `.texture-scanline`: a five-pixel repeating scanline with screen blending.
- `.texture-grid`: a 48-pixel grid fading toward the bottom.

Texture overlays ignore pointer events and carry no information. They must not
compete with text or status indicators.

## Motion

`motion.css` provides a progressive reveal, an active-status pulse, and a single
contradiction flash. `.reveal.is-visible` restores opacity and removes the
24-pixel vertical offset; the transition lasts `0.7s`. The active-status pulse
runs over two seconds, and `.flash-contradiction` runs once over `1.2s`.

Under `prefers-reduced-motion: reduce`, animations and transitions are reduced
to effectively instantaneous changes, scrolling becomes immediate, and reveal
elements remain visible without translation. Motion must explain state changes
without blocking interaction.

## Stylesheet organization

| File under `apps/desktop/src/styles/` | Responsibility |
| --- | --- |
| `tokens.css` | Palette, font stacks, shape, semantic aliases |
| `typography.css` | Literary and machine registers, heading scale, labels |
| `surfaces.css` | Panels, borders, focus, status dots, texture utilities |
| `motion.css` | Reveal, pulse, contradiction flash, reduced-motion guard |
| `index.css` | Imports the four modules |

## Theme

The interface uses a single dark theme. The palette remains constant when the
operating system requests a light color scheme. This follows the control-room,
archive, and scriptorium direction in the interface specification.

## Recorded implementation verification

The Phase UI-3 implementation report records a Vite session checked with headless
Chromium. Computed styles showed background `rgb(7, 7, 6)`, primary text
`rgb(232, 225, 210)`, Georgia/serif headings, and the monospace stack for inline
code, with no console errors. This is a historical implementation check; changes
to the styles should be verified against the current desktop build.
