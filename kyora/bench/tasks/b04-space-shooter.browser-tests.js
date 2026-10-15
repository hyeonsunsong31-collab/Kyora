/* Kyora Bench b04-space-shooter - acceptance tests owned by the bench.
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
    G().debug.clearEnemies();
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
    var p = T.input.touch(x, y, mx, my, 400);
    await T.wait(150);
    G().step(ticks);
    await p;
  }
  function stepUntil(pred, maxTicks) {
    for (var i = 0; i < maxTicks; i++) {
      if (pred(S())) return true;
      G().step(1);
    }
    return pred(S());
  }

  KyoraTest.test('b04-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'input.fire', 'debug.setSpawning', 'debug.clearEnemies', 'debug.spawnEnemy'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b04-t02', 'start() puts the ship at the bottom with 3 lives and enemies arrive', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.lives).toBe(3);
    expect(s.width).toBe(480);
    expect(s.height).toBe(640);
    expect(s.shipY).toBeGreaterThan(s.height * 0.6);
    G().step(120);
    expect(S().enemies.length).toBeGreaterThan(0);
  });

  KyoraTest.test('b04-t03', 'the ship moves sideways and stays inside the field', async function () {
    var s = quiet(1);
    G().input.press('right');
    G().step(30);
    G().input.release('right');
    expect(S().shipX).toBeGreaterThan(s.shipX);
    G().input.press('left');
    G().step(600);
    G().input.release('left');
    var a = S();
    expect(a.shipX).toBeLessThan(s.shipX);
    expect(a.shipX).toBeGreaterThanOrEqual(0);
  });

  KyoraTest.test('b04-t04', 'fire() launches a bullet that flies up and leaves the screen', async function () {
    var s = quiet(1);
    G().input.fire();
    G().step(1);
    var a = S();
    expect(a.bullets.length).toBe(1);
    expect(a.bullets[0].y).toBeLessThan(s.shipY);
    G().step(240);
    expect(S().bullets.length).toBe(0);
  });

  KyoraTest.test('b04-t05', 'firing respects a cooldown of at most 15 ticks', async function () {
    quiet(1);
    G().input.fire();
    G().input.fire();
    G().input.fire();
    G().step(1);
    expect(S().bullets.length).toBe(1);
    G().step(20);
    G().input.fire();
    G().step(1);
    expect(S().bullets.length).toBe(2);
  });

  KyoraTest.test('b04-t06', 'shooting an enemy destroys it and scores', async function () {
    var s = quiet(1);
    G().debug.spawnEnemy(s.shipX, s.shipY - 250);
    expect(S().enemies.length).toBe(1);
    G().input.fire();
    var killed = stepUntil(function (x) { return x.enemies.length === 0; }, 120);
    expect(killed).toBe(true);
    var a = S();
    expect(a.score).toBeGreaterThan(0);
    expect(a.lives).toBe(3);
  });

  KyoraTest.test('b04-t07', 'an enemy touching the ship costs a life', async function () {
    var s = quiet(1);
    G().debug.spawnEnemy(s.shipX, s.shipY - 30);
    G().step(40);
    expect(S().lives).toBe(2);
    expect(S().phase).toBe('play');
  });

  KyoraTest.test('b04-t08', 'losing all lives ends the game', async function () {
    quiet(1);
    for (var i = 0; i < 10 && S().phase === 'play'; i++) {
      var s = S();
      G().debug.spawnEnemy(s.shipX, s.shipY - 30);
      G().step(150);
    }
    var a = S();
    expect(a.phase).toBe('over');
    expect(a.lives).toBe(0);
  });

  KyoraTest.test('b04-t09', 'a new wave arrives when the field is empty', async function () {
    fresh(2);
    G().debug.clearEnemies();
    expect(S().enemies.length).toBe(0);
    G().step(120);
    expect(S().enemies.length).toBeGreaterThan(0);
  });

  KyoraTest.test('b04-t10', 'same seed gives the same enemy formation', async function () {
    function run() {
      fresh(11);
      G().step(300);
      var s = S();
      return {
        score: s.score, lives: s.lives,
        enemies: s.enemies.map(function (e) { return [Math.round(e.x * 100) / 100, Math.round(e.y * 100) / 100]; })
      };
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b04-t11', 'Space fires and arrow keys move the ship', async function () {
    var s = quiet(3);
    await holdKey('Space', 3);
    expect(S().bullets.length).toBeGreaterThanOrEqual(1);
    await holdKey('ArrowLeft', 20);
    expect(S().shipX).toBeLessThan(s.shipX);
  });

  KyoraTest.test('b04-t12', 'touching the canvas steers the ship toward the finger', async function () {
    var s = quiet(4);
    var b = box();
    await holdTouch(b.w * 0.08, b.h * 0.8, undefined, undefined, 40);
    expect(S().shipX).toBeLessThan(s.shipX);
  });

  KyoraTest.test('b04-t13', 'screenshot of the running game', async function () {
    fresh(5);
    G().input.fire();
    G().step(90);
    await T.wait(50);
    var d = await T.screenshot('b04-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
