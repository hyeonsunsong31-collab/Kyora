/* Kyora Bench b17-idle-clicker - acceptance tests owned by the bench.
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
  function up(s, id) { return s.upgrades.filter(function (u) { return u.id === id; })[0]; }
  function close(a, b, tol) { return Math.abs(a - b) <= tol; }

  KyoraTest.test('b17-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.click', 'input.buy', 'buttonCenter',
      'debug.addCoins'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b17-t02', 'start() begins with no coins and three upgrades at their base prices', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.coins).toBe(0);
    expect(s.totalEarned).toBe(0);
    expect(s.score).toBe(0);
    expect(s.perClick).toBe(1);
    expect(s.perSecond).toBe(0);
    expect([up(s, 'power').cost, up(s, 'worker').cost, up(s, 'factory').cost]).toEqual([10, 15, 100]);
    s.upgrades.forEach(function (u) { expect(u.owned).toBe(0); });
  });

  KyoraTest.test('b17-t03', 'clicking earns perClick coins', async function () {
    fresh(1);
    for (var i = 0; i < 5; i++) G().input.click();
    var s = S();
    expect(s.coins).toBe(5);
    expect(s.totalEarned).toBe(5);
    expect(s.score).toBe(5);
  });

  KyoraTest.test('b17-t04', 'an upgrade cannot be bought without enough coins', async function () {
    fresh(1);
    G().debug.addCoins(14);
    expect(G().input.buy('worker')).toBe(false);
    var s = S();
    expect(s.coins).toBe(14);
    expect(up(s, 'worker').owned).toBe(0);
  });

  KyoraTest.test('b17-t05', 'buying a worker spends its price, raises income and the next price', async function () {
    fresh(1);
    G().debug.addCoins(15);
    expect(G().input.buy('worker')).toBe(true);
    var s = S();
    expect(s.coins).toBe(0);
    expect(up(s, 'worker').owned).toBe(1);
    expect(up(s, 'worker').cost).toBe(18);
    expect(s.perSecond).toBe(1);
  });

  KyoraTest.test('b17-t06', 'income accrues every tick', async function () {
    fresh(1);
    G().debug.addCoins(15);
    G().input.buy('worker');
    G().step(600);
    var s = S();
    expect(close(s.coins, 10, 0.01)).toBe(true);
    expect(close(s.totalEarned, 25, 0.01)).toBe(true);
  });

  KyoraTest.test('b17-t07', 'the power upgrade raises coins per click', async function () {
    fresh(1);
    G().debug.addCoins(10);
    expect(G().input.buy('power')).toBe(true);
    expect(S().perClick).toBe(2);
    G().input.click();
    expect(S().coins).toBe(2);
  });

  KyoraTest.test('b17-t08', 'prices grow by 15% per purchase, rounded up', async function () {
    fresh(1);
    G().debug.addCoins(1000);
    G().input.buy('factory');
    expect(up(S(), 'factory').cost).toBe(115);
    G().input.buy('factory');
    expect(up(S(), 'factory').cost).toBe(133);
    G().input.buy('factory');
    var s = S();
    expect(up(s, 'factory').cost).toBe(153);
    expect(up(s, 'factory').owned).toBe(3);
    expect(s.coins).toBe(1000 - 100 - 115 - 133);
    expect(s.perSecond).toBe(24);
  });

  KyoraTest.test('b17-t09', 'earning one million in total wins', async function () {
    fresh(1);
    G().debug.addCoins(1000000);
    G().step(1);
    var s = S();
    expect(s.phase).toBe('over');
    expect(s.score).toBeGreaterThanOrEqual(1000000);
  });

  KyoraTest.test('b17-t10', 'clicking the coin on the canvas earns coins', async function () {
    fresh(2);
    var p = G().buttonCenter('coin');
    await T.input.click(p.x, p.y);
    await T.wait(30);
    expect(S().coins).toBeGreaterThanOrEqual(1);
  });

  KyoraTest.test('b17-t11', 'tapping an upgrade button buys it', async function () {
    fresh(3);
    G().debug.addCoins(15);
    var p = G().buttonCenter('worker');
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(up(S(), 'worker').owned).toBe(1);
  });

  KyoraTest.test('b17-t12', 'Space clicks and Digit2 buys a worker', async function () {
    fresh(4);
    await T.input.key('Space', 60);
    expect(S().coins).toBeGreaterThanOrEqual(1);
    G().debug.addCoins(15);
    await T.input.key('Digit2', 60);
    expect(up(S(), 'worker').owned).toBeGreaterThanOrEqual(1);
  });

  KyoraTest.test('b17-t13', 'the same actions give the same state', async function () {
    function run() {
      fresh(5);
      for (var i = 0; i < 30; i++) G().input.click();
      G().input.buy('worker');
      G().input.buy('power');
      G().step(1234);
      var s = S();
      return [Math.round(s.coins * 1000), Math.round(s.totalEarned * 1000), s.perClick, s.perSecond];
    }
    expect(run()).toEqual(run());
  });

  KyoraTest.test('b17-t14', 'screenshot of the running game', async function () {
    fresh(6);
    G().debug.addCoins(200);
    G().input.buy('worker');
    G().step(60);
    await T.wait(50);
    var d = await T.screenshot('b17-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
