# Kokua brand assets

The Kokua mark: a stem and a chevron, set apart by a gap, forming a **K**. The
stem is structure — the core you don't edit. The chevron is what reaches out
from it: the plugins you install and the skills Kokua writes for itself. The gap
between them is the same interval AIMU sets between its bricks, which is what
makes the two marks read as siblings rather than twins.

The palette is AIMU's, with the roles inverted: AIMU builds up from a teal base
to an indigo apex, while Kokua stands on indigo structure and reaches out in
teal.

## Palette

| Role                | Hex       | Notes                          |
|---------------------|-----------|--------------------------------|
| Indigo (stem)       | `#4f46e5` | AIMU's apex indigo             |
| Teal (chevron)      | `#14b8a6` | AIMU's base teal               |
| Ink (text, tiles)   | `#0f172a` |                                |
| Muted (tagline)     | `#475569` |                                |

Dark surfaces lighten to `#818cf8` (stem), `#2dd4bf` (chevron), `#f8fafc`
(wordmark), `#94a3b8` (tagline).

Contrast: indigo holds 6.3:1 on white, teal 9.6:1 on the ink tile, and the two
shapes separate at 2.5:1 — so the mark survives greyscale, the mono cut, and
red-green colour blindness, where an equal-luminance pairing would collapse.

## Files

| File | Canvas | Use |
|------|--------|-----|
| `kokua-mark.svg` | 90×104 | mark only, full colour |
| `kokua-mark-mono.svg` | 90×104 | single ink via `currentColor` |
| `kokua-mark.png` | 360×416 | raster mark |
| `kokua-horizontal-light.svg` / `-dark.svg` | 325.35×62 | README headers, docs nav |
| `kokua-horizontal-light.png` / `-dark.png` | 976×186 | raster headers |
| `kokua-stacked-light.svg` | 253.69×156.62 | vertical lockup |
| `kokua-favicon.svg` | 128×128 | browser tab, GitHub avatar |
| `kokua-favicon-{16,32,48,180,512}.png` | — | favicon set + Apple touch icon |

Wordmark and tagline are Inter, converted to outlines — no font dependency, no
webfont load, no substitution on a machine that lacks Inter. Every viewBox is
measured to the ink: margins are 0 on all four sides, so alignment is whatever
the surrounding layout says it is.

## README usage (auto light/dark)

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/kokua-horizontal-dark.svg">
  <img alt="Kokua — a personal AI assistant that extends itself" src="docs/assets/kokua-horizontal-light.svg" width="360">
</picture>
```

## The mono mark

`kokua-mark-mono.svg` inherits the CSS `color` property, so it takes the ink of
whatever surrounds it — one-colour print, an embossed or stamped context, or
matching adjacent text:

```html
<span style="color: #4f46e5">
  <img src="docs/assets/kokua-mark-mono.svg" width="24" alt="">
</span>
```
