/* Kyora Bench b16-tower-defense-lite - acceptance tests owned by the bench.
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
  function onPath(s, x, y) { return s.path.some(function (p) { return p.x === x && p.y === y; }); }
  function freeCells(s) {
    var out = [];
    for (var y = 0; y < s.rows; y++) {
      for (var x = 0; x < s.cols; x++) {
        if (!onPath(s, x, y) && !s.towers.some(function (t) { return t.x === x && t.y === y; })) out.push({ x: x, y: y });
      }
    }
    return out;
  }
  // free cells orthogonally next to the first path cells (near the entry)
  function cellsNearEntry(s, n) {
    var out = [];
    for (var i = 1; i < s.path.length && out.length < n; i++) {
      var p = s.path[i];
      [[1, 0], [-1, 0], [0, 1], [0, -1]].forEach(function (d) {
        var x = p.x + d[0], y = p.y + d[1];
        if (out.length >= n || x < 0 || y < 0 || x >= s.cols || y >= s.rows || onPath(s, x, y)) return;
        if (out.some(function (c) { return c.x === x && c.y === y; })) return;
        out.push({ x: x, y: y });
      });
    }
    return out;
  }
  function stepUntil(pred, maxTicks) {
    for (var i = 0; i < maxTicks; i += 10) {
      if (pred(S())) return true;
      G().step(10);
    }
    return pred(S());
  }

  KyoraTest.test('b16-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.placeTower', 'input.startWave',
      'cellCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b16-t02', 'start() sets up gold, lives and a continuous edge-to-edge path', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.gold).toBe(100);
    expect(s.lives).toBe(10);
    expect(s.wave).toBe(0);
    expect(s.waves).toBe(10);
    expect(s.waveActive).toBe(false);
    expect(s.towers.length).toBe(0);
    expect(s.cols).toBeGreaterThanOrEqual(12);
    expect(s.rows).toBeGreaterThanOrEqual(8);
    expect(s.path.length).toBeGreaterThanOrEqual(16);
    expect(s.path.length).toBeLessThanOrEqual(60);
    for (var i = 1; i < s.path.length; i++) {
      var d = Math.abs(s.path[i].x - s.path[i - 1].x) + Math.abs(s.path[i].y - s.path[i - 1].y);
      expect(d).toBe(1);
    }
    function edge(p) { return p.x === 0 || p.y === 0 || p.x === s.cols - 1 || p.y === s.rows - 1; }
    expect(edge(s.path[0])).toBe(true);
    expect(edge(s.path[s.path.length - 1])).toBe(true);
  });

  KyoraTest.test('b16-t03', 'towers cost 50 gold and only go on free cells', async function () {
    var s = fresh(1);
    expect(G().input.placeTower(s.path[3].x, s.path[3].y)).toBe(false);
    var free = freeCells(s);
    expect(G().input.placeTower(free[0].x, free[0].y)).toBe(true);
    expect(S().gold).toBe(50);
    expect(G().input.placeTower(free[0].x, free[0].y)).toBe(false);
    expect(G().input.placeTower(free[1].x, free[1].y)).toBe(true);
    expect(S().gold).toBe(0);
    expect(G().input.placeTower(free[2].x, free[2].y)).toBe(false);
    expect(S().towers.length).toBe(2);
  });

  KyoraTest.test('b16-t04', 'startWave() sends enemies and cannot overlap a running wave', async function () {
    fresh(1);
    expect(G().input.startWave()).toBe(true);
    var s = S();
    expect(s.wave).toBe(1);
    expect(s.waveActive).toBe(true);
    G().step(120);
    expect(S().enemies.length).toBeGreaterThan(0);
    expect(G().input.startWave()).toBe(false);
    expect(S().wave).toBe(1);
  });

  KyoraTest.test('b16-t05', 'enemies walk along the path', async function () {
    fresh(1);
    G().input.startWave();
    for (var k = 0; k < 6; k++) {
      G().step(100);
      var s = S();
      s.enemies.forEach(function (e) {
        var best = Infinity;
        s.path.forEach(function (p) { best = Math.min(best, Math.hypot(e.x - (p.x + 0.5), e.y - (p.y + 0.5))); });
        expect(best).toBeLessThanOrEqual(0.75);
      });
    }
  });

  KyoraTest.test('b16-t06', 'enemies that reach the exit cost lives', async function () {
    fresh(1);
    G().input.startWave();
    var done = stepUntil(function (x) { return !x.waveActive; }, 6000);
    expect(done).toBe(true);
    var s = S();
    expect(s.lives).toBeLessThan(10);
    expect(s.enemies.length).toBe(0);
  });

  KyoraTest.test('b16-t07', 'towers next to the path kill enemies for score and gold', async function () {
    var s = fresh(2);
    var spots = cellsNearEntry(s, 2);
    expect(spots.length).toBe(2);
    spots.forEach(function (c) { expect(G().input.placeTower(c.x, c.y)).toBe(true); });
    G().input.startWave();
    stepUntil(function (x) { return !x.waveActive; }, 6000);
    var a = S();
    expect(a.score).toBeGreaterThan(0);
    expect(a.gold).toBeGreaterThan(0);
  });

  KyoraTest.test('b16-t08', 'losing all lives ends the game', async function () {
    fresh(3);
    for (var w = 0; w < 10 && S().phase === 'play'; w++) {
      G().input.startWave();
      stepUntil(function (x) { return !x.waveActive || x.phase !== 'play'; }, 6000);
    }
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.lives).toBe(0);
    expect(s.won).toBe(false);
  });

  KyoraTest.test('b16-t09', 'clicking a free cell places a tower', async function () {
    var s = fresh(4);
    var c = freeCells(s)[5];
    var p = G().cellCenter(c.x, c.y);
    await T.input.click(p.x, p.y);
    await T.wait(30);
    var a = S();
    expect(a.towers.length).toBe(1);
    expect([a.towers[0].x, a.towers[0].y]).toEqual([c.x, c.y]);
    expect(a.gold).toBe(50);
  });

  KyoraTest.test('b16-t10', 'tapping a free cell places a tower', async function () {
    var s = fresh(5);
    var c = freeCells(s)[7];
    var p = G().cellCenter(c.x, c.y);
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().towers.length).toBe(1);
  });

  KyoraTest.test('b16-t11', 'Space starts the next wave', async function () {
    fresh(6);
    await T.input.key('Space', 60);
    expect(S().wave).toBe(1);
    expect(S().waveActive).toBe(true);
  });

  KyoraTest.test('b16-t12', 'same seed and same towers give the same battle', async function () {
    function run() {
      var s = fresh(7);
      var spots = cellsNearEntry(s, 1);
      G().input.placeTower(spots[0].x, spots[0].y);
      G().input.startWave();
      G().step(900);
      var a = S();
      return {
        score: a.score, gold: a.gold, lives: a.lives,
        enemies: a.enemies.map(function (e) { return [Math.round(e.x * 1000) / 1000, Math.round(e.y * 1000) / 1000, e.hp]; })
      };
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b16-t13', 'screenshot of the running game', async function () {
    var s = fresh(8);
    var spots = cellsNearEntry(s, 1);
    G().input.placeTower(spots[0].x, spots[0].y);
    G().input.startWave();
    G().step(300);
    await T.wait(50);
    var d = await T.screenshot('b16-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
