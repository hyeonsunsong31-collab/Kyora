/* Kyora Bench b07-2048 - acceptance tests owned by the bench.
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
  function box() { var r = canvas().getBoundingClientRect(); return { w: r.width, h: r.height }; }
  function missing(root, names) {
    return names.filter(function (path) {
      var cur = root;
      path.split('.').forEach(function (k) { cur = cur == null ? undefined : cur[k]; });
      return typeof cur !== 'function';
    });
  }
  function grid(row0) {
    var g = [row0.slice(), [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]];
    return g;
  }
  function tiles(g) {
    var n = 0;
    g.forEach(function (r) { r.forEach(function (v) { if (v) n++; }); });
    return n;
  }
  function board(row0, seed) {
    fresh(seed === undefined ? 1 : seed);
    G().debug.setGrid(grid(row0));
    return S();
  }

  T.test('b07-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.move', 'debug.setGrid'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b07-t02', 'start() deals two tiles of 2 or 4 on a 4x4 board', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.moves).toBe(0);
    expect(s.won).toBe(false);
    expect(s.grid.length).toBe(4);
    s.grid.forEach(function (r) { expect(r.length).toBe(4); });
    expect(tiles(s.grid)).toBe(2);
    s.grid.forEach(function (r) { r.forEach(function (v) { expect([0, 2, 4]).toContain(v); }); });
  });

  T.test('b07-t03', 'moving left merges a pair, scores it and spawns one tile', async function () {
    board([2, 2, 0, 0]);
    expect(G().input.move('left')).toBe(true);
    var s = S();
    expect(s.grid[0][0]).toBe(4);
    expect(s.score).toBe(4);
    expect(s.moves).toBe(1);
    expect(tiles(s.grid)).toBe(2);
  });

  T.test('b07-t04', 'each tile merges at most once per move', async function () {
    board([2, 2, 2, 2]);
    G().input.move('left');
    var s = S();
    expect(s.grid[0][0]).toBe(4);
    expect(s.grid[0][1]).toBe(4);
    expect(s.score).toBe(8);
    board([4, 4, 8, 0]);
    G().input.move('left');
    s = S();
    expect(s.grid[0][0]).toBe(8);
    expect(s.grid[0][1]).toBe(8);
  });

  T.test('b07-t05', 'merges happen from the side the tiles move toward', async function () {
    board([2, 2, 2, 0]);
    G().input.move('left');
    var s = S();
    expect(s.grid[0][0]).toBe(4);
    expect(s.grid[0][1]).toBe(2);
    board([0, 2, 2, 2]);
    G().input.move('right');
    s = S();
    expect(s.grid[0][3]).toBe(4);
    expect(s.grid[0][2]).toBe(2);
    board([2, 0, 0, 2]);
    G().input.move('right');
    expect(S().grid[0][3]).toBe(4);
  });

  T.test('b07-t06', 'vertical moves merge columns (row 0 is the top)', async function () {
    fresh(1);
    G().debug.setGrid([[2, 0, 0, 0], [2, 0, 0, 0], [4, 0, 0, 0], [0, 0, 0, 0]]);
    G().input.move('up');
    var s = S();
    expect(s.grid[0][0]).toBe(4);
    expect(s.grid[1][0]).toBe(4);
    G().debug.setGrid([[2, 0, 0, 0], [2, 0, 0, 0], [4, 0, 0, 0], [0, 0, 0, 0]]);
    G().input.move('down');
    s = S();
    expect(s.grid[3][0]).toBe(4);
    expect(s.grid[2][0]).toBe(4);
  });

  T.test('b07-t07', 'a move that changes nothing returns false and spawns nothing', async function () {
    var before = board([2, 4, 2, 4]);
    expect(G().input.move('left')).toBe(false);
    var s = S();
    expect(s.grid).toEqual(before.grid);
    expect(s.moves).toBe(before.moves);
    expect(s.score).toBe(before.score);
  });

  T.test('b07-t08', 'a full board without merges is game over', async function () {
    fresh(1);
    G().debug.setGrid([[2, 4, 2, 4], [4, 2, 4, 2], [2, 4, 2, 4], [4, 2, 4, 2]]);
    expect(G().input.move('left')).toBe(false);
    expect(S().phase).toBe('over');
  });

  T.test('b07-t09', 'making 2048 sets won and play continues', async function () {
    board([1024, 1024, 0, 0]);
    G().input.move('left');
    var s = S();
    expect(s.grid[0][0]).toBe(2048);
    expect(s.won).toBe(true);
    expect(s.phase).toBe('play');
  });

  T.test('b07-t10', 'same seed gives the same deal and the same spawns', async function () {
    function run() {
      var a = fresh(21).grid;
      G().input.move('left');
      G().input.move('up');
      return { a: a, b: S().grid };
    }
    expect(run()).toEqual(run());
  });

  T.test('b07-t11', 'arrow keys move the tiles', async function () {
    board([2, 2, 0, 0], 3);
    await T.input.key('ArrowLeft', 60);
    var s = S();
    expect(s.grid[0][0]).toBeGreaterThanOrEqual(4);
    expect(s.score).toBeGreaterThanOrEqual(4);
  });

  T.test('b07-t12', 'a swipe on the canvas moves the tiles', async function () {
    board([0, 0, 2, 2], 4);
    var b = box();
    await T.input.touch(b.w * 0.75, b.h / 2, b.w * 0.25, b.h / 2, 150);
    var s = S();
    expect(s.grid[0][0]).toBeGreaterThanOrEqual(4);
    expect(s.score).toBeGreaterThanOrEqual(4);
  });

  T.test('b07-t13', 'screenshot of the running game', async function () {
    board([2, 4, 8, 16], 5);
    G().input.move('down');
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b07-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
