/* Kyora Bench b19-text-adventure - acceptance tests owned by the bench.
   During measurement this file replaces tests/browser-tests.js. It drives the game only through the
   window.Game contract from the task brief (seeded, manual clock via Game.step) and never reads pixels. */
(function () {
  'use strict';
  var T = window.KyoraTest;
  var expect = T.expect;

  function G() { return window.Game; }
  function S() { return window.Game.getState(); }
  function fresh(seed) {
    var g = window.Game;
    g.step(0);
    g.setSeed(seed === undefined ? 1 : seed);
    g.start();
    return g.getState();
  }
  function canvas() { return document.querySelector('canvas'); }
  function missing(root, names) {
    return names.filter(function (path) {
      var cur = root;
      path.split('.').forEach(function (k) { cur = cur == null ? undefined : cur[k]; });
      return typeof cur !== 'function';
    });
  }
  function cmd(text) { return G().command(text); }
  function has(text, word) { return String(text).toLowerCase().indexOf(word) >= 0; }

  T.test('b19-t01', 'canvas, command input and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(document.getElementById('cmd')).toBeTruthy();
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'command', 'compassCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b19-t02', 'start() begins in the cabin with an empty inventory', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.room).toBe('cabin');
    expect(s.inventory).toEqual([]);
    expect(s.score).toBe(0);
    expect(s.moves).toBe(0);
    expect(s.lampLit).toBe(false);
    expect(typeof s.output).toBe('string');
    expect(s.output.length).toBeGreaterThan(0);
  });

  T.test('b19-t03', 'look describes the room and its items', async function () {
    fresh(1);
    var out = cmd('look');
    expect(typeof out).toBe('string');
    expect(has(out, 'lamp')).toBe(true);
    expect(S().output).toBe(out);
    expect(S().moves).toBe(1);
  });

  T.test('b19-t04', 'take moves an item into the inventory once and scores', async function () {
    fresh(1);
    cmd('take lamp');
    var s = S();
    expect(s.inventory).toEqual(['lamp']);
    expect(s.score).toBe(10);
    cmd('take lamp');
    expect(S().inventory).toEqual(['lamp']);
    expect(S().score).toBe(10);
  });

  T.test('b19-t05', 'movement accepts go north, north and n', async function () {
    fresh(1);
    cmd('go north');
    expect(S().room).toBe('forest');
    cmd('s');
    expect(S().room).toBe('cabin');
    cmd('north');
    expect(S().room).toBe('forest');
    cmd('e');
    expect(S().room).toBe('river');
    cmd('go west');
    expect(S().room).toBe('forest');
    expect(S().visited.indexOf('river')).toBeGreaterThanOrEqual(0);
  });

  T.test('b19-t06', 'blocked exits and unknown commands leave the state unchanged', async function () {
    fresh(1);
    var out = cmd('go west');
    expect(S().room).toBe('cabin');
    expect(out.length).toBeGreaterThan(0);
    var out2 = cmd('dance wildly');
    expect(out2.length).toBeGreaterThan(0);
    var s = S();
    expect(s.room).toBe('cabin');
    expect(s.inventory).toEqual([]);
    expect(s.phase).toBe('play');
  });

  T.test('b19-t07', 'the cave is too dark to open the chest without a lit lamp', async function () {
    fresh(1);
    cmd('take lamp');
    cmd('n');
    cmd('n');
    expect(S().room).toBe('cave');
    expect(has(cmd('look'), 'dark')).toBe(true);
    cmd('open chest');
    cmd('take gem');
    expect(S().inventory.indexOf('gem')).toBe(-1);
  });

  T.test('b19-t08', 'the chest needs the key even with light', async function () {
    fresh(1);
    cmd('take lamp');
    cmd('use lamp');
    expect(S().lampLit).toBe(true);
    cmd('n');
    cmd('n');
    cmd('open chest');
    cmd('take gem');
    expect(S().inventory.indexOf('gem')).toBe(-1);
  });

  T.test('b19-t09', 'the full walkthrough wins', async function () {
    fresh(1);
    ['take lamp', 'n', 'e', 'take key', 'w', 'n', 'use lamp', 'open chest', 'take gem', 's', 's'].forEach(cmd);
    var s = S();
    expect(s.room).toBe('cabin');
    expect(s.won).toBe(true);
    expect(s.phase).toBe('over');
    expect(s.score).toBe(80);
  });

  T.test('b19-t10', 'inventory, i and drop', async function () {
    fresh(1);
    cmd('take lamp');
    expect(has(cmd('inventory'), 'lamp')).toBe(true);
    expect(has(cmd('i'), 'lamp')).toBe(true);
    cmd('drop lamp');
    expect(S().inventory).toEqual([]);
    expect(has(cmd('look'), 'lamp')).toBe(true);
  });

  T.test('b19-t11', 'typing into #cmd and pressing Enter runs the command', async function () {
    fresh(2);
    var input = document.getElementById('cmd');
    input.value = 'go north';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
    await T.wait(50);
    expect(S().room).toBe('forest');
  });

  T.test('b19-t12', 'tapping the compass on the canvas moves', async function () {
    fresh(3);
    var p = G().compassCenter('n');
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().room).toBe('forest');
    var q = G().compassCenter('s');
    await T.input.click(q.x, q.y);
    await T.wait(30);
    expect(S().room).toBe('cabin');
  });

  T.test('b19-t13', 'start() after winning begins again; screenshot', async function () {
    fresh(4);
    ['take lamp', 'n', 'e', 'take key', 'w', 'n', 'use lamp', 'open chest', 'take gem', 's', 's'].forEach(cmd);
    expect(S().phase).toBe('over');
    G().start();
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.room).toBe('cabin');
    expect(s.inventory).toEqual([]);
    await T.wait(50);
    var d = await T.screenshot('b19-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
