# The shelf

`/shelf` is read-only and is not served. Copy what you use into `/work/vendor/`
and reference it with relative paths — the app has to be self-contained when
the house serves it. Copy the matching `LICENSE` alongside; these licences
require the notice to travel with the file. Nothing here is fetched at run
time, and there is nothing else: no CDN, no npm, no network.

| slot | copy | into |
|---|---|---|
| game | `/shelf/game/kaplay.js`, `/shelf/game/LICENSE` | `vendor/` |
| phaser | `/shelf/phaser/phaser.min.js`, `/shelf/phaser/LICENSE-phaser`, `/shelf/phaser/phaser-boot/` | `vendor/` |
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

**phaser — Phaser 3.90.0, 1.2 MB, MIT.** The larger 2D engine: scenes,
arcade and matter physics, tilemaps, tweens, particles. Script tag, global
`Phaser`. This is Phaser 3, the last 3.x release; Phaser 4 APIs are not here.

The house CSP blocks `data:` and `blob:` images, and Phaser uses both by
default, so **two config keys are not optional**. Without `images` the game
dies at boot; without `loader` no image ever loads.

```html
<div id="game"></div>
<script src="vendor/phaser.min.js"></script>
<script src="app.js"></script>
```

```js
new Phaser.Game({
  images: {
    default: 'vendor/phaser-boot/default.png',
    missing: 'vendor/phaser-boot/missing.png',
    white: 'vendor/phaser-boot/white.png',
  },
  loader: { imageLoadType: 'HTMLImageElement' },
  parent: 'game', width: 800, height: 600, scene: MyScene,
});
```

For the same reason `load.svg`, `load.htmlTexture` and `textures.addBase64` do
not work here. Load PNG files, or draw with `this.add.graphics()`.

**typescript — esbuild 0.28.2 and tsc 7.0.2, commands in the image, nothing to
copy.** `tsc` type-checks and writes nothing; `esbuild` strips the types and
writes the one file the page loads. Run both from `/work`, and run them again
after every change to `src/`:

```sh
tsc --noEmit src/main.ts
esbuild src/main.ts --bundle --outfile=app.js
```

With Phaser, use the global `Phaser` from the script tag (do not `import` it;
esbuild has nothing to resolve it to) and hand its types to the checker. The
types stay on the shelf: they are 6 MB and no browser reads them.

```sh
tsc --noEmit --skipLibCheck /shelf/phaser/types/phaser.d.ts src/main.ts
```

This is tsc 7: `strict` is on unless you turn it off, and options removed
after TypeScript 5 (`--moduleResolution node`, `--target es5`, `--outFile`)
are errors. Write no `tsconfig.json`: tsc refuses file
arguments beside one (TS5112).

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

**not on the shelf:** anything else. No 3D, no video, no framework, no package
manager, and no build step but the two typescript commands above. If a job
seems to need one, write it plainly instead and say so in your closing line.
