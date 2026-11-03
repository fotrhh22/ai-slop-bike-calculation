// DOM behavior only; no browser, network or real participant deletion.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
function node() {
  return { value: '', children: [], style: {}, classList: { toggle() {} },
    setAttribute(key, value) { this[key] = value; },
    removeAttribute(key) { delete this[key]; },
    replaceChildren() { this.children = []; },
    append(...items) { this.children.push(...items); } };
}
const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); };
let confirm = true;
const context = vm.createContext({ document: { getElementById: el, createElement: node, createTextNode: () => node() },
  window: { confirm: () => confirm }, AbortSignal: {}, fetch: () => new Promise(() => {}),
  setTimeout() {}, calls: [] });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/station.js'), 'utf8'), context);
vm.runInContext('action = (path, body) => calls.push({path, body});', context);
const entry = { id: 'entry1', participant_code: 'E2E66427', energy_basis: 'tokens_only', state: 'waiting' };
const data = { campaign: { enabled: true, entries: [entry], selected: entry } };
context.renderCampaign(data);
let [select, remove] = el('campaign-queue').children[0].children;
assert.equal(remove.textContent, '×');
assert.equal(remove.disabled, false);
assert.match(remove['aria-label'], /E2E66427/);
remove.onclick();
assert.equal(context.calls.length, 1);
assert.equal(context.calls[0].path, '/api/campaign/delete');
assert.equal(context.calls[0].body.entry_id, 'entry1');
confirm = false; remove.onclick();
assert.equal(context.calls.length, 1, 'cancel must not delete or select');
context.renderCampaign({ campaign: { ...data.campaign, entries: [{ ...entry, state: 'measuring' }] } });
remove = el('campaign-queue').children[0].children[1];
assert.equal(remove.disabled, true, 'claimed entries must be protected');
const older = { ...entry, id: 'old', participant_code: 'OLDER001', created_at: '2026-11-04T00:00:00Z' };
const newer = { ...entry, id: 'new', participant_code: 'NEWER001', created_at: '2026-11-04T01:00:00Z', reference_target_wh: .3463 };
context.render({ mode: 'usb', fresh: true, campaign: { enabled: true, entries: [older, newer], selected: newer }, trace: [] });
assert.match(el('campaign-queue').children[0].children[0].textContent, /NEWER001/);
assert.equal(el('target').value, '346.3');
assert.equal(el('target').readOnly, true);
assert.match(el('target-help').textContent, /Gemini 소비량 측정·추정값이 아닙니다/);
assert.equal(el('usb-card').hidden, false);
assert.equal(el('ble-card').hidden, true);
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
assert.ok(!html.includes('demo-mode') && !html.includes('comparison-controls'));
console.log('Waiting participant delete controls: confirmation, separate action and measuring protection passed');
