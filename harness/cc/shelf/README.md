# The apps-builder asset shelf

Everything an apps-builder turn is allowed to build with, vendored, hashed and
committed. `Containerfile.apps` copies this directory to `/shelf` in the image
and makes it read-only; the builder copies what it needs from `/shelf` into
`/work/vendor/` so the finished app is self-contained when the gate serves it.
Nothing here is fetched at turn time — the seat has no egress.

Spec: `SPECS/2026-09-06-apps-builder-seat.md` §F (shelf) and §G (store shim).

## The admission bar

**"An app is worse without it."** Not "an app might use it", not "it's popular",
not "it saves the builder ten minutes". Worse without it. That bar is why the
shelf is six packages and not sixty: one 2D game library, one chart library,
one CSS reset, two font families at the weights a UI actually uses, one icon
sprite. No 3D, no video, no framework, no build step — a turn writes static
files and the browser runs them.

Every entry costs image size, a licence to carry, and a version to keep an eye
on, forever. A package that only *might* help does not clear the bar.

## What is here

| path | what | licence |
|---|---|---|
| `game/kaplay.js` | kaplay 3001.0.19, IIFE, global `kaplay` | MIT |
| `chart/chart.umd.min.js` | chart.js 4.5.1, UMD, global `Chart` | MIT |
| `css/modern-normalize.css` | modern-normalize 3.0.1 | MIT |
| `fonts/*.woff2` + `fonts/fonts.css` | Inter 400/500/600/700, JetBrains Mono 400/700, latin | OFL-1.1 |
| `icons/sprite.svg` | lucide-static 1.41.0, 1807 symbols | ISC |
| `store/house-store.js`, `store/house-store.mjs` | the house store shim (§G) | house |
| `ponytail/` | ponytail instructions, pinned, prose only | MIT |
| `kenney/` | CC0 asset packs — not in this slice | CC0 |

`INDEX.md` is the short version, with one-line usage each; the builder brief
includes it verbatim, so it is the file a turn actually reads.

## Provenance

`MANIFEST.toml` has one entry per vendored third-party file: package, version,
tarball URL, tarball SHA-256, path inside the tarball, shelf path, file
SHA-256, licence. It is the only place versions and digests are written down.

House-written files (`fonts/fonts.css`, `store/`, `INDEX.md`, this README) are
not in the manifest — git is their provenance. `ponytail/` is pinned in
`PONYTAIL-PIN` instead, because it comes from a git checkout, not a registry.

    bash harness/cc/shelf/fetch.sh --verify     # re-hash the shelf, no network
    node --test harness/cc/shelf/store/test_store.mjs

Both are green on a clean checkout, and `harness/cc/tests/test_apps_image.py`
holds them there.

## Adding a package

1. Check it against the bar above. Say out loud what is *worse* without it.
2. Download the tarball, note its SHA-256 and the SHA-256 of each file you
   want, and add one `[[file]]` block per file to `MANIFEST.toml` — including
   the package's own licence file.
3. `bash harness/cc/shelf/fetch.sh` (add `SHELF_CACHE=<dir>` to use tarballs
   you already have). It downloads, checks the tarball digest, extracts *only*
   the listed members, and checks every file digest. Any mismatch stops it and
   nothing is written.
4. `bash harness/cc/shelf/fetch.sh --verify`, then commit the vendored bytes
   together with the manifest change.
5. Add a row to `INDEX.md` with one line of usage — a package the brief does
   not describe is a package no turn will find.

Removing one is the same in reverse: drop the block, `git rm` the files, drop
the `INDEX.md` row. `fetch.sh` never deletes; it only writes what the manifest
lists.

## Notes and sharp edges

- `game/kaplay.js` and `chart/chart.umd.min.js` end with a
  `//# sourceMappingURL=` comment and we do not ship the `.map` files.
  Devtools will log one 404 each. Harmless; editing the line out would break
  the digest that ties the file to the registry.
- `icons/sprite.svg` symbols carry no `stroke` presentation attributes, so the
  consuming `<svg>` must set `fill="none" stroke="currentColor"` itself — see
  `INDEX.md` for the exact snippet.
- The fonts are the latin subset only. A glyph outside it falls through to the
  next family in the CSS stack, so always give `font-family` a real fallback.
