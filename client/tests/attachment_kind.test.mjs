// How a message shows an attachment, bundled for node:
//
//     node --test client/tests/attachment_kind.test.mjs

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const CLIENT = dirname(HERE);

function nodeModules() {
  const found = [process.env.DISJORN_CLIENT_NODE_MODULES,
    join(CLIENT, 'node_modules'), '/opt/node_modules']
    .find((d) => d && existsSync(join(d, 'esbuild')));
  if (!found) throw new Error('no client toolchain (esbuild) found');
  return found;
}

const ENTRY = `
export { attachmentKind } from "../src/lib/attachmentKind";
`;

let m;
let scratch;

before(async () => {
  const nm = nodeModules();
  const bin = join(nm, '@esbuild', `${process.platform}-${process.arch}`, 'bin', 'esbuild');
  if (!process.env.ESBUILD_BINARY_PATH && existsSync(bin)) process.env.ESBUILD_BINARY_PATH = bin;
  const esbuild = createRequire(import.meta.url)(join(nm, 'esbuild'));
  const out = await esbuild.build({
    stdin: { contents: ENTRY, resolveDir: HERE, loader: 'js' },
    bundle: true, platform: 'node', format: 'esm', write: false,
    nodePaths: [nm], logLevel: 'silent',
  });
  scratch = mkdtempSync(join(tmpdir(), 'attkind-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const att = (mime_type, width = null, url = '/media/1?v=display') => ({ mime_type, width, url });

test('a file the server decoded is an image, whatever type it was uploaded as', () => {
  assert.equal(m.attachmentKind(att('image/png', 64)), 'image');
  assert.equal(m.attachmentKind(att('text/html', 64)), 'image');
  assert.equal(m.attachmentKind(att('image/heic', 4032)), 'image');
});

test('an image type the server could not decode is a download, never an <img>', () => {
  assert.equal(m.attachmentKind(att('image/svg+xml')), 'download');
  assert.equal(m.attachmentKind(att('image/png')), 'download');
});

test('audio and video open in a tab to play', () => {
  assert.equal(m.attachmentKind(att('video/mp4')), 'media');
  assert.equal(m.attachmentKind(att('Audio/MPEG')), 'media');
});

test('every other file is a download', () => {
  for (const mime of ['text/html', 'application/xhtml+xml', 'application/xml',
    'application/pdf', 'application/javascript', 'application/octet-stream', '']) {
    assert.equal(m.attachmentKind(att(mime)), 'download', mime);
  }
});

test('an attachment with no URL is never an image', () => {
  assert.equal(m.attachmentKind(att('image/png', 64, null)), 'download');
});
