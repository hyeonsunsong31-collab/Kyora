/* Kyora Bench b21-tilemap-editor - acceptance tests owned by the bench.
   During measurement this file replaces tests/browser-tests.js. It drives the editor only through the
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
  function count(tiles, pred) {
    var n = 0;
    tiles.forEach(function (r) { r.forEach(function (v) { if (pred(v)) n++; }); });
    return n;
  }
  function paint(tile, cells) {
    G().input.setTool('paint');
    G().input.selectTile(tile);
    cells.forEach(function (c) { G().input.apply(c[0], c[1]); });
  }

  KyoraTest.test('b21-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.setTool', 'input.selectTile', 'input.apply',
      'undo', 'redo', 'exportMap', 'importMap', 'save', 'load', 'cellCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b21-t02', 'start() opens an empty 20x15 map with the paint tool', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.cols).toBe(20);
    expect(s.rows).toBe(15);
    expect(s.tiles.length).toBe(15);
    s.tiles.forEach(function (r) { expect(r.length).toBe(20); });
    expect(count(s.tiles, function (v) { return v !== 0; })).toBe(0);
    expect(s.score).toBe(0);
    expect(s.tool).toBe('paint');
    expect(s.tile).toBe(1);
    expect(s.tileCount).toBeGreaterThanOrEqual(5);
  });

  KyoraTest.test('b21-t03', 'paint and erase change single cells and the score counts painted tiles', async function () {
    fresh(1);
    paint(2, [[3, 4]]);
    var s = S();
    expect(s.tiles[4][3]).toBe(2);
    expect(s.score).toBe(1);
    G().input.setTool('erase');
    G().input.apply(3, 4);
    s = S();
    expect(s.tiles[4][3]).toBe(0);
    expect(s.score).toBe(0);
  });

  KyoraTest.test('b21-t04', 'fill floods the 4-connected region of the clicked value', async function () {
    fresh(1);
    var wall = [];
    for (var y = 0; y < 15; y++) wall.push([5, y]);
    paint(1, wall);
    G().input.selectTile(3);
    G().input.setTool('fill');
    G().input.apply(0, 0);
    var s = S();
    expect(count(s.tiles, function (v) { return v === 3; })).toBe(75);
    expect(s.tiles[14][4]).toBe(3);
    expect(s.tiles[0][6]).toBe(0);
    expect(s.score).toBe(90);
  });

  KyoraTest.test('b21-t05', 'undo and redo step through edits; a fill is one step', async function () {
    fresh(1);
    paint(2, [[1, 1]]);
    expect(G().undo()).toBe(true);
    expect(S().tiles[1][1]).toBe(0);
    expect(G().redo()).toBe(true);
    expect(S().tiles[1][1]).toBe(2);
    G().input.selectTile(4);
    G().input.setTool('fill');
    G().input.apply(10, 10);
    expect(count(S().tiles, function (v) { return v === 4; })).toBe(299);
    expect(G().undo()).toBe(true);
    var s = S();
    expect(count(s.tiles, function (v) { return v === 4; })).toBe(0);
    expect(s.tiles[1][1]).toBe(2);
  });

  KyoraTest.test('b21-t06', 'a new edit clears the redo history', async function () {
    fresh(1);
    paint(2, [[1, 1]]);
    G().undo();
    paint(3, [[2, 2]]);
    expect(G().redo()).toBe(false);
    expect(S().tiles[1][1]).toBe(0);
    expect(S().tiles[2][2]).toBe(3);
  });

  KyoraTest.test('b21-t07', 'exportMap() and importMap() round-trip; bad input is rejected', async function () {
    fresh(1);
    paint(2, [[0, 0], [19, 14], [7, 3]]);
    var json = G().exportMap();
    expect(typeof json).toBe('string');
    var parsed = JSON.parse(json);
    expect(parsed.cols).toBe(20);
    expect(parsed.rows).toBe(15);
    expect(parsed.tiles[3][7]).toBe(2);
    var saved = S().tiles;
    G().start();
    expect(S().score).toBe(0);
    expect(G().importMap(json)).toBe(true);
    expect(S().tiles).toEqual(saved);
    expect(G().importMap('not json')).toBe(false);
    expect(G().importMap('{"cols":2,"rows":2,"tiles":"x"}')).toBe(false);
    expect(S().tiles).toEqual(saved);
  });

  KyoraTest.test('b21-t08', 'save() and load() use localStorage', async function () {
    fresh(1);
    paint(3, [[4, 4], [5, 5]]);
    expect(G().save()).toBe(true);
    expect(localStorage.getItem('kyora-tilemap')).toBeTruthy();
    G().start();
    expect(S().tiles[4][4]).toBe(0);
    expect(G().load()).toBe(true);
    var s = S();
    expect(s.tiles[4][4]).toBe(3);
    expect(s.tiles[5][5]).toBe(3);
  });

  KyoraTest.test('b21-t09', 'clicking a cell paints it', async function () {
    fresh(2);
    G().input.selectTile(2);
    var p = G().cellCenter(7, 7);
    await T.input.click(p.x, p.y);
    await T.wait(30);
    expect(S().tiles[7][7]).toBe(2);
  });

  KyoraTest.test('b21-t10', 'dragging a finger paints along the stroke', async function () {
    fresh(3);
    var a = G().cellCenter(2, 10);
    var b = G().cellCenter(8, 10);
    await T.input.touch(a.x, a.y, b.x, b.y, 300);
    await T.wait(30);
    var s = S();
    expect(s.tiles[10][2]).toBe(1);
    expect(s.tiles[10][8]).toBe(1);
  });

  KyoraTest.test('b21-t11', 'keyboard shortcuts pick tiles and tools', async function () {
    fresh(4);
    await T.input.key('Digit3', 60);
    expect(S().tile).toBe(3);
    await T.input.key('KeyE', 60);
    expect(S().tool).toBe('erase');
    await T.input.key('KeyF', 60);
    expect(S().tool).toBe('fill');
    await T.input.key('KeyB', 60);
    expect(S().tool).toBe('paint');
  });

  KyoraTest.test('b21-t12', 'screenshot of the editor', async function () {
    fresh(5);
    paint(1, [[0, 0], [1, 0], [2, 0], [2, 1], [2, 2]]);
    paint(2, [[10, 10], [11, 10]]);
    await T.wait(50);
    var d = await T.screenshot('b21-edit');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
