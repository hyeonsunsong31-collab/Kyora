/* Kyora Bench b24-breakout-touch-and-bugfix - acceptance tests owned by the bench.
   During measurement this file replaces tests/browser-tests.js. It checks the paddle collision fix and the
   new touch controls of the imported Breakout only through the window.Game contract (seeded, manual clock
   via Game.step); it never reads pixels. */
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
  // drop the ball straight down onto the paddle at a fraction of its half width (-1 = left end, 1 = right end)
  function dropAt(fraction) {
    var s = fresh(1);
    G().debug.setBall(s.paddleX + fraction * s.paddleW / 2, s.height - 150, 0, 5);
    var bounced = stepUntil(function (x) { return x.ballVY < 0 || x.lives < 3; }, 150);
    return { bounced: bounced && S().ballVY < 0, lives: S().lives };
  }

  KyoraTest.test('b24-t01', 'the existing contract is intact', async function () {
    expect(canvas()).toBeTruthy();
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'input.action', 'debug.setBall', 'debug.keepBricks'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b24-t02', 'start() still deals the same game', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.lives).toBe(3);
    expect(s.bricks).toBe(48);
    expect(s.ballAttached).toBe(true);
    expect(s.width).toBe(480);
    expect(s.height).toBe(640);
  });

  KyoraTest.test('b24-t03', 'the left half of the paddle still returns the ball', async function () {
    var r = dropAt(-0.5);
    expect(r.bounced).toBe(true);
    expect(r.lives).toBe(3);
    r = dropAt(0);
    expect(r.bounced).toBe(true);
  });

  KyoraTest.test('b24-t04', 'bug fixed: the right half of the paddle returns the ball', async function () {
    var r = dropAt(0.5);
    expect(r.bounced).toBe(true);
    expect(r.lives).toBe(3);
  });

  KyoraTest.test('b24-t05', 'bug fixed: both paddle ends return the ball and deflect it outward', async function () {
    var right = dropAt(0.95);
    expect(right.bounced).toBe(true);
    expect(S().ballVX).toBeGreaterThan(0);
    var left = dropAt(-0.95);
    expect(left.bounced).toBe(true);
    expect(S().ballVX).toBeLessThan(0);
  });

  KyoraTest.test('b24-t06', 'a ball beside the paddle is still lost', async function () {
    var s = fresh(1);
    G().debug.setBall(s.paddleX + s.paddleW / 2 + 40, s.height - 150, 0, 5);
    G().step(150);
    expect(S().lives).toBe(2);
  });

  KyoraTest.test('b24-t07', 'keyboard controls still work', async function () {
    var s = fresh(2);
    await holdKey('ArrowLeft', 20);
    expect(S().paddleX).toBeLessThan(s.paddleX);
    await holdKey('Space', 2);
    expect(S().ballAttached).toBe(false);
  });

  KyoraTest.test('b24-t08', 'dragging on the canvas moves the paddle toward the finger (left)', async function () {
    var s = fresh(3);
    var b = box();
    await holdTouch(b.w * 0.5, b.h * 0.85, b.w * 0.1, b.h * 0.85, 40);
    expect(S().paddleX).toBeLessThan(s.paddleX - 20);
  });

  KyoraTest.test('b24-t09', 'dragging on the canvas moves the paddle toward the finger (right)', async function () {
    var s = fresh(4);
    var b = box();
    await holdTouch(b.w * 0.5, b.h * 0.85, b.w * 0.9, b.h * 0.85, 40);
    expect(S().paddleX).toBeGreaterThan(s.paddleX + 20);
  });

  KyoraTest.test('b24-t10', 'a tap launches the ball', async function () {
    fresh(5);
    var b = box();
    await T.input.touch(b.w * 0.5, b.h * 0.5);
    await T.wait(30);
    G().step(1);
    expect(S().ballAttached).toBe(false);
  });

  KyoraTest.test('b24-t11', 'the canvas does not scroll the page on touch', async function () {
    expect(getComputedStyle(canvas()).touchAction).toBe('none');
  });

  KyoraTest.test('b24-t12', 'bricks still break and the last brick still wins', async function () {
    var s = fresh(6);
    var low = s.brickRects.reduce(function (a, r) { return (r.y + r.h > a.y + a.h) ? r : a; }, s.brickRects[0]);
    G().debug.setBall(low.x + low.w / 2, low.y + low.h + 40, 0, -4);
    expect(stepUntil(function (x) { return x.bricks < s.bricks; }, 90)).toBe(true);
    expect(S().score).toBeGreaterThan(0);
    G().debug.keepBricks(1);
    var r = S().brickRects[0];
    G().debug.setBall(r.x + r.w / 2, r.y + r.h + 40, 0, -4);
    stepUntil(function (x) { return x.phase !== 'play'; }, 120);
    expect(S().won).toBe(true);
  });

  KyoraTest.test('b24-t13', 'screenshot of the running game', async function () {
    fresh(7);
    G().input.action();
    G().step(30);
    await T.wait(50);
    var d = await T.screenshot('b24-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
