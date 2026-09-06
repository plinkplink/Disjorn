// build.mjs — generate house-store.mjs from house-store.js.
//
//     node harness/cc/shelf/store/build.mjs            # write house-store.mjs
//     node harness/cc/shelf/store/build.mjs --check    # drift check, exit 1
//
// One shim, two builds: a classic <script> that sets window.houseStore, and an
// ES module that exports it. The two files are byte-identical down to the
// BUILD TAIL marker; below it, the window assignments become exports. That is
// the whole "bundler". test_store.mjs holds both halves of the promise.

import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const MARKER = '/* --- BUILD TAIL: the classic and ESM builds differ here and nowhere else. --- */\n';

export const ESM_TAIL = `// GENERATED from house-store.js by build.mjs — do not edit; edit the .js.
export { houseStore, STORE_VERSION, StoreScopeNotAvailable };
export default houseStore;
`;

export const CLASSIC = fileURLToPath(new URL('house-store.js', import.meta.url));
export const ESM = fileURLToPath(new URL('house-store.mjs', import.meta.url));

/** The ESM build of a classic source: everything above the marker, then exports. */
export function render(classicSource) {
  const cut = classicSource.indexOf(MARKER);
  if (cut === -1) throw new Error('house-store.js has no BUILD TAIL marker');
  return classicSource.slice(0, cut) + MARKER + ESM_TAIL;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const wanted = render(await readFile(CLASSIC, 'utf8'));
  const found = await readFile(ESM, 'utf8').catch(() => null);
  if (process.argv.includes('--check')) {
    if (found !== wanted) {
      console.error('house-store.mjs is stale — run: node build.mjs');
      process.exit(1);
    }
    console.log('house-store.mjs is current');
  } else if (found === wanted) {
    console.log('house-store.mjs is current');
  } else {
    await writeFile(ESM, wanted);
    console.log('wrote house-store.mjs');
  }
}
