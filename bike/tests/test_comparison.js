// Run from the repository root: node bike/tests/test_comparison.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, textContent: '', style: {},
    setAttribute(key, value) { this[key] = value; },
    removeAttribute(key) { delete this[key]; }
  });
  return elements.get(id);
}
const requests = [];
const context = vm.createContext({
  document: { getElementById: element },
  AbortSignal: {}, fetch: (url, options) => { requests.push({ url, options }); return new Promise(() => {}); }, setTimeout() {}
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/station.js'), 'utf8'), context);
const show = context.renderComparison;

// No invented AI energy: blank/invalid input has no comparison values.
for (const value of ['', '0', '-1', 'invalid']) {
  element('target').value = value;
  show({ mode: 'usb', session: null });
  assert.equal(element('comparison-content').hidden, true);
}
// Idle input is already mWh, while historical session targets remain Wh.
element('target').value = '50';
show({ mode: 'usb', session: null });
assert.equal(element('comparison-content').hidden, false);
assert.equal(element('comparison-target').textContent, '50');
assert.equal(element('comparison-generated').textContent, '0');
assert.equal(element('comparison-phase').textContent, '측정 준비');
// Session target wins over a stale/different browser input.
const session = { target_wh: 0.05, energy_mwh: 0.25, campaign_energy_wh: 0.025, status: 'running' };
element('target').value = '1';
show({ mode: 'usb', session });
assert.equal(element('comparison-target').textContent, '50');
assert.equal(element('comparison-percent').textContent, '50%');
assert.equal(element('comparison-fill').style.width, '50%');
assert.match(element('comparison-equivalent').textContent, /50%/);
assert.match(element('comparison-remaining').textContent, /25 mWh 남/);
// Over 100% must not erase surplus or imply that recording stops.
show({ mode: 'ble', session: { ...session, campaign_energy_wh: 0.125 } });
assert.equal(element('comparison-percent').textContent, '250%');
assert.equal(element('comparison-fill').style.width, '100%');
assert.match(element('comparison-remaining').textContent, /75 mWh 초과/);
assert.equal(element('comparison-progress')['aria-valuenow'], '100');
assert.match(element('comparison-progress')['aria-valuetext'], /250%/);
// Historical sessions without a target do not inherit a draft target.
show({ mode: 'usb', session: { ...session, target_wh: null } });
assert.equal(element('comparison-content').hidden, true);
// Small values and synthetic sources remain explicitly described.
show({ mode: 'usb', session: { ...session, campaign_energy_wh: 0.0001, status: 'completed' } });
assert.match(element('comparison-equivalent').textContent, /0.2%/);
assert.equal(element('comparison-generated-label').textContent, '×100 환산 발전량');
assert.equal(element('comparison-phase').textContent, '측정 결과');
// Clearing a target empties the battery rather than retaining the old fill.
element('target').value = '';
show({ mode: 'usb', session: null });
assert.equal(element('comparison-fill').style.width, '0%');
assert.equal(element('comparison-percent').textContent, '—');
assert.equal(element('target-empty').hidden, false);
// Convert the user's mWh input exactly once when submitting the Wh API field.
element('target').value = '50';
element('participant').value = 'unit-check';
element('start').onclick();
const start = requests.find(r => r.url === '/api/session/start');
assert.equal(JSON.parse(start.options.body).target_wh, 0.05);
console.log('AI comparison checks passed: input preview, units, ratio, surplus, saved targets, simulation.');

// Gemini never receives a fabricated Wh goal from tokens or a draft input.
show({mode:'usb', session:{target_wh:null,energy_mwh:1,campaign_energy_wh:0.1,campaign_entry:{energy_basis:'tokens_only'}}});
assert.equal(element('comparison-percent').textContent,'—');
assert.match(element('target-empty').textContent,/Gemini/);
