/* Kyora Bench b02-snake - acceptance tests owned by the bench.
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
  function moves(n) { G().step(S().ticksPerMove * n); }

  KyoraTest.test('b02-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.turn', 'debug.setFood'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b02-t02', 'start() places a 3-long snake heading right at the centre', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.length).toBe(3);
    expect(s.dir).toBe('right');
    expect(s.cols).toBeGreaterThanOrEqual(15);
    expect(s.rows).toBeGreaterThanOrEqual(15);
    expect(s.head).toEqual({ x: Math.floor(s.cols / 2), y: Math.floor(s.rows / 2) });
    expect(s.ticksPerMove).toBeGreaterThanOrEqual(1);
    expect(s.food.x === s.head.x && s.food.y === s.head.y).toBe(false);
  });

  KyoraTest.test('b02-t03', 'the snake advances exactly one cell every ticksPerMove ticks', async function () {
    var s = fresh(1);
    var tpm = s.ticksPerMove;
    if (tpm > 1) {
      G().step(tpm - 1);
      expect(S().head).toEqual(s.head);
      G().step(1);
    } else {
      G().step(1);
    }
    expect(S().head).toEqual({ x: s.head.x + 1, y: s.head.y });
    moves(2);
    expect(S().head).toEqual({ x: s.head.x + 3, y: s.head.y });
  });

  KyoraTest.test('b02-t04', 'turning up moves the head to a smaller y', async function () {
    var s = fresh(1);
    G().input.turn('up');
    moves(1);
    var a = S();
    expect(a.dir).toBe('up');
    expect(a.head).toEqual({ x: s.head.x, y: s.head.y - 1 });
  });

  KyoraTest.test('b02-t05', 'a turn onto the opposite direction is ignored', async function () {
    var s = fresh(1);
    G().input.turn('left');
    moves(1);
    expect(S().head).toEqual({ x: s.head.x + 1, y: s.head.y });
    expect(S().phase).toBe('play');
  });

  KyoraTest.test('b02-t06', 'eating food scores 10, grows the snake and respawns the food', async function () {
    var s = fresh(1);
    G().debug.setFood(s.head.x + 1, s.head.y);
    moves(1);
    var eaten = S();
    expect(eaten.score).toBe(10);
    expect(eaten.food.x === eaten.head.x && eaten.food.y === eaten.head.y).toBe(false);
    G().debug.setFood(0, 0);
    moves(2);
    expect(S().length).toBe(4);
  });

  KyoraTest.test('b02-t07', 'running into a wall ends the game', async function () {
    var s = fresh(1);
    G().debug.setFood(0, 0);
    moves(s.cols);
    expect(S().phase).toBe('over');
  });

  KyoraTest.test('b02-t08', 'running into its own body ends the game', async function () {
    var s = fresh(1);
    G().debug.setFood(s.head.x + 1, s.head.y);
    moves(1);
    G().debug.setFood(s.head.x + 2, s.head.y);
    moves(1);
    G().debug.setFood(0, 0);
    moves(2);
    expect(S().length).toBe(5);
    expect(S().phase).toBe('play');
    G().input.turn('up');
    moves(1);
    G().input.turn('left');
    moves(1);
    G().input.turn('down');
    moves(1);
    expect(S().phase).toBe('over');
  });

  KyoraTest.test('b02-t09', 'start() after game over begins a fresh game', async function () {
    var s = fresh(1);
    moves(s.cols);
    expect(S().phase).toBe('over');
    G().start();
    var a = S();
    expect(a.phase).toBe('play');
    expect(a.score).toBe(0);
    expect(a.length).toBe(3);
    expect(a.dir).toBe('right');
  });

  KyoraTest.test('b02-t10', 'same seed gives the same food and the same game', async function () {
    function run() {
      var s = fresh(42);
      var first = s.food;
      G().debug.setFood(s.head.x + 1, s.head.y);
      moves(3);
      var a = S();
      return { first: first, food: a.food, head: a.head, score: a.score };
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b02-t11', 'arrow keys turn the snake', async function () {
    var s = fresh(5);
    await T.input.key('ArrowUp', 80);
    moves(1);
    expect(S().head).toEqual({ x: s.head.x, y: s.head.y - 1 });
    await T.input.key('ArrowRight', 80);
    moves(1);
    expect(S().head).toEqual({ x: s.head.x + 1, y: s.head.y - 1 });
  });

  KyoraTest.test('b02-t12', 'a swipe on the canvas turns the snake', async function () {
    var s = fresh(6);
    var b = box();
    await T.input.touch(b.w / 2, b.h / 2, b.w / 2, b.h / 2 - Math.min(120, b.h / 3), 200);
    moves(1);
    expect(S().head).toEqual({ x: s.head.x, y: s.head.y - 1 });
  });

  KyoraTest.test('b02-t13', 'screenshot of the running game', async function () {
    fresh(7);
    moves(2);
    await T.wait(50);
    var d = await T.screenshot('b02-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
