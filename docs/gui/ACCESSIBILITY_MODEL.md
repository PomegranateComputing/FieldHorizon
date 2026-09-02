# Accessibility Model (Phase UI-6 item 3)

FABLE §16's requirements, audited against the real app before anything was
changed: keyboard navigation, visible focus, sufficient contrast,
`prefers-reduced-motion`, and status never conveyed by color alone.

## What the audit found already in place

* **prefers-reduced-motion**: `styles/motion.css`'s existing `@media
  (prefers-reduced-motion: reduce)` block already zeroes every
  `animation-duration`/`transition-duration` app-wide and resolves `.reveal`
  to its settled state. The app's entire motion surface is two `@keyframes`
  (a liveness pulse, a one-shot contradiction flash) plus one CSS
  transition — all already covered. No changes made here.
* **Keyboard navigation, structurally**: every interactive element audited
  across `pages/*.tsx` and `shell/*.tsx` is a native `<button>`, `<a>`, or
  `NavLink` — there is no `<div onClick>` pattern anywhere in the tree, so
  nothing was silently unreachable by keyboard by construction.
* **React Flow graph nodes** (CanonPage, SemanticsPage): neither screen sets
  `disableKeyboardA11y` or turns off `nodesFocusable`/`elementsSelectable`,
  so `@xyflow/react`'s own defaults apply — nodes render with `tabIndex={0}`
  and `role="group"`, and its internal `onKeyDown` handler calls the same
  node-click path for Enter/Space as a mouse click does. Verified by reading
  the installed `@xyflow/react@12.11.2` source directly
  (`NodeWrapper`/`elementSelectionKeys` in `dist/esm/index.mjs`), not
  assumed from documentation.
* **Non-color status encoding**: audited TopBar's connection dot,
  COUNCILS' examined/overturned counts, CONTRADICTIONS' capability banner,
  and CANON's verdict transitions — every one already pairs color with a
  real text label (e.g. TopBar's dot sits next to the literal `"ok"` /
  `"degraded"` / `"failed"` string, never color alone). No genuine
  color-only violation was found; no changes made here.

## What was a genuine gap, and the fix

* **No focus-visible styling anywhere.** `grep`-ing every `.css` file found
  zero `:focus`/`:focus-visible` rules — and also zero `outline: none`
  resets, so focus was never actively suppressed, just never styled,
  leaving it at an inconsistent browser default against a near-black
  theme. `styles/surfaces.css` now has one global rule:
  `:focus-visible { outline: 2px solid var(--gold); outline-offset: 2px; }`
  — `:focus-visible` specifically (not `:focus`) so a mouse click never
  draws a ring a pointer user didn't ask for.
* **Two text colors failed WCAG AA contrast.** Measured relative-luminance
  ratios against `--ink` (#070706): `--ash` (`--text-meta`, used as real
  body text in ~30 places) was 4.18:1; `--pomegranate-bright`
  (`--status-danger`, used as real body text in ~19 places, e.g.
  CONTRADICTIONS' reason paragraph and TopBar's failed-status text) was
  3.47:1 — both short of AA's 4.5:1 for normal-size text. Both are the
  site's literal verbatim brand values (`docs/gui/DESIGN_SYSTEM.md`), but
  unlike `--pomegranate` ("identity, used sparingly") these two are
  workhorse UI text colors, not a rare decorative brand mark — so the
  fix was to nudge lightness while holding hue constant: `--ash` →
  `#7f7971` (4.68:1), `--pomegranate-bright` → `#df3063` (4.55:1).
  `DESIGN_SYSTEM.md` keeps the original sourced values as a historical
  record with a note pointing here.
* **DreamsPage's `ApplyConfirmDialog` had no dialog semantics.** It renders
  its own backdrop/panel (no `<dialog>` element, no modal library) with no
  `role="dialog"`, no `aria-modal`, no Escape-to-close, no initial focus,
  no focus trap, and no focus return to the button that opened it. Fixed
  directly in `DreamsPage.tsx`: the dialog container now has `role="dialog"
  aria-modal="true" aria-labelledby="dreams-apply-dialog-title"`; a mount
  effect focuses the first button; a hand-rolled `trapTabKey` wraps Tab/
  Shift+Tab between the dialog's own focusable elements (no library needed
  for two buttons); Escape calls the same cancel/close path a click would,
  except mid-request ("applying") where there's nothing to cancel; and
  `ProposalCard` now stores a ref to its own Apply button and returns focus
  there when the dialog closes either way. `platform.confirm()` (used by
  DREAMS' reject flow) needed none of this — it's the OS-native dialog on
  desktop and `window.confirm` on web, both already fully accessible by
  construction.

## Verification

* `DreamsPage.test.tsx` (new): renders the real page with mocked API
  responses, opens the dialog, asserts `role="dialog"`/`aria-modal="true"`,
  asserts focus lands inside the dialog on open, presses Escape, and
  asserts the dialog unmounts and focus returns to the Apply button.
* Live Playwright pass against a disposable scratch install (own config,
  own DB, one throwaway proposal, deleted after): confirmed in a real
  browser rather than jsdom —
  - `Tab` twice from page load lands on a real focusable element with a
    computed `outline: rgb(182, 149, 91) solid 2px` (the `--gold` token) —
    the ring genuinely renders, not just declared in CSS.
  - Focusing the Apply button and pressing `Enter` (not a mouse click)
    opens the dialog — confirms native button semantics carry the keyboard
    path with no extra work.
  - `Tab` inside the open dialog cycles Cancel → Confirm → (wraps) Cancel —
    the trap holds focus inside with only two elements.
  - `Escape` closes the dialog and moves focus back to the literal Apply
    button that opened it.

## Known gaps

* No automated contrast-ratio regression test exists (the ratios above were
  computed by hand, once, for this audit) — a future palette change could
  silently regress below 4.5:1 with nothing catching it.
* No `axe-core`/`jest-axe` integration exists in the test suite; accessibility
  verification here is manual audit + targeted behavioral tests, not a
  blanket automated scan.
