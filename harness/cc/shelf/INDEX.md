# The shelf

`/shelf` is read-only and is not served. Copy what you use into `/work/vendor/`
and reference it with relative paths — the app has to be self-contained when
the house serves it. Copy the matching `LICENSE` alongside; these licences
require the notice to travel with the file. Nothing here is fetched at run
time, and there is nothing else: no CDN, no npm, no network.

| slot | copy | into |
|---|---|---|
| game | `/shelf/game/kaplay.js`, `/shelf/game/LICENSE` | `vendor/` |
| chart | `/shelf/chart/chart.umd.min.js`, `/shelf/chart/LICENSE` | `vendor/` |
| reset | `/shelf/css/modern-normalize.css`, `/shelf/css/LICENSE` | `vendor/` |
| fonts | `/shelf/fonts/` — the whole directory | `vendor/fonts/` |
| icons | `/shelf/icons/sprite.svg`, `/shelf/icons/LICENSE` | `vendor/` |
| store | `/shelf/store/house-store.js` or `.mjs` | `vendor/` |

**game — kaplay 3001.0.19, 189 KB, MIT.** One script tag, no bundler and no
modules. `kaplay()` makes its own full-window canvas and puts the whole API on
`window`, so `add`, `sprite`, `onKeyDown` and friends are just there.

```html
<script src="vendor/kaplay.js"></script>
<script>
  kaplay({ background: '#6d80fa' });
  add([text('hi'), pos(80, 40)]);
</script>
```

**chart — Chart.js 4.5.1 UMD, 208 KB, MIT.** Script tag, global `Chart`, all
controllers registered.

```html
<script src="vendor/chart.umd.min.js"></script>
<script>new Chart(document.querySelector('canvas'), { type: 'bar', data: { labels: ['a'], datasets: [{ data: [1] }] } });</script>
```

**reset — modern-normalize 3.0.1, 3.3 KB, MIT.** First stylesheet in the head,
before your own; it has no opinions beyond sane defaults.

```html
<link rel="stylesheet" href="vendor/modern-normalize.css">
```

**fonts — Inter 400/500/600/700 and JetBrains Mono 400/700, latin, 150 KB
total, OFL-1.1.** One stylesheet for all six; keep the directory intact, the
`@font-face` urls are relative to `fonts.css`. Always leave a real fallback in
the stack — the latin subset does not cover every glyph.

```html
<link rel="stylesheet" href="vendor/fonts/fonts.css">
<style>body { font-family: 'Inter', system-ui, sans-serif } code { font-family: 'JetBrains Mono', monospace }</style>
```

**icons — lucide-static 1.41.0, 1807 symbols in one 500 KB file, ISC.** Symbol
ids are the plain icon name — `activity`, `arrow-left`, `circle-check`,
`trash` — with no prefix. The symbols carry no colour or stroke of their own,
so set them on the `<svg>`; `currentColor` makes an icon follow its text.

```html
<svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"
     stroke-linecap="round" stroke-linejoin="round"><use href="vendor/sprite.svg#activity"></use></svg>
```

If an icon comes out blank, the browser is refusing the cross-file reference:
paste the `<symbol>` elements you need into a hidden `<svg>` at the top of the
body and use `href="#activity"` instead. Never ship all 1807 that way.

**store — the house store, v1, house-written.** Per-user values in this
browser, namespaced to your app. `houseStore(appId)` gives
`get(scope, key)` / `set(scope, key, value)` / `del(scope, key)` /
`keys(scope)`; values are JSON, missing keys read `undefined`.
`scope` is `'user'` today. `scope: 'app'` — storage shared between everyone
using the app — throws `StoreScopeNotAvailable`: it arrives with server-side
storage, so do not design around it yet. Any other scope throws `RangeError`.
`STORE_VERSION` is `1`.

```html
<script src="vendor/house-store.js"></script>
<script>
  const store = houseStore(APP_ID);          // your 12-char app id
  store.set('user', 'best', 42);
  store.get('user', 'best');                 // 42
  store.keys('user');                        // ['best']
</script>
```

As a module instead: `import { houseStore } from './vendor/house-store.mjs';`
— the same code, exported rather than assigned to `window`.

**not on the shelf:** anything else. No 3D, no video, no framework, no build
step, no package manager. If a job seems to need one, write it plainly instead
and say so in your closing line.
