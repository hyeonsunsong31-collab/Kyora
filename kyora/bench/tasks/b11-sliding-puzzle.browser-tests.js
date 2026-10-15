/* Kyora Bench b11-sliding-puzzle - acceptance tests owned by the bench.
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
  var SOLVED = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 0];
  var ONE_LEFT = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0, 15];
  var ONE_UP = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0, 13, 14, 15, 12];
  function isPermutation(t) {
    return t.length === 16 && t.slice().sort(function (a, b) { return a - b; }).every(function (v, i) { return v === i; });
  }
  // 4x4 solvability: (inversions + row of the blank counted from the bottom, 1-based) must be odd
  function solvable(t) {
    var inv = 0;
    var flat = t.filter(function (v) { return v !== 0; });
    for (var i = 0; i < flat.length; i++) for (var j = i + 1; j < flat.length; j++) if (flat[i] > flat[j]) inv++;
    var blankRowFromBottom = 4 - Math.floor(t.indexOf(0) / 4);
    return (inv + blankRowFromBottom) % 2 === 1;
  }
  function setup(tiles) {
    fresh(1);
    G().debug.setTiles(tiles.slice());
    return S();
  }

  KyoraTest.test('b11-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.slide', 'input.tap', 'cellCenter',
      'debug.setTiles'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b11-t02', 'start() deals a shuffled, solvable, unsolved 4x4 puzzle', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.size).toBe(4);
    expect(s.moves).toBe(0);
    expect(s.solved).toBe(false);
    expect(isPermutation(s.tiles)).toBe(true);
    expect(solvable(s.tiles)).toBe(true);
  });

  KyoraTest.test('b11-t03', 'every seed gives a solvable shuffle that is not already solved', async function () {
    for (var seed = 2; seed <= 7; seed++) {
      var s = fresh(seed);
      expect(isPermutation(s.tiles)).toBe(true);
      expect(solvable(s.tiles)).toBe(true);
      expect(s.tiles.join(',') === SOLVED.join(',')).toBe(false);
    }
  });

  KyoraTest.test('b11-t04', "slide('left') moves the tile right of the blank and can solve the puzzle", async function () {
    setup(ONE_LEFT);
    expect(G().input.slide('left')).toBe(true);
    var s = S();
    expect(s.tiles).toEqual(SOLVED);
    expect(s.moves).toBe(1);
    expect(s.solved).toBe(true);
    expect(s.phase).toBe('over');
    expect(s.score).toBeGreaterThan(0);
  });

  KyoraTest.test('b11-t05', "slide('up') moves the tile below the blank up", async function () {
    setup(ONE_UP);
    expect(G().input.slide('up')).toBe(true);
    expect(S().tiles).toEqual(SOLVED);
    expect(S().solved).toBe(true);
  });

  KyoraTest.test('b11-t06', 'a slide with no tile to move returns false', async function () {
    setup(ONE_LEFT);
    expect(G().input.slide('up')).toBe(false);
    var s = S();
    expect(s.tiles).toEqual(ONE_LEFT);
    expect(s.moves).toBe(0);
  });

  KyoraTest.test('b11-t07', 'slides in both directions count as moves', async function () {
    setup(ONE_LEFT);
    expect(G().input.slide('right')).toBe(true);
    var s = S();
    expect(s.tiles.indexOf(0)).toBe(13);
    expect(s.tiles[14]).toBe(14);
    expect(s.moves).toBe(1);
    expect(G().input.slide('left')).toBe(true);
    expect(S().tiles).toEqual(ONE_LEFT);
    expect(S().moves).toBe(2);
  });

  KyoraTest.test('b11-t08', 'tap() moves a tile next to the blank only', async function () {
    setup(ONE_LEFT);
    expect(G().input.tap(0, 0)).toBe(false);
    expect(S().moves).toBe(0);
    expect(G().input.tap(3, 3)).toBe(true);
    expect(S().solved).toBe(true);
  });

  KyoraTest.test('b11-t09', 'same seed gives the same shuffle', async function () {
    expect(fresh(33).tiles).toEqual(fresh(33).tiles);
  });

  KyoraTest.test('b11-t10', 'arrow keys slide tiles', async function () {
    setup(ONE_LEFT);
    await T.input.key('ArrowLeft', 60);
    var s = S();
    expect(s.solved).toBe(true);
    expect(s.phase).toBe('over');
  });

  KyoraTest.test('b11-t11', 'tapping a tile on the canvas slides it', async function () {
    setup(ONE_UP);
    var p = G().cellCenter(3, 3);
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().solved).toBe(true);
  });

  KyoraTest.test('b11-t12', 'clicking a tile slides it', async function () {
    setup(ONE_LEFT);
    var p = G().cellCenter(3, 3);
    await T.input.click(p.x, p.y);
    await T.wait(30);
    expect(S().solved).toBe(true);
  });

  KyoraTest.test('b11-t13', 'start() after solving deals a new puzzle; screenshot', async function () {
    setup(ONE_LEFT);
    G().input.slide('left');
    expect(S().phase).toBe('over');
    G().start();
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.solved).toBe(false);
    expect(s.moves).toBe(0);
    await T.wait(50);
    var d = await T.screenshot('b11-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
