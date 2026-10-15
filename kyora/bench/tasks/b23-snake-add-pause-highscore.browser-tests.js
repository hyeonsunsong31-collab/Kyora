/* Kyora Bench b23-snake-add-pause-highscore - acceptance tests owned by the bench.
   During measurement this file replaces tests/browser-tests.js. It checks that the imported Snake keeps
   working and gained pause + a persisted high score, only through the window.Game contract (seeded,
   manual clock via Game.step); it never reads pixels. */
(function () {
  'use strict';
  var T = window.KyoraTest;
  var expect = T.expect;
  var KEY = 'kyora-snake-best';

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
  function moves(n) { G().step(S().ticksPerMove * n); }
  function eatTwiceThenCrash() {
    var s = S();
    G().debug.setFood(s.head.x + 1, s.head.y);
    moves(1);
    G().debug.setFood(s.head.x + 2, s.head.y);
    moves(1);
    G().debug.setFood(0, 0);
    moves(s.cols);
  }
  function store(value) {
    try {
      if (value === null) localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, value);
    } catch (e) { /* storage blocked: the assertions below will report it */ }
  }

  KyoraTest.test('b23-t01', 'the existing contract is intact and the new API exists', async function () {
    expect(canvas()).toBeTruthy();
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.turn', 'debug.setFood',
      'input.togglePause', 'pauseButtonRect'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b23-t02', 'the snake still starts at the centre and moves one cell per ticksPerMove', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.length).toBe(3);
    expect(s.paused).toBe(false);
    moves(2);
    expect(S().head).toEqual({ x: s.head.x + 2, y: s.head.y });
    G().input.turn('down');
    moves(1);
    expect(S().head).toEqual({ x: s.head.x + 2, y: s.head.y + 1 });
  });

  KyoraTest.test('b23-t03', 'eating still scores 10 and grows; walls still end the game', async function () {
    var s = fresh(1);
    G().debug.setFood(s.head.x + 1, s.head.y);
    moves(1);
    expect(S().score).toBe(10);
    G().debug.setFood(0, 0);
    moves(1);
    expect(S().length).toBe(4);
    moves(s.cols);
    expect(S().phase).toBe('over');
  });

  KyoraTest.test('b23-t04', 'togglePause() freezes the snake and resumes it', async function () {
    var s = fresh(1);
    G().input.togglePause();
    expect(S().paused).toBe(true);
    moves(5);
    expect(S().head).toEqual(s.head);
    expect(S().phase).toBe('play');
    G().input.togglePause();
    expect(S().paused).toBe(false);
    moves(1);
    expect(S().head).toEqual({ x: s.head.x + 1, y: s.head.y });
  });

  KyoraTest.test('b23-t05', 'turns are ignored while paused', async function () {
    var s = fresh(1);
    G().input.togglePause();
    G().input.turn('up');
    G().input.togglePause();
    moves(1);
    expect(S().head).toEqual({ x: s.head.x + 1, y: s.head.y });
  });

  KyoraTest.test('b23-t06', 'P pauses and Escape resumes', async function () {
    fresh(2);
    await T.input.key('KeyP', 60);
    expect(S().paused).toBe(true);
    await T.input.key('Escape', 60);
    expect(S().paused).toBe(false);
  });

  KyoraTest.test('b23-t07', 'the on-canvas pause button works by touch', async function () {
    fresh(3);
    var r = G().pauseButtonRect();
    expect(r.w).toBeGreaterThanOrEqual(24);
    expect(r.h).toBeGreaterThanOrEqual(24);
    await T.input.touch(r.x + r.w / 2, r.y + r.h / 2);
    await T.wait(30);
    expect(S().paused).toBe(true);
    await T.input.touch(r.x + r.w / 2, r.y + r.h / 2);
    await T.wait(30);
    expect(S().paused).toBe(false);
  });

  KyoraTest.test('b23-t08', 'pause only applies while playing; start() unpauses', async function () {
    var s = fresh(4);
    G().input.togglePause();
    expect(S().paused).toBe(true);
    G().start();
    expect(S().paused).toBe(false);
    moves(s.cols);
    expect(S().phase).toBe('over');
    G().input.togglePause();
    expect(S().paused).toBe(false);
  });

  KyoraTest.test('b23-t09', 'a new best score is saved to localStorage at game over', async function () {
    store(null);
    fresh(5);
    expect(S().highScore).toBe(0);
    eatTwiceThenCrash();
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.score).toBe(20);
    expect(s.highScore).toBe(20);
    expect(localStorage.getItem(KEY)).toBe('20');
  });

  KyoraTest.test('b23-t10', 'the stored best is loaded by start()', async function () {
    store('990');
    var s = fresh(6);
    expect(s.highScore).toBe(990);
    expect(s.score).toBe(0);
  });

  KyoraTest.test('b23-t11', 'a worse game never lowers the best', async function () {
    store('990');
    fresh(7);
    eatTwiceThenCrash();
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.highScore).toBe(990);
    expect(localStorage.getItem(KEY)).toBe('990');
    G().start();
    expect(S().highScore).toBe(990);
    store(null);
  });

  KyoraTest.test('b23-t12', 'screenshot of the paused game', async function () {
    fresh(8);
    moves(2);
    G().input.togglePause();
    await T.wait(50);
    var d = await T.screenshot('b23-paused');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
