/**
 * Unit tests for ledger merge, settlement, and copy-text logic.
 *
 * Run:  node test_ledger_ui.js
 *
 * These tests exercise the pure functions extracted from index.html:
 *   _getEffectivePlayers, _computeSettlements, _mergeLabel, copy text generation
 */

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) { passed++; }
  else { failed++; console.error(`  FAIL: ${msg}`); }
}

function assertClose(a, b, msg, eps = 0.005) {
  assert(Math.abs(a - b) < eps, `${msg} (expected ${b}, got ${a})`);
}

function section(name) { console.log(`\n--- ${name} ---`); }

// ====== Extracted functions (mirroring index.html logic) ======

// Globals the functions depend on
let _ledgerData = null;
let _playerMerges = [];
let _forcedPairs = [];

function _getEffectivePlayers() {
  if (!_ledgerData) return [];
  const players = _ledgerData.players.map(p => ({...p}));
  for (const merge of _playerMerges) {
    const fromIdx = players.findIndex(p => p.id === merge.from);
    const intoIdx = players.findIndex(p => p.id === merge.into);
    if (fromIdx === -1 || intoIdx === -1 || fromIdx === intoIdx) continue;
    players[intoIdx].buyin = Math.round((players[intoIdx].buyin + players[fromIdx].buyin) * 100) / 100;
    players[intoIdx].cashout = Math.round((players[intoIdx].cashout + players[fromIdx].cashout) * 100) / 100;
    players[intoIdx].net = Math.round((players[intoIdx].net + players[fromIdx].net) * 100) / 100;
    players.splice(fromIdx, 1);
  }
  players.sort((a, b) => b.net - a.net);
  return players;
}

function _mergeLabel(p) {
  const dupes = _ledgerData.players.filter(o => o.name === p.name);
  return dupes.length > 1 ? `${p.name} (${p.id.slice(0, 6)})` : p.name;
}

function _computeSettlements(players, forcedPairs) {
  const nets = {};
  for (const p of players) nets[p.name] = p.net;

  const settlements = [];

  for (const fp of forcedPairs) {
    const fromNet = nets[fp.from] || 0;
    const toNet = nets[fp.to] || 0;
    if (fromNet >= -0.005 || toNet <= 0.005) continue;
    const amount = Math.round(Math.min(Math.abs(fromNet), toNet) * 100) / 100;
    if (amount < 0.01) continue;
    settlements.push({ from: fp.from, to: fp.to, amount, forced: true });
    nets[fp.from] = Math.round((nets[fp.from] + amount) * 100) / 100;
    nets[fp.to] = Math.round((nets[fp.to] - amount) * 100) / 100;
  }

  const debtors = Object.entries(nets).filter(([,v]) => v < -0.005).map(([n,v]) => ({name:n, r:Math.round(Math.abs(v)*100)/100}));
  const creditors = Object.entries(nets).filter(([,v]) => v > 0.005).map(([n,v]) => ({name:n, r:Math.round(v*100)/100}));
  debtors.sort((a,b) => b.r - a.r);
  creditors.sort((a,b) => b.r - a.r);

  let i = 0, j = 0;
  while (i < debtors.length && j < creditors.length) {
    const amount = Math.round(Math.min(debtors[i].r, creditors[j].r) * 100) / 100;
    if (amount > 0.005) settlements.push({ from: debtors[i].name, to: creditors[j].name, amount });
    debtors[i].r -= amount;
    creditors[j].r -= amount;
    if (debtors[i].r < 0.01) i++;
    if (creditors[j].r < 0.01) j++;
  }
  return settlements;
}

function copyText(settlements) {
  return settlements.map(s => `${s.from} pays ${s.to} $${s.amount.toFixed(2)}`).join('\n');
}

// ====== Test data helpers ======

function makePlayers(...specs) {
  // specs: [name, id, buyin, cashout, net]
  return specs.map(([name, id, buyin, cashout, net]) => ({ name, id, buyin, cashout, net }));
}

// ====== Tests ======

section('_getEffectivePlayers: no merges');
{
  _ledgerData = { players: makePlayers(['JJ', 'p1', 100, 150, 50], ['Noah', 'p2', 100, 50, -50]) };
  _playerMerges = [];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, 'should return 2 players');
  assert(eff[0].name === 'JJ', 'JJ should be first (highest net)');
  assert(eff[1].name === 'Noah', 'Noah should be second');
}

