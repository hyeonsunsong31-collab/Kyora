/* Kyora Bench b08-match-3 - acceptance tests owned by the bench.
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
  function copy(g) { return g.map(function (r) { return r.slice(); }); }
  // true when the board contains a horizontal or vertical line of 3+ equal gems
  function hasMatch(g) {
    for (var y = 0; y < g.length; y++) {
      for (var x = 0; x < g[y].length; x++) {
        var v = g[y][x];
        if (x + 2 < g[y].length && g[y][x + 1] === v && g[y][x + 2] === v) return true;
        if (y + 2 < g.length && g[y + 1][x] === v && g[y + 2][x] === v) return true;
      }
    }
    return false;
  }
  function swapped(g, x1, y1, x2, y2) {
    var c = copy(g);
    var t = c[y1][x1]; c[y1][x1] = c[y2][x2]; c[y2][x2] = t;
    return c;
  }
  function findSwap(g, wantValid) {
    for (var y = 0; y < g.length; y++) {
      for (var x = 0; x < g[y].length; x++) {
        var opts = [[x + 1, y], [x, y + 1]];
        for (var i = 0; i < opts.length; i++) {
          var nx = opts[i][0], ny = opts[i][1];
          if (ny >= g.length || nx >= g[0].length) continue;
          if (hasMatch(swapped(g, x, y, nx, ny)) === wantValid) return [x, y, nx, ny];
        }
      }
    }
    return null;
  }
  // 8x8 board without any line of three; (x + 2y) mod 6 never repeats three times in a row or column
  function base() {
    var g = [];
    for (var y = 0; y < 8; y++) {
      g.push([]);
      for (var x = 0; x < 8; x++) g[y].push((x + 2 * y) % 6);
    }
    return g;
  }
  // horizontal setup: swapping (2,0) with (2,1) completes row 0 columns 0-2
  function horizontalBoard() {
    var g = base();
    var a = g[1][2];
    g[0][0] = a; g[0][1] = a;
    return g;
  }
  // vertical setup: swapping (0,5) with (1,5) completes column 0 rows 5-7
  function verticalBoard() {
    var g = base();
    var a = g[5][1];
    g[6][0] = a; g[7][0] = a;
    return g;
  }
  // four in a row: swapping (2,3) with (2,2) completes row 3 columns 0-3
  function fourBoard() {
    var g = base();
    var a = g[2][2];
    g[3][0] = a; g[3][1] = a; g[3][3] = a;
    return g;
  }
  function settled(s) {
    var full = s.grid.every(function (r) { return r.every(function (v) { return typeof v === 'number' && v >= 0 && v <= 5; }); });
    return full && !hasMatch(s.grid) && s.busy === false;
  }
  function useBoard(g, seed) {
    fresh(seed === undefined ? 1 : seed);
    expect(hasMatch(g)).toBe(false);
    G().debug.setGrid(g);
    return S();
  }

  KyoraTest.test('b08-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.swap', 'input.select', 'cellCenter',
      'debug.setGrid', 'debug.setMovesLeft'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b08-t02', 'start() deals an 8x8 board of 6 gem types with no matches and a valid move', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.movesLeft).toBe(30);
    expect(s.cols).toBe(8);
    expect(s.rows).toBe(8);
    expect(s.grid.length).toBe(8);
    s.grid.forEach(function (r) {
      expect(r.length).toBe(8);
      r.forEach(function (v) { expect([0, 1, 2, 3, 4, 5]).toContain(v); });
    });
    expect(hasMatch(s.grid)).toBe(false);
    expect(findSwap(s.grid, true)).toBeTruthy();
  });

  KyoraTest.test('b08-t03', 'a matching swap scores, costs a move and leaves a settled board', async function () {
    useBoard(horizontalBoard());
    expect(G().input.swap(2, 0, 2, 1)).toBe(true);
    G().step(60);
    var s = S();
    expect(s.score).toBeGreaterThanOrEqual(30);
    expect(s.movesLeft).toBe(29);
    expect(settled(s)).toBe(true);
  });

  KyoraTest.test('b08-t04', 'a swap that makes no line is undone and costs nothing', async function () {
    var g = base();
    var before = useBoard(g);
    var sw = findSwap(g, false);
    expect(sw).toBeTruthy();
    expect(G().input.swap(sw[0], sw[1], sw[2], sw[3])).toBe(false);
    G().step(60);
    var s = S();
    expect(s.grid).toEqual(before.grid);
    expect(s.movesLeft).toBe(30);
    expect(s.score).toBe(0);
  });

  KyoraTest.test('b08-t05', 'only orthogonally adjacent gems can be swapped', async function () {
    var before = useBoard(horizontalBoard());
    expect(G().input.swap(2, 0, 4, 0)).toBe(false);
    expect(G().input.swap(1, 0, 2, 1)).toBe(false);
    G().step(60);
    expect(S().grid).toEqual(before.grid);
    expect(S().movesLeft).toBe(30);
  });

  KyoraTest.test('b08-t06', 'vertical lines count too', async function () {
    useBoard(verticalBoard());
    expect(G().input.swap(0, 5, 1, 5)).toBe(true);
    G().step(60);
    var s = S();
    expect(s.score).toBeGreaterThanOrEqual(30);
    expect(settled(s)).toBe(true);
  });

  KyoraTest.test('b08-t07', 'a line of four scores at least 40', async function () {
    useBoard(fourBoard());
    expect(G().input.swap(2, 3, 2, 2)).toBe(true);
    G().step(60);
    var s = S();
    expect(s.score).toBeGreaterThanOrEqual(40);
    expect(settled(s)).toBe(true);
  });

  KyoraTest.test('b08-t08', 'running out of moves ends the game', async function () {
    useBoard(horizontalBoard());
    G().debug.setMovesLeft(1);
    expect(G().input.swap(2, 0, 2, 1)).toBe(true);
    G().step(60);
    var s = S();
    expect(s.movesLeft).toBe(0);
    expect(s.phase).toBe('over');
  });

  KyoraTest.test('b08-t09', 'same seed gives the same board and the same refills', async function () {
    function run() {
      var s = fresh(4);
      var sw = findSwap(s.grid, true);
      G().input.swap(sw[0], sw[1], sw[2], sw[3]);
      G().step(60);
      return { start: s.grid, after: S().grid, score: S().score };
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b08-t10', 'select() marks a gem and selecting a neighbour swaps', async function () {
    useBoard(horizontalBoard());
    G().input.select(2, 0);
    expect(S().selected).toEqual({ x: 2, y: 0 });
    G().input.select(2, 1);
    G().step(60);
    var s = S();
    expect(s.score).toBeGreaterThanOrEqual(30);
    expect(s.selected).toBe(null);
  });

  KyoraTest.test('b08-t11', 'clicking two adjacent gems swaps them', async function () {
    useBoard(horizontalBoard());
    var a = G().cellCenter(2, 0);
    var b = G().cellCenter(2, 1);
    await T.input.click(a.x, a.y);
    await T.wait(30);
    await T.input.click(b.x, b.y);
    G().step(60);
    expect(S().score).toBeGreaterThanOrEqual(30);
  });

  KyoraTest.test('b08-t12', 'tapping two adjacent gems swaps them', async function () {
    useBoard(verticalBoard());
    var a = G().cellCenter(0, 5);
    var b = G().cellCenter(1, 5);
    await T.input.touch(a.x, a.y);
    await T.wait(30);
    await T.input.touch(b.x, b.y);
    G().step(60);
    expect(S().score).toBeGreaterThanOrEqual(30);
  });

  KyoraTest.test('b08-t13', 'screenshot of the running game', async function () {
    fresh(5);
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b08-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
