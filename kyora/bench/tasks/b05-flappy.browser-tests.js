/* Kyora Bench b05-flappy - acceptance tests owned by the bench.
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
  function collectPipes(ticks) {
    var seen = {};
    var list = [];
    for (var i = 0; i < ticks; i += 10) {
      G().step(10);
      S().pipes.forEach(function (p) {
        var key = Math.round(p.gapY * 1000) + ':' + Math.round(p.gapH);
        if (!seen[key]) { seen[key] = true; list.push(p); }
      });
    }
    return list;
  }

  T.test('b05-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.flap', 'debug.setInvincible'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b05-t02', 'start() puts the bird in the air with score 0', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.width).toBe(400);
    expect(s.height).toBe(640);
    expect(s.birdY).toBeGreaterThan(0);
    expect(s.birdY).toBeLessThan(s.groundY);
  });

  T.test('b05-t03', 'gravity pulls the bird down', async function () {
    var s = fresh(1);
    G().step(20);
    var a = S();
    expect(a.birdVY).toBeGreaterThan(0);
    expect(a.birdY).toBeGreaterThan(s.birdY);
  });

  T.test('b05-t04', 'flap() gives an immediate upward velocity', async function () {
    fresh(1);
    G().step(20);
    var before = S();
    G().input.flap();
    expect(S().birdVY).toBeLessThan(0);
    G().step(5);
    expect(S().birdY).toBeLessThan(before.birdY);
  });

  T.test('b05-t05', 'hitting the ground ends the game', async function () {
    fresh(1);
    G().step(600);
    expect(S().phase).toBe('over');
  });

  T.test('b05-t06', 'pipes enter from the right and scroll left', async function () {
    fresh(2);
    G().debug.setInvincible(true);
    G().step(240);
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.pipes.length).toBeGreaterThan(0);
    var p = s.pipes[s.pipes.length - 1];
    G().step(5);
    var same = S().pipes.filter(function (q) { return q.gapY === p.gapY && q.gapH === p.gapH; })[0];
    expect(same).toBeDefined();
    expect(same.x).toBeLessThan(p.x);
  });

  T.test('b05-t07', 'pipe gaps follow the brief (centre in the middle half, 140-200 tall)', async function () {
    fresh(3);
    G().debug.setInvincible(true);
    var pipes = collectPipes(1500);
    expect(pipes.length).toBeGreaterThanOrEqual(5);
    var h = S().height;
    var bad = pipes.filter(function (p) {
      return p.gapY < h * 0.25 - 1 || p.gapY > h * 0.75 + 1 || p.gapH < 140 - 0.5 || p.gapH > 200 + 0.5;
    });
    expect(bad.length).toBe(0);
  });

  T.test('b05-t08', 'passing pipes increases the score', async function () {
    fresh(4);
    G().debug.setInvincible(true);
    G().step(1200);
    var a = S();
    expect(a.phase).toBe('play');
    expect(a.score).toBeGreaterThanOrEqual(2);
  });

  T.test('b05-t09', 'flying into a pipe ends the game', async function () {
    fresh(5);
    for (var i = 0; i < 2000 && S().phase === 'play'; i++) {
      G().input.flap();
      G().step(1);
    }
    var a = S();
    expect(a.phase).toBe('over');
    expect(a.birdY).toBeLessThan(a.groundY * 0.5);
  });

  T.test('b05-t10', 'same seed gives the same pipes', async function () {
    function run() {
      fresh(6);
      G().debug.setInvincible(true);
      return collectPipes(600).map(function (p) { return Math.round(p.gapY * 100) / 100; });
    }
    expect(run()).toEqual(run());
  });

  T.test('b05-t11', 'Space flaps', async function () {
    fresh(7);
    G().step(20);
    expect(S().birdVY).toBeGreaterThan(0);
    await T.input.key('Space', 60);
    G().step(1);
    expect(S().birdVY).toBeLessThan(0);
  });

  T.test('b05-t12', 'a tap on the canvas flaps', async function () {
    fresh(8);
    G().step(20);
    var b = box();
    await T.input.touch(b.w / 2, b.h / 2);
    G().step(1);
    expect(S().birdVY).toBeLessThan(0);
  });

  T.test('b05-t13', 'start() after game over begins a new flight; screenshot', async function () {
    fresh(9);
    G().step(600);
    expect(S().phase).toBe('over');
    G().start();
    var a = S();
    expect(a.phase).toBe('play');
    expect(a.score).toBe(0);
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b05-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