section('_getEffectivePlayers: basic merge');
{
  _ledgerData = { players: makePlayers(
    ['JJ', 'p1', 100, 180, 80],
    ['JJ2', 'p2', 50, 20, -30],
    ['Noah', 'p3', 100, 50, -50],
  )};
  _playerMerges = [{ from: 'p2', into: 'p1' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, 'should have 2 players after merge');
  const jj = eff.find(p => p.name === 'JJ');
  assert(jj !== undefined, 'JJ should exist');
  assertClose(jj.buyin, 150, 'merged buyin = 100 + 50');
  assertClose(jj.cashout, 200, 'merged cashout = 180 + 20');
  assertClose(jj.net, 50, 'merged net = 80 + (-30)');
}

section('_getEffectivePlayers: merge does not mutate original');
{
  _ledgerData = { players: makePlayers(
    ['A', 'p1', 100, 200, 100],
    ['B', 'p2', 50, 10, -40],
  )};
  _playerMerges = [{ from: 'p2', into: 'p1' }];
  _getEffectivePlayers();
  assert(_ledgerData.players.length === 2, 'original data should still have 2 players');
  assert(_ledgerData.players[0].buyin === 100, 'original buyin unchanged');
}

section('_getEffectivePlayers: chained merges');
{
  _ledgerData = { players: makePlayers(
    ['JJ', 'p1', 100, 200, 100],
    ['JJ2', 'p2', 30, 10, -20],
    ['JJ3', 'p3', 20, 5, -15],
    ['Noah', 'p4', 50, 5, -45],
  )};
  // Merge JJ2 into JJ, then JJ3 into JJ
  _playerMerges = [{ from: 'p2', into: 'p1' }, { from: 'p3', into: 'p1' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, 'should have 2 players after two merges');
  const jj = eff.find(p => p.name === 'JJ');
  assertClose(jj.buyin, 150, 'triple merged buyin');
  assertClose(jj.cashout, 215, 'triple merged cashout');
  assertClose(jj.net, 65, 'triple merged net');
}

section('_getEffectivePlayers: invalid merge (same from/into) skipped');
{
  _ledgerData = { players: makePlayers(['A', 'p1', 100, 150, 50], ['B', 'p2', 100, 50, -50]) };
  _playerMerges = [{ from: 'p1', into: 'p1' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, 'should still have 2 players');
}

section('_getEffectivePlayers: invalid merge (unknown id) skipped');
{
  _ledgerData = { players: makePlayers(['A', 'p1', 100, 150, 50], ['B', 'p2', 100, 50, -50]) };
  _playerMerges = [{ from: 'pX', into: 'p1' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, 'should still have 2 players');
}

section('_getEffectivePlayers: sort order after merge');
{
  _ledgerData = { players: makePlayers(
    ['A', 'p1', 100, 80, -20],
    ['B', 'p2', 100, 120, 20],
    ['C', 'p3', 100, 130, 30],
  )};
  _playerMerges = [{ from: 'p1', into: 'p2' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 2, '2 players after merge');
  assert(eff[0].name === 'C', 'C (net 30) should be first');
  assert(eff[1].name === 'B', 'B (net 0) should be second');
  assertClose(eff[1].net, 0, 'B merged net = 20 + (-20)');
}

section('_computeSettlements: basic settlement');
{
  const players = makePlayers(
    ['Noah', 'p1', 100, 50, -50],
    ['JJ', 'p2', 100, 150, 50],
  );
  const s = _computeSettlements(players, []);
  assert(s.length === 1, 'should have 1 settlement');
  assert(s[0].from === 'Noah', 'Noah pays');
  assert(s[0].to === 'JJ', 'JJ receives');
  assertClose(s[0].amount, 50, 'amount = 50');
}

section('_computeSettlements: multiple debtors/creditors');
{
  const players = makePlayers(
    ['A', 'p1', 100, 200, 100],
    ['B', 'p2', 100, 170, 70],
    ['C', 'p3', 100, 20, -80],
    ['D', 'p4', 100, 10, -90],
  );
  const s = _computeSettlements(players, []);
  // Sum of debts = 170, sum of credits = 170. Should balance.
  const totalPaid = s.reduce((sum, x) => sum + x.amount, 0);
  assertClose(totalPaid, 170, 'total payments should equal total debts');
  assert(s.length >= 2, 'should have at least 2 settlements');
}

section('_computeSettlements: zero-net player produces no settlement');
{
  const players = makePlayers(
    ['A', 'p1', 100, 100, 0],
    ['B', 'p2', 100, 150, 50],
    ['C', 'p3', 100, 50, -50],
  );
  const s = _computeSettlements(players, []);
  assert(s.length === 1, 'only 1 settlement needed');
  assert(s[0].from === 'C', 'C pays');
  assert(s[0].to === 'B', 'B receives');
}

section('_computeSettlements: all even = no settlements');
{
  const players = makePlayers(
    ['A', 'p1', 100, 100, 0],
    ['B', 'p2', 100, 100, 0],
  );
  const s = _computeSettlements(players, []);
  assert(s.length === 0, 'no settlements needed');
}

section('_computeSettlements: forced pair');
{
  const players = makePlayers(
    ['A', 'p1', 100, 200, 100],
    ['B', 'p2', 100, 130, 30],
    ['C', 'p3', 100, 20, -80],
    ['D', 'p4', 100, 50, -50],
  );
  const forced = [{ from: 'D', to: 'B' }];
  const s = _computeSettlements(players, forced);
  // D should pay B first (forced), then greedy for the rest
  assert(s[0].forced === true, 'first settlement should be forced');
  assert(s[0].from === 'D', 'D pays');
  assert(s[0].to === 'B', 'B receives');
  assertClose(s[0].amount, 30, 'forced amount = min(50, 30) = 30');
  // Total should still balance
  const totalPaid = s.reduce((sum, x) => sum + x.amount, 0);
  assertClose(totalPaid, 130, 'total payments should equal total debts');
}

section('_computeSettlements: forced pair with invalid debtor is skipped');
{
  const players = makePlayers(
    ['A', 'p1', 100, 200, 100],
    ['B', 'p2', 100, 0, -100],
  );
  // A has positive net, so forcing A to pay should be skipped
  const forced = [{ from: 'A', to: 'A' }];
  const s = _computeSettlements(players, forced);
  assert(s.every(x => !x.forced), 'no forced settlement should appear');
}

section('_mergeLabel: unique names');
{
  _ledgerData = { players: makePlayers(['JJ', 'p1', 0, 0, 0], ['Noah', 'p2', 0, 0, 0]) };
  assert(_mergeLabel({ name: 'JJ', id: 'p1' }) === 'JJ', 'unique name shows plain');
  assert(_mergeLabel({ name: 'Noah', id: 'p2' }) === 'Noah', 'unique name shows plain');
}

section('_mergeLabel: duplicate names get id suffix');
{
  _ledgerData = { players: makePlayers(['chat', 'abc123xyz', 0, 0, 0], ['chat', 'def456uvw', 0, 0, 0]) };
  const label1 = _mergeLabel({ name: 'chat', id: 'abc123xyz' });
  const label2 = _mergeLabel({ name: 'chat', id: 'def456uvw' });
  assert(label1 === 'chat (abc123)', 'first chat gets id suffix');
  assert(label2 === 'chat (def456)', 'second chat gets id suffix');
  assert(label1 !== label2, 'labels must differ');
}

section('_mergeLabel: only duplicated name gets suffix');
{
  _ledgerData = { players: makePlayers(['chat', 'p1abc', 0, 0, 0], ['chat', 'p2def', 0, 0, 0], ['Noah', 'p3ghi', 0, 0, 0]) };
  assert(_mergeLabel({ name: 'Noah', id: 'p3ghi' }) === 'Noah', 'unique name stays plain');
  assert(_mergeLabel({ name: 'chat', id: 'p1abc' }).includes('('), 'duplicate gets suffix');
}

section('copyText: format matches expected output');
{
  const settlements = [
    { from: 'Noah', to: 'JJ', amount: 264.69 },
    { from: 'Jake', to: 'Saiesh', amount: 450.00 },
    { from: 'Riley', to: 'JJ', amount: 79.29 },
  ];
  const text = copyText(settlements);
  const lines = text.split('\n');
  assert(lines.length === 3, '3 lines');
  assert(lines[0] === 'Noah pays JJ $264.69', 'first line format');
  assert(lines[1] === 'Jake pays Saiesh $450.00', 'second line format');
  assert(lines[2] === 'Riley pays JJ $79.29', 'third line format');
}

section('copyText: forced settlements included');
{
  const settlements = [
    { from: 'A', to: 'B', amount: 50, forced: true },
    { from: 'C', to: 'D', amount: 30 },
  ];
  const text = copyText(settlements);
  assert(text === 'A pays B $50.00\nC pays D $30.00', 'forced tag not in copy text');
}

section('Integration: merge + settlements');
{
  _ledgerData = { players: makePlayers(
    ['JJ', 'p1', 100, 180, 80],
    ['JJ2', 'p2', 50, 20, -30],
    ['Noah', 'p3', 100, 50, -50],
  )};
  _playerMerges = [{ from: 'p2', into: 'p1' }];
  const players = _getEffectivePlayers();
  assert(players.length === 2, '2 effective players');
  const s = _computeSettlements(players, []);
  assert(s.length === 1, '1 settlement');
  assert(s[0].from === 'Noah', 'Noah pays');
  assert(s[0].to === 'JJ', 'JJ (merged) receives');
  assertClose(s[0].amount, 50, 'amount = 50');
  const text = copyText(s);
  assert(text === 'Noah pays JJ $50.00', 'copy text correct after merge');
}

section('Integration: merge same-name players');
{
  _ledgerData = { players: makePlayers(
    ['chat', 'id_a', 100, 160, 60],
    ['chat', 'id_b', 50, 20, -30],
    ['Noah', 'id_c', 100, 70, -30],
  )};
  _playerMerges = [{ from: 'id_b', into: 'id_a' }];
  const players = _getEffectivePlayers();
  assert(players.length === 2, '2 players after merge');
  const chat = players.find(p => p.name === 'chat');
  assertClose(chat.net, 30, 'merged chat net = 60 + (-30)');
  const s = _computeSettlements(players, []);
  assert(s.length === 1, '1 settlement');
  assertClose(s[0].amount, 30, 'Noah pays chat $30');
}

section('Integration: merge + forced pair cleanup');
{
  _ledgerData = { players: makePlayers(
    ['A', 'p1', 100, 200, 100],
    ['B', 'p2', 50, 10, -40],
    ['C', 'p3', 100, 40, -60],
  )};
  // B owes money. If we merge B into A, B disappears as a debtor.
  _playerMerges = [{ from: 'p2', into: 'p1' }];
  const players = _getEffectivePlayers();
  const effectiveNames = new Set(players.map(p => p.name));
  // Simulate forced pair referencing the now-merged-away player
  _forcedPairs = [{ from: 'B', to: 'A' }];
  const cleaned = _forcedPairs.filter(fp => effectiveNames.has(fp.from) && effectiveNames.has(fp.to));
  assert(cleaned.length === 0, 'forced pair referencing merged player should be removed');
}

section('Edge: single player = no settlements');
{
  const players = makePlayers(['A', 'p1', 100, 100, 0]);
  const s = _computeSettlements(players, []);
  assert(s.length === 0, 'no settlements for single player');
}

section('Edge: very small amounts ignored');
{
  const players = makePlayers(
    ['A', 'p1', 100, 100.004, 0.004],
    ['B', 'p2', 100, 99.996, -0.004],
  );
  const s = _computeSettlements(players, []);
  assert(s.length === 0, 'sub-penny amounts should not produce settlements');
}

section('Edge: merge all into one');
{
  _ledgerData = { players: makePlayers(
    ['A', 'p1', 100, 120, 20],
    ['B', 'p2', 50, 40, -10],
    ['C', 'p3', 50, 40, -10],
  )};
  _playerMerges = [{ from: 'p2', into: 'p1' }, { from: 'p3', into: 'p1' }];
  const eff = _getEffectivePlayers();
  assert(eff.length === 1, 'all merged into one');
  assertClose(eff[0].net, 0, 'single player net should be 0');
  const s = _computeSettlements(eff, []);
  assert(s.length === 0, 'no settlements when everyone merged');
}

// ====== Summary ======

console.log(`\n=============================`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log(`=============================`);
process.exit(failed > 0 ? 1 : 0);
