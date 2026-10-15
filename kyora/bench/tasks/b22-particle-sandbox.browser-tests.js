/* Kyora Bench b22-particle-sandbox - acceptance tests owned by the bench.
   During measurement this file replaces tests/browser-tests.js. It drives the sandbox only through the
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
  function put(material, x, y, r) {
    G().input.selectMaterial(material);
    G().input.paint(x, y, r || 0);
  }
  function row(s, y, material) {
    var n = 0;
    for (var x = 0; x < s.cols; x++) if (G().getCell(x, y) === material) n++;
    return n;
  }
  function snapshot(s) {
    var out = [];
    for (var y = 0; y < s.rows; y++) {
      var line = '';
      for (var x = 0; x < s.cols; x++) line += ({ empty: '.', sand: 's', water: '~', wall: '#' })[G().getCell(x, y)] || '?';
      out.push(line);
    }
    return out.join('\n');
  }

  KyoraTest.test('b22-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.selectMaterial', 'input.paint',
      'input.togglePause', 'getCell', 'clear', 'cellCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b22-t02', 'start() opens an empty sandbox of at least 80x60 cells', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.cols).toBeGreaterThanOrEqual(80);
    expect(s.rows).toBeGreaterThanOrEqual(60);
    expect(s.counts).toEqual({ sand: 0, water: 0, wall: 0 });
    expect(s.score).toBe(0);
    expect(s.material).toBe('sand');
    expect(s.paused).toBe(false);
    expect(G().getCell(0, 0)).toBe('empty');
  });

  KyoraTest.test('b22-t03', 'sand falls to the floor', async function () {
    var s = fresh(1);
    put('sand', 10, 0);
    expect(G().getCell(10, 0)).toBe('sand');
    G().step(s.rows * 2);
    expect(G().getCell(10, s.rows - 1)).toBe('sand');
    expect(G().getCell(10, 0)).toBe('empty');
    expect(S().counts.sand).toBe(1);
    expect(S().score).toBe(1);
  });

  KyoraTest.test('b22-t04', 'sand piles up and spreads sideways', async function () {
    var s = fresh(1);
    var cx = Math.floor(s.cols / 2);
    for (var i = 0; i < 40; i++) {
      put('sand', cx, 0);
      G().step(3);
    }
    G().step(s.rows * 3);
    var placed = S().counts.sand;
    expect(placed).toBeGreaterThanOrEqual(35);
    expect(G().getCell(cx, s.rows - 1)).toBe('sand');
    expect(row(s, s.rows - 1, 'sand')).toBeGreaterThanOrEqual(3);
    var column = 0;
    for (var y = 0; y < s.rows; y++) if (G().getCell(cx, y) === 'sand') column++;
    expect(column).toBeLessThan(placed);
    G().step(300);
    expect(S().counts.sand).toBe(placed);
  });

  KyoraTest.test('b22-t05', 'walls stop falling sand', async function () {
    var s = fresh(1);
    for (var x = 5; x <= 25; x++) put('wall', x, 30);
    put('sand', 15, 0);
    G().step(s.rows * 2);
    expect(G().getCell(15, 29)).toBe('sand');
    expect(G().getCell(15, s.rows - 1)).toBe('empty');
    expect(S().counts.wall).toBe(21);
  });

  KyoraTest.test('b22-t06', 'water flows sideways into a flat layer', async function () {
    var s = fresh(1);
    var cx = Math.floor(s.cols / 2);
    for (var i = 0; i < 30; i++) {
      put('water', cx, 0);
      G().step(3);
    }
    G().step(600);
    var water = S().counts.water;
    expect(water).toBeGreaterThanOrEqual(25);
    expect(row(s, s.rows - 1, 'water')).toBeGreaterThanOrEqual(Math.min(water, 20));
    var stacked = 0;
    for (var y = 0; y < s.rows - 5; y++) if (G().getCell(cx, y) === 'water') stacked++;
    expect(stacked).toBe(0);
  });

  KyoraTest.test('b22-t07', 'sand sinks below water', async function () {
    var s = fresh(1);
    var cx = Math.floor(s.cols / 2);
    for (var x = 0; x < s.cols; x++) put('water', x, s.rows - 1);
    var water = S().counts.water;
    expect(water).toBe(s.cols);
    put('sand', cx, 0);
    G().step(s.rows * 3);
    expect(G().getCell(cx, s.rows - 1)).toBe('sand');
    expect(S().counts.water).toBe(water);
    expect(S().counts.sand).toBe(1);
  });

  KyoraTest.test('b22-t08', 'the simulation neither creates nor destroys particles', async function () {
    var s = fresh(2);
    put('sand', 20, 5, 3);
    put('water', 40, 5, 3);
    put('sand', 60, 10, 2);
    put('wall', 30, 40, 2);
    var c0 = S().counts;
    G().step(400);
    expect(S().counts).toEqual(c0);
    expect(S().score).toBe(c0.sand + c0.water + c0.wall);
  });

  KyoraTest.test('b22-t09', 'pause freezes the simulation', async function () {
    var s = fresh(1);
    G().input.togglePause();
    expect(S().paused).toBe(true);
    put('sand', 12, 0);
    G().step(50);
    expect(G().getCell(12, 0)).toBe('sand');
    G().input.togglePause();
    expect(S().paused).toBe(false);
    G().step(s.rows * 2);
    expect(G().getCell(12, 0)).toBe('empty');
  });

  KyoraTest.test('b22-t10', 'clear() empties the sandbox', async function () {
    fresh(1);
    put('sand', 20, 20, 3);
    put('wall', 30, 30, 2);
    G().clear();
    var s = S();
    expect(s.counts).toEqual({ sand: 0, water: 0, wall: 0 });
    expect(s.score).toBe(0);
    expect(G().getCell(20, 20)).toBe('empty');
  });

  KyoraTest.test('b22-t11', 'same seed and same strokes give the same sandbox', async function () {
    function run() {
      var s = fresh(9);
      put('sand', 30, 2, 4);
      put('water', 34, 8, 4);
      put('wall', 32, 40, 1);
      G().step(250);
      return snapshot(s);
    }
    expect(run()).toBe(run());
  });

  KyoraTest.test('b22-t12', 'clicking and dragging on the canvas paints immediately', async function () {
    fresh(3);
    var p = G().cellCenter(20, 20);
    await T.input.click(p.x, p.y);
    expect(G().getCell(20, 20)).toBe('sand');
    G().input.selectMaterial('wall');
    var a = G().cellCenter(10, 50);
    var b = G().cellCenter(30, 50);
    await T.input.touch(a.x, a.y, b.x, b.y, 250);
    expect(G().getCell(10, 50)).toBe('wall');
    expect(G().getCell(30, 50)).toBe('wall');
  });

  KyoraTest.test('b22-t13', 'number keys pick materials and Space pauses', async function () {
    fresh(4);
    await T.input.key('Digit2', 60);
    expect(S().material).toBe('water');
    await T.input.key('Digit3', 60);
    expect(S().material).toBe('wall');
    await T.input.key('Space', 60);
    expect(S().paused).toBe(true);
  });

  KyoraTest.test('b22-t14', 'screenshot of the sandbox', async function () {
    var s = fresh(5);
    put('wall', 40, 45, 2);
    put('sand', 40, 5, 4);
    put('water', 50, 5, 4);
    G().step(120);
    await T.wait(50);
    var d = await T.screenshot('b22-sandbox');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
    expect(s.cols).toBeGreaterThan(0);
  });
})();
