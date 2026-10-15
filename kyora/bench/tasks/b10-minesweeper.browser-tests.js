/* Kyora Bench b10-minesweeper - acceptance tests owned by the bench.
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
  function revealedMap(s) {
    return s.cells.map(function (r) { return r.map(function (c) { return c.revealed ? (c.adjacent === null ? -1 : c.adjacent) : '.'; }).join(','); });
  }
  function countRevealed(s) {
    var n = 0;
    s.cells.forEach(function (r) { r.forEach(function (c) { if (c.revealed) n++; }); });
    return n;
  }

  T.test('b10-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.reveal', 'input.flag', 'cellCenter',
      'debug.setMines'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b10-t02', 'start() deals a hidden 9x9 board with 10 mines', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.cols).toBe(9);
    expect(s.rows).toBe(9);
    expect(s.mines).toBe(10);
    expect(s.flags).toBe(0);
    expect(s.revealed).toBe(0);
    expect(s.score).toBe(0);
    expect(s.won).toBe(false);
    expect(s.cells.length).toBe(9);
    expect(countRevealed(s)).toBe(0);
    expect(s.cells[0][0].adjacent).toBe(null);
  });

  T.test('b10-t03', 'the first reveal is safe and opens an empty area', async function () {
    fresh(5);
    G().input.reveal(4, 4);
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.cells[4][4].revealed).toBe(true);
    expect(s.cells[4][4].adjacent).toBe(0);
    expect(s.revealed).toBeGreaterThan(1);
    expect(s.revealed).toBe(countRevealed(s));
    expect(s.score).toBe(s.revealed);
  });

  T.test('b10-t04', 'revealing every safe cell wins', async function () {
    fresh(1);
    G().debug.setMines([[0, 0]]);
    G().input.reveal(8, 8);
    var s = S();
    expect(s.revealed).toBe(80);
    expect(s.won).toBe(true);
    expect(s.phase).toBe('over');
  });

  T.test('b10-t05', 'revealing a mine loses', async function () {
    fresh(1);
    G().debug.setMines([[3, 3], [7, 7]]);
    G().input.reveal(3, 3);
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.won).toBe(false);
  });

  T.test('b10-t06', 'numbers count the adjacent mines', async function () {
    fresh(1);
    G().debug.setMines([[0, 0], [2, 0], [1, 2]]);
    G().input.reveal(1, 1);
    G().input.reveal(1, 0);
    G().input.reveal(0, 2);
    var s = S();
    expect(s.cells[1][1].adjacent).toBe(3);
    expect(s.cells[0][1].adjacent).toBe(2);
    expect(s.cells[2][0].adjacent).toBe(1);
    expect(s.revealed).toBe(3);
  });

  T.test('b10-t07', 'flood fill stops at numbered cells', async function () {
    fresh(1);
    var wall = [];
    for (var y = 0; y < 9; y++) wall.push([4, y]);
    G().debug.setMines(wall);
    G().input.reveal(0, 0);
    var s = S();
    expect(s.mines).toBe(9);
    expect(s.revealed).toBe(36);
    expect(s.cells[0][3].adjacent).toBe(2);
    expect(s.cells[4][3].adjacent).toBe(3);
    expect(s.cells[0][5].revealed).toBe(false);
    expect(s.phase).toBe('play');
  });

  T.test('b10-t08', 'flags toggle, are counted and protect the cell', async function () {
    fresh(1);
    G().input.flag(5, 5);
    var s = S();
    expect(s.cells[5][5].flagged).toBe(true);
    expect(s.flags).toBe(1);
    expect(G().input.reveal(5, 5)).toBe(false);
    expect(S().cells[5][5].revealed).toBe(false);
    G().input.flag(5, 5);
    s = S();
    expect(s.cells[5][5].flagged).toBe(false);
    expect(s.flags).toBe(0);
  });

  T.test('b10-t09', 'same seed and same first click give the same board', async function () {
    function run() {
      fresh(8);
      G().input.reveal(4, 4);
      G().input.reveal(0, 8);
      var s = S();
      return { map: revealedMap(s), phase: s.phase };
    }
    expect(run()).toEqual(run());
  });

  T.test('b10-t10', 'clicking a cell reveals it', async function () {
    fresh(2);
    var p = G().cellCenter(4, 4);
    await T.input.click(p.x, p.y);
    expect(S().cells[4][4].revealed).toBe(true);
  });

  T.test('b10-t11', 'a long press flags a cell', async function () {
    fresh(3);
    var p = G().cellCenter(2, 2);
    await T.input.touch(p.x, p.y, undefined, undefined, 700);
    await T.wait(30);
    var c = S().cells[2][2];
    expect(c.flagged).toBe(true);
    expect(c.revealed).toBe(false);
  });

  T.test('b10-t12', 'a short tap reveals a cell', async function () {
    fresh(4);
    var p = G().cellCenter(6, 6);
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().cells[6][6].revealed).toBe(true);
  });

  T.test('b10-t13', 'screenshot of the running game', async function () {
    fresh(6);
    G().input.reveal(4, 4);
    G().input.flag(0, 0);
    await T.wait(50);
    var d = await T.screenshot('b10-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
