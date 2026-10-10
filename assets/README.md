# assets/

One folder per profile: `assets/<profile>/` holds that game's template images
(PNGs) — the things Ultibot looks for on screen.

## Snipping templates

Use Screenshotter (it's in this repo) pointed at the profile's folder:

```
pythonw Screenshotter\screenshotter.py --dir "assets\<profile>"
```

F12 → drag a marquee over the thing (button, icon, health bar frame…) → the
PNG lands here, ready to reference from rules as `"image": "<file>.png"`.

## Tips for robust templates (PLAN §3)

- Snip tight: the icon, not its background. Less background = fewer false
  positives.
- PNGs with transparency use the transparent part as a **mask** — only the
  solid pixels are matched.
- Variants (hover states, themes) are welcome: save each and list them in the
  rule's `variants`.

## Try one

```
.venv\Scripts\python -m ultibot find <file>.png --profile <profile>
```
