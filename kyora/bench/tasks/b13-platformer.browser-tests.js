/* Kyora Bench b13-platformer - acceptance tests owned by the bench.
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
  var FLOOR_Y = 360;
  function level(extra) {
    var L = {
      width: 800, height: 400, start: { x: 40, y: 300 },
      platforms: [{ x: 0, y: FLOOR_Y, w: 800, h: 40 }], coins: [], spikes: [],
      goal: { x: 760, y: 280, w: 20, h: 80 }
    };
    Object.keys(extra || {}).forEach(function (k) { L[k] = extra[k]; });
    fresh(1);
    G().loadLevel(L);
    G().step(90);
    return S();
  }
  function walkRightUntil(pred, maxTicks) {
    G().input.press('right');
    var ok = stepUntil(pred, maxTicks);
    G().input.release('right');
    return ok;
  }

  KyoraTest.test('b13-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.press', 'input.release', 'input.jump',
      'loadLevel', 'debug.setPlayer'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b13-t02', 'start() loads the built-in level and the hero lands on solid ground', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.lives).toBe(3);
    expect(s.score).toBe(0);
    expect(s.won).toBe(false);
    expect(s.coins).toBeGreaterThanOrEqual(3);
    expect(s.player.w).toBeLessThanOrEqual(32);
    expect(s.player.h).toBeLessThanOrEqual(48);
    G().step(120);
    expect(S().player.onGround).toBe(true);
    expect(S().lives).toBe(3);
  });

  KyoraTest.test('b13-t03', 'gravity drops the hero onto a platform', async function () {
    var s = level();
    expect(s.player.onGround).toBe(true);
    expect(Math.abs(s.player.y + s.player.h - FLOOR_Y)).toBeLessThanOrEqual(1);
    expect(Math.abs(s.player.vy)).toBeLessThan(0.5);
    G().debug.setPlayer(200, 100);
    G().step(1);
    expect(S().player.onGround).toBe(false);
    G().step(120);
    var a = S();
    expect(a.player.onGround).toBe(true);
    expect(Math.abs(a.player.y + a.player.h - FLOOR_Y)).toBeLessThanOrEqual(1);
  });

  KyoraTest.test('b13-t04', 'left and right walk', async function () {
    var s = level();
    G().input.press('right');
    G().step(30);
    G().input.release('right');
    var a = S();
    expect(a.player.x).toBeGreaterThan(s.player.x);
    expect(a.player.x - s.player.x).toBeLessThanOrEqual(30 * 6 + 1);
    G().input.press('left');
    G().step(20);
    G().input.release('left');
    expect(S().player.x).toBeLessThan(a.player.x);
  });

  KyoraTest.test('b13-t05', 'a jump rises at least 64 units and lands again', async function () {
    var s = level();
    G().input.jump();
    G().step(1);
    expect(S().player.vy).toBeLessThan(0);
    var minY = s.player.y;
    for (var i = 0; i < 90; i++) {
      G().step(1);
      minY = Math.min(minY, S().player.y);
    }
    var rise = s.player.y - minY;
    expect(rise).toBeGreaterThanOrEqual(64);
    expect(rise).toBeLessThan(200);
    expect(stepUntil(function (x) { return x.player.onGround; }, 120)).toBe(true);
  });

  KyoraTest.test('b13-t06', 'there is no double jump', async function () {
    level();
    G().input.jump();
    G().step(10);
    var vy1 = S().player.vy;
    G().input.jump();
    G().step(1);
    expect(S().player.vy).toBeGreaterThanOrEqual(vy1);
  });

  KyoraTest.test('b13-t07', 'platforms are solid from the side', async function () {
    level({ platforms: [{ x: 0, y: FLOOR_Y, w: 800, h: 40 }, { x: 300, y: 200, w: 40, h: 160 }] });
    G().input.press('right');
    G().step(240);
    G().input.release('right');
    var p = S().player;
    expect(p.x + p.w).toBeLessThanOrEqual(301);
    expect(p.onGround).toBe(true);
  });

  KyoraTest.test('b13-t08', 'walking through a coin collects it', async function () {
    var s = level({ coins: [{ x: 200, y: 345 }] });
    expect(s.coins).toBe(1);
    var got = walkRightUntil(function (x) { return x.coins === 0; }, 300);
    expect(got).toBe(true);
    expect(S().score).toBeGreaterThanOrEqual(10);
  });

  KyoraTest.test('b13-t09', 'spikes cost a life and send the hero back to the start; the last life ends the game', async function () {
    level({ spikes: [{ x: 150, y: 350, w: 60, h: 10 }] });
    var hurt = walkRightUntil(function (x) { return x.lives < 3; }, 400);
    expect(hurt).toBe(true);
    G().step(5);
    var a = S();
    expect(a.lives).toBe(2);
    expect(a.player.x).toBeLessThan(120);
    walkRightUntil(function (x) { return x.phase !== 'play'; }, 2400);
    var b = S();
    expect(b.phase).toBe('over');
    expect(b.lives).toBe(0);
    expect(b.won).toBe(false);
  });

  KyoraTest.test('b13-t10', 'falling below the level costs a life', async function () {
    level({ platforms: [{ x: 0, y: FLOOR_Y, w: 200, h: 40 }, { x: 400, y: FLOOR_Y, w: 400, h: 40 }] });
    var fell = walkRightUntil(function (x) { return x.lives < 3; }, 600);
    expect(fell).toBe(true);
    expect(S().lives).toBe(2);
  });

  KyoraTest.test('b13-t11', 'touching the goal wins', async function () {
    level({ goal: { x: 220, y: 300, w: 20, h: 60 } });
    var won = walkRightUntil(function (x) { return x.phase !== 'play'; }, 400);
    expect(won).toBe(true);
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.won).toBe(true);
  });

  KyoraTest.test('b13-t12', 'arrow keys walk and Space jumps', async function () {
    var s = level();
    await holdKey('ArrowRight', 20);
    expect(S().player.x).toBeGreaterThan(s.player.x);
    G().step(30);
    await holdKey('Space', 3);
    expect(S().player.onGround).toBe(false);
  });

  KyoraTest.test('b13-t13', 'holding the right third of the canvas walks right', async function () {
    var s = level();
    var b = box();
    await holdTouch(b.w * 0.9, b.h * 0.6, undefined, undefined, 20);
    expect(S().player.x).toBeGreaterThan(s.player.x);
  });

  KyoraTest.test('b13-t14', 'screenshot of the running game', async function () {
    fresh(2);
    G().input.press('right');
    G().step(40);
    G().input.release('right');
    await T.wait(50);
    var d = await T.screenshot('b13-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
