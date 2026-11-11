// The chimes' output level, bundled for node:
//
//     node --test client/tests/chime_level.test.mjs

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
export * from "../src/lib/chimeLevel";
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
  scratch = mkdtempSync(join(tmpdir(), 'chime-level-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

// The value an AudioParam holds t seconds into the chime, per the Web Audio
// definition of exponentialRampToValueAtTime.
function envelopeAt(note, t) {
  const dt = t - note.at;
  if (dt < 0 || dt > note.decay) return 0;
  const floor = m.ENVELOPE_FLOOR;
  if (dt <= m.ATTACK_S) return floor * (note.peak / floor) ** (dt / m.ATTACK_S);
  return note.peak * (floor / note.peak) ** ((dt - m.ATTACK_S) / (note.decay - m.ATTACK_S));
}

// No sample can exceed the summed envelopes, whatever the partials' phases.
function peakBound(kind, volume) {
  const notes = m.CHIMES[kind];
  const length = Math.max(...notes.map((n) => n.at + n.decay));
  let peak = 0;
  for (let t = 0; t <= length; t += 1 / 48_000) {
    peak = Math.max(peak, notes.reduce((sum, n) => sum + envelopeAt(n, t), 0));
  }
  return peak * m.masterGain(volume);
}

test('every slider position plays half again as loud as the squared curve alone', () => {
  for (const volume of [0.05, 0.3, 0.6, 1]) {
    assert.ok(Math.abs(m.masterGain(volume) / (volume * volume) - 1.5) < 1e-12);
  }
});

test('a slider at zero is silent', () => {
  assert.equal(m.masterGain(0), 0);
});

test('neither chime can clip with the slider at full', () => {
  for (const kind of ['message', 'mention']) {
    const peak = peakBound(kind, 1);
    assert.ok(peak > 0.2 && peak < 1, `${kind} peaks at ${peak}`);
  }
});
