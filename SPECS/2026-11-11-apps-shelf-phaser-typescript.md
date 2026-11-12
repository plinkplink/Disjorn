# Spec: the apps shelf gets Phaser, and the apps image gets a TypeScript toolchain

<!-- Governing plan: BUILD-LOOP.md. COMMS.md binds this file's prose. -->

## Request
- **Verbatim**: "apps builder shelf: add Phaser (pinned SHA in the shelf manifest) and a TypeScript toolchain (tsc or esbuild, vendored, no egress) to the apps image. Asked by plink in app session 18. Owner: plink. Until then app builds are plain JS, vanilla canvas or KAPLAY 3001.0.19."
- **Requester**: plink
- **Origin**: backlog item #51; asked in app session 18.

## Agreed UX
An app turn can build a Phaser game, and can write it in TypeScript. The
builder copies Phaser from `/shelf` like any other shelf entry, type-checks
with `tsc`, bundles with `esbuild`, and leaves a static app in `/work`. The
user sees a working game. Nothing is fetched at turn time; `npm` and `npx`
stay refused.

## What was pinned (2026-11-11, npm registry)

| what | version | where it lands | bytes | license |
|---|---|---|---|---|
| Phaser, minified UMD build | 3.90.0 | `/shelf/phaser/phaser.min.js` | 1 196 122 | MIT |
| Phaser type definitions | 3.90.0 | `/shelf/phaser/types/phaser.d.ts` | 6 211 016 | MIT |
| Matter types that file references | 3.90.0 | `/shelf/phaser/types/matter.d.ts` | 198 421 | MIT |
| Phaser licence text | 3.90.0 | `/shelf/phaser/LICENSE-phaser` | 1 120 | MIT |
| Phaser's three boot textures, as files | 3.90.0 | `/shelf/phaser/phaser-boot/{default,missing,white}.png` | 476 | MIT |
| esbuild, linux-x64 binary | 0.28.2 | `/usr/local/bin/esbuild` | 11 427 952 | MIT |
| esbuild licence text | 0.28.2 | `/usr/local/share/licenses/esbuild/LICENSE.md` | 1 069 | MIT |
| TypeScript, linux-x64 native package | 7.0.2 | `/usr/local/lib/typescript/`, `tsc` linked into `/usr/local/bin` | 27 945 990 | Apache-2.0, `LICENSE` and `NOTICE.txt` kept beside it |

Digests (SHA-256):

| file | digest |
|---|---|
| `phaser-3.90.0.tgz` | `c4e3a8da498ab94aa8859c7a32cb89140d94bfd95d769bee36a98cdef1bea6e6` |
| `phaser.min.js` | `e92ddef111ba42e92d316979c732311757093688ea1810591cb7aa2858eba7a7` |
| `phaser.d.ts` | `8a97fd70c0953b95a039c883413ef1e58e7fa3559b3ece02a36505b6cbe9cead` |
| `matter.d.ts` | `49315baaf695c2081834d7faa35f6d18a6bd2908d0ee7b6408db555c4b30b976` |
| `@esbuild/linux-x64` 0.28.2 tarball | `9573bb2233aab0f9ea7647d5cca9726113cc1768de61d66b17267f4db84488f6` |
| `esbuild` 0.28.2 tarball (licence only) | `e045f94c235c7adc50e77ba2a579c7bec41b496b6b47c3a7845f7c1e19959a88` |
| `@typescript/typescript-linux-x64` 7.0.2 tarball | `7ecad6f67377e831856367ab062ef394f21506a611405bf8ac0ff039348637d3` |
| the `esbuild` binary inside it | `e1698a3d5c6c0798fee4fd3b5cc816651f460c63d390a7a26ea4beb0b1884100` |
| the `tsc` binary inside it | `4f2de678286401759b3fb4475bafe35b8f32b4b3a07d92642bbf37eadc9b34a4` |

The Phaser digests are recorded in `harness/cc/shelf/MANIFEST.toml`; the three
tool tarball digests are `ARG`s in `harness/cc/Containerfile.apps`. Each
tarball was also checked against the registry's own SHA-512 integrity value.

Size: the repo grows by 7 607 155 bytes of working tree (about 1.27 MB as
compressed git objects). The image grows by about 47 MB: 7.6 MB of shelf and
39.4 MB of tools.

## Choices, and why

