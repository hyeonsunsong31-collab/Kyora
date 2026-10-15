/* Kyora Bench b06-asteroids - acceptance tests owned by the bench.
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
  // signed smallest difference b - a in (-PI, PI]
  function angDiff(a, b) {
    var d = (b - a) % (2 * Math.PI);
    if (d > Math.PI) d -= 2 * Math.PI;
    if (d <= -Math.PI) d += 2 * Math.PI;
    return d;
  }
  function empty(seed) {
    var s = fresh(seed);
    G().debug.clearAsteroids();
    return s;
  }
  // an empty field plus one parked small asteroid in a corner, so no new wave spawns during the test
  function calm(seed) {
    var s = empty(seed);
    G().debug.spawnAsteroid(10, 10, 1, 0, 0);
    return s;
  }

  KyoraTest.test('b06-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'input.fire', 'debug.setShip', 'debug.clearAsteroids', 'debug.spawnAsteroid'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b06-t02', 'start() spawns 4 large asteroids away from the ship', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.lives).toBe(3);
    expect(s.score).toBe(0);
    expect(s.width).toBe(640);
    expect(s.height).toBe(480);
    expect(s.asteroids.length).toBe(4);
    s.asteroids.forEach(function (a) {
      expect(a.size).toBe(3);
      expect(Math.hypot(a.x - s.ship.x, a.y - s.ship.y)).toBeGreaterThanOrEqual(100);
    });
  });

  KyoraTest.test('b06-t03', 'right rotates clockwise, left counter-clockwise', async function () {
    var s = calm(1);
    G().input.press('right');
    G().step(10);
    G().input.release('right');
    var a = S().ship.angle;
    expect(angDiff(s.ship.angle, a)).toBeGreaterThan(0.01);
    G().input.press('left');
    G().step(10);
    G().input.release('left');
    expect(angDiff(a, S().ship.angle)).toBeLessThan(-0.01);
  });

  KyoraTest.test('b06-t04', 'thrust accelerates the ship in the facing direction', async function () {
    var s = calm(1);
    G().debug.setShip(s.width / 2, s.height / 2, 0, 0, 0);
    G().input.press('thrust');
    G().step(20);
    G().input.release('thrust');
    var a = S().ship;
    expect(a.vy).toBeLessThan(0);
    expect(Math.abs(a.vx)).toBeLessThan(0.01);
    expect(a.y).toBeLessThan(s.height / 2);
  });

  KyoraTest.test('b06-t05', 'the ship wraps around the screen edges', async function () {
    var s = calm(1);
    G().debug.setShip(s.width - 5, s.height / 2, Math.PI / 2, 4, 0);
    G().step(5);
    var a = S().ship;
    expect(a.x).toBeLessThan(s.width / 2);
    expect(a.x).toBeGreaterThanOrEqual(0);
    G().debug.setShip(s.width / 2, 3, 0, 0, -4);
    G().step(5);
    expect(S().ship.y).toBeGreaterThan(s.height / 2);
  });

  KyoraTest.test('b06-t06', 'shooting a large asteroid splits it into two medium ones', async function () {
    var s = empty(1);
    G().debug.setShip(s.width / 2, s.height * 0.8, 0, 0, 0);
    G().debug.spawnAsteroid(s.width / 2, s.height * 0.8 - 160, 3, 0, 0);
    G().input.fire();
    var split = stepUntil(function (x) { return x.asteroids.length !== 1; }, 90);
    expect(split).toBe(true);
    var a = S();
    expect(a.asteroids.length).toBe(2);
    a.asteroids.forEach(function (r) { expect(r.size).toBe(2); });
    expect(a.score).toBeGreaterThan(0);
  });

  KyoraTest.test('b06-t07', 'shooting a small asteroid destroys it', async function () {
    var s = empty(2);
    G().debug.setShip(s.width / 2, s.height * 0.8, 0, 0, 0);
    G().debug.spawnAsteroid(s.width / 2, s.height * 0.8 - 160, 1, 0, 0);
    G().input.fire();
    var gone = stepUntil(function (x) { return x.asteroids.length === 0; }, 90);
    expect(gone).toBe(true);
    expect(S().score).toBeGreaterThan(0);
  });

  KyoraTest.test('b06-t08', 'a collision costs a life and grants a short invulnerability', async function () {
    var s = empty(1);
    G().debug.setShip(s.width / 2, s.height / 2, 0, 0, 0);
    G().debug.spawnAsteroid(s.width / 2, s.height / 2, 2, 0, 0);
    G().step(3);
    var a = S();
    expect(a.lives).toBe(2);
    expect(a.invulnerable).toBeGreaterThan(0);
    expect(a.invulnerable).toBeLessThanOrEqual(120);
  });

  KyoraTest.test('b06-t09', 'losing every life ends the game', async function () {
    empty(1);
    for (var i = 0; i < 10 && S().phase === 'play'; i++) {
      G().debug.clearAsteroids();
      var sh = S().ship;
      G().debug.spawnAsteroid(sh.x, sh.y, 2, 0, 0);
      G().step(130);
    }
    var a = S();
    expect(a.phase).toBe('over');
    expect(a.lives).toBe(0);
  });

  KyoraTest.test('b06-t10', 'clearing the field starts the next wave', async function () {
    var s = fresh(3);
    G().debug.clearAsteroids();
    G().step(130);
    var a = S();
    expect(a.asteroids.length).toBeGreaterThanOrEqual(1);
    expect(a.wave).toBeGreaterThan(s.wave);
  });

  KyoraTest.test('b06-t11', 'same seed gives the same asteroid field', async function () {
    function run() {
      var s = fresh(4);
      return s.asteroids.map(function (a) { return [Math.round(a.x * 100) / 100, Math.round(a.y * 100) / 100, a.size]; });
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b06-t12', 'arrow keys rotate and Space fires', async function () {
    var s = calm(5);
    await holdKey('ArrowLeft', 15);
    expect(Math.abs(angDiff(s.ship.angle, S().ship.angle))).toBeGreaterThan(0.01);
    await holdKey('Space', 1);
    expect(S().bullets.length).toBeGreaterThanOrEqual(1);
  });

  KyoraTest.test('b06-t13', 'holding the left third of the canvas rotates the ship', async function () {
    var s = calm(6);
    var b = box();
    await holdTouch(b.w * 0.1, b.h * 0.5, undefined, undefined, 20);
    expect(angDiff(s.ship.angle, S().ship.angle)).toBeLessThan(-0.01);
  });

  KyoraTest.test('b06-t14', 'screenshot of the running game', async function () {
    fresh(7);
    G().input.fire();
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b06-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
