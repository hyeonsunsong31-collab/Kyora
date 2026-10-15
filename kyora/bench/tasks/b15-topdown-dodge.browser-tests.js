/* Kyora Bench b15-topdown-dodge - acceptance tests owned by the bench.
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
  function quiet(seed) {
    fresh(seed);
    G().debug.setSpawning(false);
    G().debug.clearHazards();
    return S();
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
  async function holdKey(code, ticks) {
    var p = T.input.key(code, 300);
    await T.wait(80);
    G().step(ticks);
    await p;
  }
  async function holdTouch(x, y, mx, my, ticks) {
    var p = T.input.touch(x, y, mx, my, 500);
    await T.wait(200);
    G().step(ticks);
    await p;
  }
  function hold(names, ticks) {
    names.forEach(function (n) { G().input.press(n); });
    G().step(ticks);
    names.forEach(function (n) { G().input.release(n); });
    return S();
  }

  KyoraTest.test('b15-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'debug.setSpawning', 'debug.clearHazards', 'debug.spawnHazard'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b15-t02', 'start() puts the player in the centre of a 600x600 arena', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.time).toBe(0);
    expect(s.width).toBe(600);
    expect(s.height).toBe(600);
    expect(Math.abs(s.player.x - 300)).toBeLessThanOrEqual(1);
    expect(Math.abs(s.player.y - 300)).toBeLessThanOrEqual(1);
    expect(s.player.r).toBeGreaterThan(0);
  });

  KyoraTest.test('b15-t03', 'surviving scores one point per second', async function () {
    quiet(1);
    G().step(600);
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.time).toBe(600);
    expect(s.score).toBe(10);
  });

  KyoraTest.test('b15-t04', 'the player moves in four directions (y grows downward)', async function () {
    var s = quiet(1);
    var a = hold(['right'], 20);
    expect(a.player.x).toBeGreaterThan(s.player.x);
    var b = hold(['down'], 20);
    expect(b.player.y).toBeGreaterThan(a.player.y);
    var c = hold(['left'], 20);
    expect(c.player.x).toBeLessThan(b.player.x);
    var d = hold(['up'], 20);
    expect(d.player.y).toBeLessThan(c.player.y);
  });

  KyoraTest.test('b15-t05', 'the player stays inside the arena', async function () {
    quiet(1);
    var a = hold(['left', 'up'], 600);
    expect(a.player.x - a.player.r).toBeGreaterThanOrEqual(-0.5);
    expect(a.player.y - a.player.r).toBeGreaterThanOrEqual(-0.5);
    var b = hold(['right', 'down'], 900);
    expect(b.player.x + b.player.r).toBeLessThanOrEqual(b.width + 0.5);
    expect(b.player.y + b.player.r).toBeLessThanOrEqual(b.height + 0.5);
  });

  KyoraTest.test('b15-t06', 'diagonal movement is not faster than straight movement', async function () {
    var s = quiet(1);
    var a = hold(['right'], 30);
    var straight = a.player.x - s.player.x;
    quiet(1);
    var b = hold(['right', 'down'], 30);
    var diag = Math.hypot(b.player.x - s.player.x, b.player.y - s.player.y);
    expect(straight).toBeGreaterThan(0);
    expect(Math.abs(diag - straight)).toBeLessThanOrEqual(straight * 0.15);
  });

  KyoraTest.test('b15-t07', 'a hazard hit ends the game', async function () {
    var s = quiet(2);
    G().debug.spawnHazard(s.player.x + 60, s.player.y, -4, 0, 10);
    G().step(40);
    expect(S().phase).toBe('over');
  });

  KyoraTest.test('b15-t08', 'a near miss does not end the game and hazards leave the arena', async function () {
    var s = quiet(3);
    G().debug.spawnHazard(s.player.x + 100, s.player.y + s.player.r + 40, -4, 0, 8);
    G().step(60);
    expect(S().phase).toBe('play');
    G().debug.clearHazards();
    G().debug.spawnHazard(s.width - 10, 10, 8, 0, 6);
    G().step(40);
    expect(S().hazards.length).toBe(0);
  });

  KyoraTest.test('b15-t09', 'hazards spawn by themselves', async function () {
    fresh(4);
    var most = 0;
    for (var i = 0; i < 60 && S().phase === 'play'; i++) {
      G().step(10);
      most = Math.max(most, S().hazards.length);
    }
    expect(most).toBeGreaterThan(0);
  });

  KyoraTest.test('b15-t10', 'same seed gives the same hazards', async function () {
    function run() {
      fresh(17);
      G().step(300);
      var s = S();
      return {
        phase: s.phase, time: s.time,
        hazards: s.hazards.map(function (h) { return [Math.round(h.x * 100) / 100, Math.round(h.y * 100) / 100]; })
      };
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b15-t11', 'arrow keys move the player', async function () {
    var s = quiet(5);
    await holdKey('ArrowUp', 20);
    expect(S().player.y).toBeLessThan(s.player.y);
    await holdKey('ArrowRight', 20);
    expect(S().player.x).toBeGreaterThan(s.player.x);
  });

  KyoraTest.test('b15-t12', 'dragging on the canvas works as a virtual joystick', async function () {
    var s = quiet(6);
    var b = box();
    await holdTouch(b.w / 2, b.h / 2, b.w / 2 + Math.min(100, b.w / 4), b.h / 2, 20);
    expect(S().player.x).toBeGreaterThan(s.player.x);
  });

  KyoraTest.test('b15-t13', 'screenshot of the running game', async function () {
    fresh(7);
    G().step(120);
    await T.wait(50);
    var d = await T.screenshot('b15-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