- **Phaser 3.90.0, not 4.2.1.** The registry's `latest` is 4.2.1 (4.0.0
  shipped 2026-04-10). 3.90.0 (2025-05-23) is the last 3.x release. The
  builder has no network and no documentation; it writes Phaser from what the
  model already knows, and that is Phaser 3. Phaser 4 is a new major; its API
  differences from 3 were not audited here. A 3.x that will never change is
  the lower-risk shelf entry today, and it will also never get a fix. Phaser 4
  can be added beside it later as its own slot; this is plink's call to
  revisit.
- **TypeScript 7.0.2, not 6.0.3.** The registry's `latest` is 7.0.2
  (2026-07-08), the native compiler. Its linux-x64 package is one static
  binary plus the `lib.*.d.ts` files, with no dependency on node. Measured on
  the hello app against `phaser.d.ts`: 0.3 to 0.5 s and 77 MB peak, against
  2.7 s for 6.0.3 under node. Both passed it. TypeScript 7
  removed options that TypeScript 5 accepted (`--moduleResolution node`,
  `--target es5`, `--outFile`) and turns `strict` on by default; the index
  entry says so, and the two commands it gives need no `tsconfig.json`.
- **Both tools, not one.** esbuild strips types without checking them; tsc
  checks and is not asked to emit. `tsc --noEmit` then `esbuild --bundle` is
  the whole build.
- **Only the platform packages are installed.** `@esbuild/linux-x64` and
  `@typescript/typescript-linux-x64` carry the binaries. The `esbuild` and
  `typescript` wrapper packages are node launchers that locate those binaries;
  the image links the binaries directly, so no npm install runs for them.
  `@esbuild/linux-x64` ships no licence file, so the `esbuild` wrapper tarball
  is fetched for its `LICENSE.md` and nothing else.
- **The types are never copied into an app.** `phaser.d.ts` is 6.2 MB. A turn
  passes it to `tsc` from `/shelf`.
- **TypeScript apps use the global `Phaser`.** The page loads
  `vendor/phaser.min.js` with a script tag and the bundle after it. The bundle
  for the hello app is 774 bytes. `import Phaser from 'phaser'` type-checks
  but esbuild cannot resolve it; aliasing it to the vendored file works and
  makes a 1.8 MB bundle, so the index tells the builder not to import.

## Phaser under the gate's CSP (found while proving the build)

The gate serves `default-src 'self'` with no `img-src`, so `data:` and
`blob:` images are refused. Phaser 3.90.0 uses both by default. (The 4.2.1
build carries the same `data:` literals and `createObjectURL` calls; it was
not run.)

- It creates its `__DEFAULT`, `__MISSING` and `__WHITE` textures from base64
  `data:` URIs. Refused, the game throws before the first scene: observed in
  headless Chromium (WebGL and Canvas) and headless Firefox (Canvas), on a
  top-level page served with the gate's header (its `frame-ancestors` value
  aside).
- Its loader fetches images by XHR and assigns a `blob:` URL. Refused, no
  image loads.

Both have a documented config key. With both set, the same probe boots and
loads a PNG with no error in both browsers, and the Chromium WebGL screenshot
shows the text, the shapes and the image:

```js
images: { default: 'vendor/phaser-boot/default.png', missing: 'vendor/phaser-boot/missing.png', white: 'vendor/phaser-boot/white.png' },
loader: { imageLoadType: 'HTMLImageElement' },
```

