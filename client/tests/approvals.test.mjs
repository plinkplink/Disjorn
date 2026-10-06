// The approvals store, hash route, modal and panel, bundled for node:
//
//     node --test client/tests/approvals.test.mjs

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
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ApprovalModal } from "../src/components/ApprovalModal";
import { ApprovalsPanel } from "../src/views/ApprovalsPanel";
import { planRoomHash, planRoomRouteFromHash } from "../src/hashRoute";
import {
  actBlockReason, pendingForMe, upsertProposal, useApprovals,
} from "../src/stores/approvals";

export { actBlockReason, pendingForMe, planRoomHash, planRoomRouteFromHash,
  upsertProposal, useApprovals };

const noop = () => {};

export function modal(proposal, me) {
  return renderToStaticMarkup(createElement(ApprovalModal, {
    slug: proposal ? proposal.slug : "missing", proposal, me,
    hasSpecCard: false, onOpenCard: noop, onClose: noop,
  }));
}

export function panel(state) {
  // A server render reads the store's initial state, so the fixture goes there.
  Object.assign(useApprovals.getInitialState(), state);
  return renderToStaticMarkup(createElement(ApprovalsPanel, {
    slug: null, onOpen: noop, hasSpecCard: () => false, onOpenCard: noop,
  }));
}
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
    jsx: 'automatic', nodePaths: [nm], logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' },
    banner: { js: "import { createRequire } from 'node:module';"
      + ' const require = createRequire(import.meta.url);' },
  });
  scratch = mkdtempSync(join(tmpdir(), 'approvals-'));
  const file = join(scratch, 'bundle.mjs');
  writeFileSync(file, out.outputFiles[0].text);
  m = await import(pathToFileURL(file).href);
});

after(() => { if (scratch) rmSync(scratch, { recursive: true, force: true }); });

const DISARMED = 'The approval surface is not enabled on this server: '
  + 'APPROVAL_ENABLED is false.';

function proposal(id, { closed = false, states = {}, decision = 'pending' } = {}) {
  const principals = ['plink', 'res-claudette', 'res-gable'];
  return {
    id, slug: `p-${id}`, title: `Proposal ${id}`, text: '**bold** body',
    created_by: { type: 'bot', id: 1, label: 'Gable (via broker)' },
    created_at: '2026-10-05T10:00:00Z',
    closed_at: closed ? '2026-10-05T11:00:00Z' : null,
    decision,
    states: principals.map((principal) => ({
      principal, state: states[principal] ?? 'pending', remarks: null,
      acted_by: null, acted_at: null,
    })),
  };
}

function buttons(html) {
  return Object.fromEntries([...html.matchAll(/<button([^>]*)>(Approve|Rework|Deny)<\/button>/g)]
    .map((b) => [b[2], b[1].includes('disabled')]));
}

test('the badge counts open proposals still waiting on my own answer', () => {
  const list = [
    proposal(1),
    proposal(2, { states: { plink: 'approve' } }),
    proposal(3, { closed: true, decision: 'denied', states: { 'res-gable': 'deny' } }),
    proposal(4, { states: { 'res-gable': 'rework' }, decision: 'rework' }),
  ];
  assert.equal(m.pendingForMe(list, 'plink'), 2);
  assert.equal(m.pendingForMe(list, 'res-gable'), 2);
  assert.equal(m.pendingForMe(list, 'alice'), 0);
  assert.equal(m.pendingForMe(list, null), 0);
});

test('an update replaces its proposal and keeps the newest first', () => {
  const store = m.useApprovals;
  store.setState({ status: 'ready', proposals: [proposal(2), proposal(1)] });
  store.getState().onUpdate({ type: 'approval_update',
    proposal: proposal(1, { states: { plink: 'approve' } }) });
  store.getState().onUpdate({ type: 'approval_update', proposal: proposal(3) });
  const got = store.getState().proposals;
  assert.deepEqual(got.map((p) => p.id), [3, 2, 1]);
  assert.equal(got[2].states[0].state, 'approve');
});

test('a disarmed server is unavailable with its own sentence, not an empty list', async () => {
  const real = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: DISARMED }),
    { status: 503, headers: { 'Content-Type': 'application/json' } });
  try {
    m.useApprovals.setState({ status: 'idle', proposals: [] });
    await m.useApprovals.getState().refresh();
  } finally {
    globalThis.fetch = real;
  }
  const st = m.useApprovals.getState();
  assert.equal(st.status, 'unavailable');
  assert.equal(st.detail, DISARMED);
});

test('the disarmed panel shows the detail verbatim and never says empty', () => {
  const html = m.panel({ status: 'unavailable', detail: DISARMED, proposals: [] });
  assert.ok(html.includes(DISARMED), html);
  assert.ok(!/no open proposals/i.test(html), html);
});

test('deny and rework wait for remarks while approve does not', () => {
  const html = m.modal(proposal(1), 'plink');
  assert.deepEqual(buttons(html), { Approve: false, Rework: true, Deny: true });
  assert.ok(html.includes('Deny and Rework need remarks.'), html);
  assert.ok(html.includes('0 / 4000'), html);
  assert.ok(html.includes('<strong>bold</strong>'), html);
  assert.equal(m.actBlockReason('approve', ''), null);
  assert.equal(m.actBlockReason('deny', 'no'), null);
  assert.notEqual(m.actBlockReason('rework', '   '), null);
  assert.notEqual(m.actBlockReason('approve', 'x'.repeat(4001)), null);
});

test('a closed proposal shows its record and no buttons', () => {
  const html = m.modal(proposal(1, { closed: true, decision: 'approved', states:
    { plink: 'approve', 'res-claudette': 'approve', 'res-gable': 'approve' } }), 'plink');
  assert.deepEqual(buttons(html), {});
  assert.ok(html.includes('approved'), html);
});

test('someone who is not a principal gets the record and no buttons', () => {
  assert.deepEqual(buttons(m.modal(proposal(1), 'alice')), {});
});

test('the hash route round-trips the tab and slug', () => {
  assert.deepEqual(m.planRoomRouteFromHash('#/planroom'), { tab: 'board', slug: null });
  assert.deepEqual(m.planRoomRouteFromHash('#/planroom/approvals'),
    { tab: 'approvals', slug: null });
  assert.deepEqual(m.planRoomRouteFromHash('#/planroom/approvals/2026-10-05-x.y'),
    { tab: 'approvals', slug: '2026-10-05-x.y' });
  assert.equal(m.planRoomRouteFromHash('#/channels/3'), null);
  assert.equal(m.planRoomHash({ tab: 'approvals', slug: 'a b' }), '#/planroom/approvals/a%20b');
});
