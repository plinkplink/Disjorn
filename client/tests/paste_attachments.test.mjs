// Which clipboard contents a paste attaches, bundled for node:
//
//     node --test client/tests/paste_attachments.test.mjs

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
export { pastedImages } from "../src/lib/pasteAttachments";
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
  scratch = mkdtempSync(join(tmpdir(), 'paste-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

// Local time, so the expected names do not depend on the host's zone.
const NOW = new Date(2026, 10, 11, 14, 32, 5);
const STAMP = 'pasted-2026-11-11-143205';

const png = (name = 'image.png') => ({ name, type: 'image/png' });

// A DataTransfer as the paste event carries it. `only` says which of the two
// file views the browser filled in; most fill both.
function clip({ files = [], text = '', html = '', only = 'both' } = {}) {
  const strings = { 'text/plain': text, 'text/html': html };
  const items = [
    ...Object.entries(strings).filter(([, v]) => v !== '')
      .map(([type]) => ({ kind: 'string', type, getAsFile: () => null })),
    ...files.map((f) => ({ kind: 'file', type: f.type, getAsFile: () => f })),
  ];
  return {
    getData: (format) => strings[format] ?? '',
    files: only === 'items' ? [] : files,
    items: only === 'files' ? [] : items,
  };
}

const names = (c, taken) => m.pastedImages(c, NOW, taken).map((p) => p.name);

test('a screenshot attaches under a timestamped name', () => {
  const shot = png();
  const got = m.pastedImages(clip({ files: [shot] }), NOW);
  assert.equal(got.length, 1);
  assert.equal(got[0].file, shot);
  assert.equal(got[0].name, `${STAMP}.png`);
});

test('a nameless image takes its extension from its type', () => {
  assert.deepEqual(names(clip({ files: [{ name: '', type: 'image/jpeg' }] })), [`${STAMP}.jpg`]);
  assert.deepEqual(names(clip({ files: [{ name: '', type: 'image/webp' }] })), [`${STAMP}.webp`]);
  assert.deepEqual(names(clip({ files: [{ name: '', type: 'image/svg+xml' }] })), [`${STAMP}.svg`]);
});

test('the image is found whichever file view the browser filled in', () => {
  assert.deepEqual(names(clip({ files: [png()], only: 'files' })), [`${STAMP}.png`]);
  assert.deepEqual(names(clip({ files: [png()], only: 'items' })), [`${STAMP}.png`]);
  assert.deepEqual(names(clip({ files: [png()], only: 'both' })), [`${STAMP}.png`]);
});

test('every image in one paste attaches, each under its own name', () => {
  const got = names(clip({ files: [png(), png(), png('cat.png')] }));
  assert.deepEqual(got, [`${STAMP}.png`, `${STAMP}-2.png`, 'cat.png']);
});

test('a second paste in the same second does not reuse a staged name', () => {
  assert.deepEqual(names(clip({ files: [png()] }), [`${STAMP}.png`]), [`${STAMP}-2.png`]);
  assert.deepEqual(names(clip({ files: [png()] }), [`${STAMP}.png`, `${STAMP}-2.png`]),
    [`${STAMP}-3.png`]);
});

test('a real filename is kept', () => {
  assert.deepEqual(names(clip({ files: [png('holiday photo.png')] })), ['holiday photo.png']);
});

test('plain text wins over an image rendering of it', () => {
  const sheet = clip({ files: [png()], text: 'a\tb\n1\t2', html: '<table></table>' });
  assert.deepEqual(m.pastedImages(sheet, NOW), []);
});

test('an image copied from a page, html but no plain text, attaches', () => {
  const copied = clip({ files: [png()], html: '<img src="https://example.test/cat.png">' });
  assert.deepEqual(names(copied), [`${STAMP}.png`]);
});

test('whitespace-only text does not count as text', () => {
  assert.deepEqual(names(clip({ files: [png()], text: ' \n' })), [`${STAMP}.png`]);
});

test('text that only names the copied files is a file copy, not text', () => {
  const a = png('a b.png');
  const b = { name: 'b.jpg', type: 'image/jpeg' };
  assert.deepEqual(names(clip({ files: [a], text: 'a b.png' })), ['a b.png']);
  assert.deepEqual(names(clip({ files: [a, b], text: '/home/u/a b.png\n/home/u/b.jpg' })),
    ['a b.png', 'b.jpg']);
  assert.deepEqual(names(clip({ files: [a], text: 'file:///home/u/a%20b.png\r\n' })), ['a b.png']);
  assert.deepEqual(names(clip({ files: [a], text: 'C:\\Users\\u\\a b.png' })), ['a b.png']);
  assert.deepEqual(names(clip({ files: [a], text: 'see a b.png' })), []);
  assert.deepEqual(names(clip({ files: [a], text: 'a b.png\nand a caption' })), []);
});

test('a paste with no image is left alone', () => {
  assert.deepEqual(m.pastedImages(clip({ text: 'hello' }), NOW), []);
  assert.deepEqual(m.pastedImages(clip(), NOW), []);
  const pdf = { name: 'spec.pdf', type: 'application/pdf' };
  assert.deepEqual(m.pastedImages(clip({ files: [pdf] }), NOW), []);
});

test('only the images of a mixed file copy attach', () => {
  const pdf = { name: 'spec.pdf', type: 'application/pdf' };
  const mixed = clip({ files: [pdf, png('cat.png')], text: 'spec.pdf\ncat.png' });
  assert.deepEqual(names(mixed), ['cat.png']);
});