So the shelf carries the three boot textures as PNG files
(`phaser/phaser-boot/`), decoded from the base64 literals in `phaser.min.js`;
a test holds them equal to those literals. `INDEX.md` gives the builder the
two keys as required. `load.svg`, `load.htmlTexture` and `textures.addBase64`
stay unusable and the index says so. One `data:` refusal remains in the
console at load (Phaser's blend-mode probe); it only falls back.

The other fix is `img-src 'self' data: blob:` at the gate. That is a change
to a ruled security header in `server/app/gate.py`, outside this lane, and is
not made here.

## Architecture notes
- **Shelf** (`harness/cc/shelf/`): three `[[file]]` entries in `MANIFEST.toml`
  for Phaser, vendored by `fetch.sh` exactly as the existing entries were. The
  boot textures are the one exception to "every third-party file has an
  entry": they are not tarball members, and the manifest header says how they
  are held. `INDEX.md` (the part of the brief that lists the shelf) gains a
  `phaser` entry and a `typescript` entry. `README.md` is brought up to date.
- **Image** (`harness/cc/Containerfile.apps`): a first stage, `toolchain`,
  downloads the three tarballs with `ADD`, checks all three with one
  `sha256sum -c`, and unpacks them under `/out`. The final stage copies `/out`
  onto `/usr/local`, links `tsc`, and runs both tools' `--version` so an image
  whose tools cannot start fails at build. The downloads never enter the
  final image. Versions are exact `ARG`s; a checksum mismatch stops the build
  before anything is unpacked.
- **Permissions** (`harness/cc/apps/config/settings.json`): `Bash(esbuild:*)`
  and `Bash(tsc:*)` join the allowlist. `Bash(npm:*)` and `Bash(npx:*)` stay
  denied. `SETTINGS-NOTES.md` records the reason. `node` was already allowed,
  so these add no reach.
- **Brief** (`harness/cc/apps/config/BUILDER-BRIEF.md`, §3): "no build step,
  no bundler" becomes "nothing is built after you stop", and a short
  subsection names the two commands, the one esbuild line, where Phaser is,
  and that every library is still a file copied from `/shelf`.
- **Commands exposed to the builder**, exactly:
  - `tsc --noEmit src/main.ts`
  - `tsc --noEmit --skipLibCheck /shelf/phaser/types/phaser.d.ts src/main.ts`
  - `esbuild src/main.ts --bundle --outfile=app.js`
- **Tests** (`harness/cc/tests/test_apps_image.py`): the Phaser entries hash
  to the files on disk and nothing unlisted sits in the slot; the boot
  textures are the literals inside `phaser.min.js`; the Containerfile pins
  exact versions and every `ADD` has a 64-hex digest in the `sha256sum -c`
  line; the Bash allowlist is exactly the eleven expected commands and
  `npm`/`npx` are denied; the brief and the index name the pinned versions,
  the esbuild line, the type path and the two config keys.

## What was proved without the image, and what was not
Proved, on the host, in a namespace with no network and `/shelf` read-only:
- The `toolchain` stage's `RUN` text, taken from the Containerfile itself and
  run unchanged against tarballs fetched from the `ADD` URLs, verifies all
  three digests and produces the `/out` tree. A digest altered by one
  character fails it before anything is unpacked.
- The final stage's `ln` and `--version` line, run unchanged over that tree,
  prints `0.28.2` and `Version 7.0.2`.
- With that tree as `/usr/local`, the three commands above type-check and
  bundle a Phaser and TypeScript hello app; a deliberate type error exits 1.
- That built app, served the same way as the probe, renders its text and its
  rectangle in headless Chromium through WebGL.

Not proved:
- That `podman build` accepts the file: `ADD` from a URL with `ARG`
  expansion, the second stage, `COPY --from`. These are standard instructions
  but no image was built.
- That the tools run inside the real image under the seat's uid, mounts and
  limits, and that the registry is reachable from the build.
- That Claude Code's permission matcher admits `esbuild` and `tsc` from the
  managed settings in the image. The entries follow the form of the existing
  ones.
- That the builder, given this brief, writes a working Phaser app. No app
  turn was run.
- Phaser inside the house iframe, and Phaser audio, tilemaps, physics and
  every loader other than `load.image` under the CSP.

## Deploy, after merge
1. `sudo bash harness/keyboard/10-appsbuilding.sh`. It rebuilds the image and
   loads it into the seat's store. The build needs `registry.npmjs.org` for
   the three tarballs. Its log should show three `OK` lines from
   `sha256sum`, then `0.28.2` and `Version 7.0.2`.
2. No server restart, no config key, no migration. The brief, settings, shelf
   and tools are all image content.
3. Confirm with one app turn that asks for a small Phaser game in TypeScript.
   Check that `/work` holds `src/`, `app.js`, `vendor/phaser.min.js` and
   `vendor/phaser-boot/`, and that the preview renders.

`harness/cc/Containerfile.apps` is a gated path at push time.

Rollback is a revert of the merge and step 1 again.

## Lane → Review owner (DETERMINISTIC — filled from the lane, never preference)
- **Lane**: gable (`harness/cc/`).
- **Review owner**: Gable.

## Builder (USER PREFERENCE — who orchestrates; never touches Review owner)
- **Builder**: BuildGable, the keyboard seat.

## Cross-lane split
- **Applies**: no

## Expected diff tier
Tier 2 — the apps image, its managed settings and its brief.

## Token estimate
One build hand, one session.

## Confirm record
- **Confirmed by**: plink
- **#custodian seq**: 3507
- **Confirmed at**: 2026-11-11
- **Review**: PASS by Gable #3496 at 4fbf278; his tsconfig note verified on the pinned binary and folded at ed45283.

## Status
merged
<!-- advanced from `built@loop/2026-11-11-apps-shelf-phaser-typescript` by `board --mark-merged` on 2026-11-12: build merged as b51f831. The word `built@loop/2026-11-11-apps-shelf-phaser-typescript` on a merged spec made it indistinguishable from a buildable one. -->
