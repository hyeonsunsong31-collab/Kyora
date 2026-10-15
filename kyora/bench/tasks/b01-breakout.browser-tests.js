/* Kyora Bench b01-breakout - acceptance tests owned by the bench.
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
  function paddleToLeft() {
    G().input.press('left');
    G().step(400);
    G().input.release('left');
  }
  function stepUntil(pred, maxTicks) {
    for (var i = 0; i < maxTicks; i++) {
      if (pred(S())) return true;
      G().step(1);
    }
    return pred(S());
  }

  KyoraTest.test('b01-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(window.Game).toBeDefined();
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'input.action', 'debug.setBall', 'debug.keepBricks'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b01-t02', 'start() enters play with 3 lives, a brick wall and the ball on the paddle', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.lives).toBe(3);
    expect(s.won).toBe(false);
    expect(s.bricks).toBeGreaterThanOrEqual(40);
    expect(s.brickRects.length).toBe(s.bricks);
    expect(s.ballAttached).toBe(true);
    expect(s.width).toBe(480);
    expect(s.height).toBe(640);
  });

  KyoraTest.test('b01-t03', 'paddle moves with input and stays inside the field', async function () {
    var s0 = fresh(1);
    G().input.press('right');
    G().step(30);
    G().input.release('right');
    var s1 = S();
    expect(s1.paddleX).toBeGreaterThan(s0.paddleX);
    paddleToLeft();
    var s2 = S();
    expect(s2.paddleX).toBeLessThan(s0.paddleX);
    expect(s2.paddleX - s2.paddleW / 2).toBeGreaterThanOrEqual(-1);
    G().input.press('right');
    G().step(600);
    G().input.release('right');
    var s3 = S();
    expect(s3.paddleX + s3.paddleW / 2).toBeLessThanOrEqual(s3.width + 1);
  });

  KyoraTest.test('b01-t04', 'the resting ball follows the paddle and action() launches it upwards', async function () {
    fresh(2);
    G().input.press('right');
    G().step(20);
    G().input.release('right');
    var s = S();
    expect(s.ballAttached).toBe(true);
    expect(Math.abs(s.ballX - s.paddleX)).toBeLessThan(s.paddleW / 2);
    G().input.action();
    G().step(1);
    var s1 = S();
    expect(s1.ballAttached).toBe(false);
    expect(s1.ballVY).toBeLessThan(0);
    G().step(10);
    expect(S().ballY).toBeLessThan(s.ballY);
  });

  KyoraTest.test('b01-t05', 'the ball bounces off the side walls', async function () {
    var s = fresh(1);
    G().debug.setBall(20, s.height * 0.6, -5, -1);
    G().step(20);
    var a = S();
    expect(a.ballVX).toBeGreaterThan(0);
    expect(a.ballX).toBeGreaterThanOrEqual(0);
    G().debug.setBall(s.width - 20, s.height * 0.6, 5, -1);
    G().step(20);
    var b = S();
    expect(b.ballVX).toBeLessThan(0);
    expect(b.ballX).toBeLessThanOrEqual(s.width);
  });

  KyoraTest.test('b01-t06', 'the ball bounces off the paddle without losing a life', async function () {
    var s = fresh(1);
    G().debug.setBall(s.paddleX, s.height - 150, 0, 5);
    var bounced = stepUntil(function (x) { return x.ballVY < 0; }, 120);
    expect(bounced).toBe(true);
    expect(S().lives).toBe(3);
  });

  KyoraTest.test('b01-t07', 'hitting a brick removes it, scores and reflects the ball', async function () {
    var s = fresh(1);
    var low = s.brickRects.reduce(function (a, r) { return (r.y + r.h > a.y + a.h) ? r : a; }, s.brickRects[0]);
    G().debug.setBall(low.x + low.w / 2, low.y + low.h + 40, 0, -4);
    var hit = stepUntil(function (x) { return x.bricks < s.bricks; }, 90);
    expect(hit).toBe(true);
    var after = S();
    expect(after.bricks).toBe(s.bricks - 1);
    expect(after.score).toBeGreaterThan(0);
    G().step(2);
    expect(S().ballVY).toBeGreaterThan(0);
  });

  KyoraTest.test('b01-t08', 'missing the ball costs a life and puts the ball back on the paddle', async function () {
    var s = fresh(1);
    paddleToLeft();
    G().debug.setBall(s.width - 20, s.height * 0.5, 0, 6);
    G().step(150);
    var a = S();
    expect(a.lives).toBe(2);
    expect(a.phase).toBe('play');
    expect(a.ballAttached).toBe(true);
  });

  KyoraTest.test('b01-t09', 'losing every life ends the game', async function () {
    var s = fresh(1);
    for (var i = 0; i < 6 && S().phase === 'play'; i++) {
      paddleToLeft();
      G().debug.setBall(s.width - 20, s.height * 0.5, 0, 6);
      G().step(150);
    }
    var a = S();
    expect(a.phase).toBe('over');
    expect(a.lives).toBe(0);
    expect(a.won).toBe(false);
  });

  KyoraTest.test('b01-t10', 'clearing the last brick wins', async function () {
    fresh(1);
    G().debug.keepBricks(1);
    var s = S();
    expect(s.bricks).toBe(1);
    var r = s.brickRects[0];
    G().debug.setBall(r.x + r.w / 2, r.y + r.h + 40, 0, -4);
    stepUntil(function (x) { return x.bricks === 0 || x.phase !== 'play'; }, 120);
    var a = S();
    expect(a.bricks).toBe(0);
    expect(a.phase).toBe('over');
    expect(a.won).toBe(true);
    expect(a.score).toBeGreaterThan(0);
  });

  KyoraTest.test('b01-t11', 'same seed and same inputs give the same game', async function () {
    function run() {
      fresh(7);
      G().input.action();
      G().step(240);
      var s = S();
      return [s.ballX, s.ballY, s.ballVX, s.ballVY, s.score, s.bricks, s.lives].map(function (v) {
        return typeof v === 'number' ? Math.round(v * 1000) / 1000 : v;
      });
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b01-t12', 'arrow keys move the paddle and Space launches the ball', async function () {
    var s0 = fresh(3);
    await holdKey('ArrowLeft', 30);
    expect(S().paddleX).toBeLessThan(s0.paddleX);
    var s1 = S();
    await holdKey('ArrowRight', 30);
    expect(S().paddleX).toBeGreaterThan(s1.paddleX);
    await holdKey('Space', 2);
    expect(S().ballAttached).toBe(false);
  });

  KyoraTest.test('b01-t13', 'touching the canvas moves the paddle toward the finger', async function () {
    var s0 = fresh(4);
    var b = box();
    await holdTouch(b.w * 0.08, b.h * 0.85, undefined, undefined, 40);
    expect(S().paddleX).toBeLessThan(s0.paddleX);
  });

  KyoraTest.test('b01-t14', 'screenshot of the running game', async function () {
    fresh(5);
    G().input.action();
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b01-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
