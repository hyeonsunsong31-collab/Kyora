/* Kyora Bench b03-pong - acceptance tests owned by the bench.
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
  function shootPastAI() {
    var s = S();
    var y = s.aiY < s.height / 2 ? s.height - 25 : 25;
    G().debug.setBall(s.width * 0.7, y, 6, 0);
    G().step(80);
  }

  KyoraTest.test('b03-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release',
      'debug.setBall', 'debug.setAI'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b03-t02', 'start() resets scores and centres the ball', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.aiScore).toBe(0);
    expect(s.winner).toBe(null);
    expect(s.width).toBe(640);
    expect(s.height).toBe(480);
    expect(Math.abs(s.ballX - s.width / 2)).toBeLessThan(s.width * 0.1);
    expect(s.playerX).toBeLessThan(s.width / 2);
    expect(s.aiX).toBeGreaterThan(s.width / 2);
  });

  KyoraTest.test('b03-t03', 'the ball is served within 60 ticks', async function () {
    var s = fresh(1);
    G().step(90);
    var a = S();
    expect(a.ballVX !== 0).toBe(true);
    expect(Math.abs(a.ballX - s.width / 2)).toBeGreaterThan(1);
  });

  KyoraTest.test('b03-t04', 'the player paddle moves and stays inside the field', async function () {
    var s = fresh(1);
    G().input.press('down');
    G().step(30);
    G().input.release('down');
    expect(S().playerY).toBeGreaterThan(s.playerY);
    G().input.press('up');
    G().step(600);
    G().input.release('up');
    var a = S();
    expect(a.playerY).toBeLessThan(s.playerY);
    expect(a.playerY - a.paddleH / 2).toBeGreaterThanOrEqual(-1);
  });

  KyoraTest.test('b03-t05', 'the ball bounces off the top and bottom walls', async function () {
    var s = fresh(1);
    G().debug.setBall(s.width / 2, 20, 3, -5);
    G().step(15);
    expect(S().ballVY).toBeGreaterThan(0);
    G().debug.setBall(s.width / 2, s.height - 20, 3, 5);
    G().step(15);
    expect(S().ballVY).toBeLessThan(0);
    expect(S().ballY).toBeLessThanOrEqual(s.height);
  });

  KyoraTest.test('b03-t06', 'the player paddle returns the ball', async function () {
    var s = fresh(1);
    G().debug.setBall(s.playerX + 60, s.playerY, -5, 0);
    var back = stepUntil(function (x) { return x.ballVX > 0; }, 60);
    expect(back).toBe(true);
    expect(S().aiScore).toBe(0);
  });

  KyoraTest.test('b03-t07', 'a ball past the AI paddle scores for the player', async function () {
    fresh(1);
    G().debug.setAI(false);
    shootPastAI();
    var a = S();
    expect(a.score).toBe(1);
    expect(a.aiScore).toBe(0);
  });

  KyoraTest.test('b03-t08', 'a ball past the player paddle scores for the AI', async function () {
    var s = fresh(1);
    G().input.press('up');
    G().step(300);
    G().input.release('up');
    G().debug.setBall(s.width * 0.3, s.height - 25, -6, 0);
    G().step(80);
    var a = S();
    expect(a.aiScore).toBe(1);
    expect(a.score).toBe(0);
  });

  KyoraTest.test('b03-t09', 'first to 5 points wins', async function () {
    fresh(1);
    G().debug.setAI(false);
    for (var i = 0; i < 5; i++) shootPastAI();
    var a = S();
    expect(a.score).toBe(5);
    expect(a.phase).toBe('over');
    expect(a.winner).toBe('player');
  });

  KyoraTest.test('b03-t10', 'same seed gives the same rally', async function () {
    function run() {
      fresh(9);
      G().step(300);
      var s = S();
      return [s.ballX, s.ballY, s.score, s.aiScore, s.aiY].map(function (v) { return Math.round(v * 1000) / 1000; });
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b03-t11', 'arrow keys move the player paddle', async function () {
    var s = fresh(2);
    await holdKey('ArrowDown', 30);
    expect(S().playerY).toBeGreaterThan(s.playerY);
    var a = S();
    await holdKey('ArrowUp', 30);
    expect(S().playerY).toBeLessThan(a.playerY);
  });

  KyoraTest.test('b03-t12', 'touching the canvas moves the paddle toward the finger', async function () {
    var s = fresh(3);
    var b = box();
    await holdTouch(b.w * 0.1, b.h * 0.08, undefined, undefined, 40);
    expect(S().playerY).toBeLessThan(s.playerY);
  });

  KyoraTest.test('b03-t13', 'screenshot of the running game', async function () {
    fresh(4);
    G().step(120);
    await T.wait(50);
    var d = await T.screenshot('b03-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
