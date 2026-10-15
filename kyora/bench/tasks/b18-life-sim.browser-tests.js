/* Kyora Bench b18-life-sim - acceptance tests owned by the bench.
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
  function missing(root, names) {
    return names.filter(function (path) {
      var cur = root;
      path.split('.').forEach(function (k) { cur = cur == null ? undefined : cur[k]; });
      return typeof cur !== 'function';
    });
  }
  function near(a, b) { return Math.abs(a - b) <= 0.01; }
  var HOUR = 60;

  KyoraTest.test('b18-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.feed', 'input.play', 'input.work',
      'input.sleep', 'buttonCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b18-t02', 'start() wakes the creature on day 1 at 08:00', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.day).toBe(1);
    expect(s.hour).toBe(8);
    expect(s.score).toBe(0);
    expect([s.hunger, s.energy, s.fun]).toEqual([80, 80, 80]);
    expect(s.coins).toBe(20);
    expect(s.asleep).toBe(false);
  });

  KyoraTest.test('b18-t03', 'each in-game hour (60 ticks) the needs decay', async function () {
    fresh(1);
    G().step(HOUR);
    var s = S();
    expect(s.hour).toBe(9);
    expect(near(s.hunger, 77)).toBe(true);
    expect(near(s.energy, 78)).toBe(true);
    expect(near(s.fun, 78)).toBe(true);
  });

  KyoraTest.test('b18-t04', 'feeding costs 5 coins, adds 25 hunger and is capped at 100', async function () {
    fresh(1);
    expect(G().input.feed()).toBe(true);
    var s = S();
    expect(s.hunger).toBe(100);
    expect(s.coins).toBe(15);
    G().input.feed();
    G().input.feed();
    G().input.feed();
    expect(S().coins).toBe(0);
    expect(G().input.feed()).toBe(false);
    expect(S().coins).toBe(0);
  });

  KyoraTest.test('b18-t05', 'working earns coins and costs energy and fun', async function () {
    fresh(1);
    expect(G().input.work()).toBe(true);
    var s = S();
    expect(s.coins).toBe(30);
    expect(s.energy).toBe(65);
    expect(s.fun).toBe(70);
  });

  KyoraTest.test('b18-t06', 'playing raises fun and costs energy', async function () {
    fresh(1);
    expect(G().input.play()).toBe(true);
    var s = S();
    expect(s.fun).toBe(100);
    expect(s.energy).toBe(70);
  });

  KyoraTest.test('b18-t07', 'sleeping restores energy while hunger keeps falling', async function () {
    fresh(1);
    G().input.sleep();
    expect(S().asleep).toBe(true);
    expect(G().input.play()).toBe(false);
    expect(G().input.work()).toBe(false);
    G().step(2 * HOUR);
    var s = S();
    expect(near(s.energy, 96)).toBe(true);
    expect(near(s.hunger, 74)).toBe(true);
    G().input.sleep();
    expect(S().asleep).toBe(false);
  });

  KyoraTest.test('b18-t08', 'midnight starts a new day and scores it', async function () {
    fresh(1);
    G().step(16 * HOUR);
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.day).toBe(2);
    expect(s.hour).toBe(0);
    expect(s.score).toBe(1);
  });

  KyoraTest.test('b18-t09', 'neglect ends the game', async function () {
    fresh(1);
    G().step(40 * HOUR);
    var s = S();
    expect(s.phase).toBe('over');
    expect(Math.min(s.hunger, s.energy, s.fun)).toBeLessThanOrEqual(0);
  });

  KyoraTest.test('b18-t10', 'same seed and same care give the same days', async function () {
    function run() {
      fresh(2);
      G().step(10 * HOUR);
      G().input.feed();
      G().input.play();
      G().step(10 * HOUR);
      G().input.feed();
      G().input.work();
      G().step(10 * HOUR);
      var s = S();
      return [s.phase, s.day, s.hour, s.coins, Math.round(s.hunger * 100), Math.round(s.energy * 100), Math.round(s.fun * 100)];
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b18-t11', 'the F key feeds', async function () {
    fresh(3);
    G().step(4 * HOUR);
    var before = S().hunger;
    await T.input.key('KeyF', 60);
    expect(S().hunger).toBeGreaterThan(before);
  });

  KyoraTest.test('b18-t12', 'clicking the feed button feeds', async function () {
    fresh(4);
    G().step(4 * HOUR);
    var before = S().hunger;
    var p = G().buttonCenter('feed');
    await T.input.click(p.x, p.y);
    await T.wait(30);
    expect(S().hunger).toBeGreaterThan(before);
  });

  KyoraTest.test('b18-t13', 'tapping the play button plays', async function () {
    fresh(5);
    G().step(4 * HOUR);
    var before = S().fun;
    var p = G().buttonCenter('play');
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().fun).toBeGreaterThan(before);
  });

  KyoraTest.test('b18-t14', 'screenshot of the running game', async function () {
    fresh(6);
    G().step(3 * HOUR);
    await T.wait(50);
    var d = await T.screenshot('b18-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
