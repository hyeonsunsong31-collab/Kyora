/* Kyora Bench b14-endless-runner - acceptance tests owned by the bench.
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
    G().debug.clearObstacles();
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
  function stepUntil(pred, maxTicks) {
    for (var i = 0; i < maxTicks; i++) {
      if (pred(S())) return true;
      G().step(1);
    }
    return pred(S());
  }
  function height(s) { return s.groundY - (s.player.y + s.player.h); }

  T.test('b14-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.jump', 'input.press', 'input.release',
      'debug.setSpawning', 'debug.clearObstacles', 'debug.spawnObstacle'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b14-t02', 'start() runs on the ground with nothing in the way', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.speed).toBeGreaterThan(0);
    expect(s.speed).toBeLessThanOrEqual(8);
    expect(s.player.onGround).toBe(true);
    expect(Math.abs(height(s))).toBeLessThanOrEqual(1);
    s.obstacles.forEach(function (o) { expect(o.x).toBeGreaterThan(s.player.x + s.player.w); });
  });

  T.test('b14-t03', 'the score grows with distance', async function () {
    quiet(1);
    G().step(120);
    var a = S().score;
    expect(a).toBeGreaterThan(0);
    G().step(120);
    expect(S().score).toBeGreaterThan(a);
  });

  T.test('b14-t04', 'the speed rises at least 10% within 1800 ticks', async function () {
    var s = quiet(1);
    G().step(1800);
    var a = S();
    expect(a.phase).toBe('play');
    expect(a.speed).toBeGreaterThanOrEqual(s.speed * 1.1 - 1e-9);
  });

  T.test('b14-t05', 'jump() leaves the ground and the runner lands again', async function () {
    quiet(1);
    G().input.jump();
    G().step(1);
    var a = S();
    expect(a.player.vy).toBeLessThan(0);
    expect(a.player.onGround).toBe(false);
    expect(stepUntil(function (x) { return x.player.onGround; }, 150)).toBe(true);
  });

  T.test('b14-t06', 'running into a low obstacle ends the run', async function () {
    var s = quiet(1);
    G().debug.spawnObstacle('low', s.player.x + s.player.w + 100);
    stepUntil(function (x) { return x.phase !== 'play'; }, 300);
    expect(S().phase).toBe('over');
  });

  T.test('b14-t07', 'a well-timed jump clears a low obstacle', async function () {
    var s = quiet(2);
    // measure the obstacle and the jump arc on an empty track
    G().debug.spawnObstacle('low', s.player.x + 5000);
    var ob = S().obstacles[0];
    G().debug.clearObstacles();
    G().input.jump();
    var heights = [];
    for (var t = 1; t <= 200; t++) {
      G().step(1);
      var x = S();
      heights.push(height(x));
      if (x.player.onGround) break;
    }
    var a = -1, b = -1;
    for (var i = 0; i < heights.length; i++) {
      if (heights[i] > ob.h + 0.5) { if (a < 0) a = i + 1; b = i + 1; }
    }
    expect(a).toBeGreaterThan(0);
    var s2 = S();
    var speed = s2.speed;
    var cross = (ob.w + s2.player.w) / speed;
    var slack = (b - a) - cross;
    expect(slack).toBeGreaterThan(1);
    var tIn = a + slack / 2;
    var dist = tIn * speed;
    G().step(10);
    G().debug.spawnObstacle('low', S().player.x + S().player.w + dist);
    G().input.jump();
    var passed = stepUntil(function (x) {
      return x.phase !== 'play' || x.obstacles.every(function (o) { return o.x + o.w < x.player.x; });
    }, 300);
    expect(passed).toBe(true);
    expect(S().phase).toBe('play');
  });

  T.test('b14-t08', 'ducking passes under a high obstacle', async function () {
    var s = quiet(3);
    G().input.press('duck');
    G().step(2);
    expect(S().player.ducking).toBe(true);
    G().debug.spawnObstacle('high', s.player.x + s.player.w + 60);
    var passed = stepUntil(function (x) {
      return x.phase !== 'play' || x.obstacles.every(function (o) { return o.x + o.w < x.player.x; });
    }, 400);
    G().input.release('duck');
    expect(passed).toBe(true);
    expect(S().phase).toBe('play');
    G().step(2);
    expect(S().player.ducking).toBe(false);
  });

  T.test('b14-t09', 'a high obstacle hits a runner who does not duck', async function () {
    var s = quiet(4);
    G().debug.spawnObstacle('high', s.player.x + s.player.w + 60);
    stepUntil(function (x) { return x.phase !== 'play'; }, 400);
    expect(S().phase).toBe('over');
  });

  T.test('b14-t10', 'obstacles spawn by themselves', async function () {
    fresh(5);
    var most = 0;
    for (var i = 0; i < 60 && S().phase === 'play'; i++) {
      G().step(10);
      most = Math.max(most, S().obstacles.length);
    }
    expect(most).toBeGreaterThan(0);
  });

  T.test('b14-t11', 'same seed gives the same course', async function () {
    function run() {
      fresh(13);
      G().step(400);
      var s = S();
      return {
        phase: s.phase, score: s.score,
        obstacles: s.obstacles.map(function (o) { return [o.type, Math.round(o.x * 100) / 100]; })
      };
    }
    expect(run()).toEqual(run());
  });

  T.test('b14-t12', 'Space jumps and ArrowDown ducks', async function () {
    quiet(6);
    var p = T.input.key('ArrowDown', 300);
    await T.wait(80);
    G().step(2);
    expect(S().player.ducking).toBe(true);
    await p;
    G().step(2);
    await T.input.key('Space', 60);
    G().step(1);
    expect(S().player.onGround).toBe(false);
  });

  T.test('b14-t13', 'tapping the upper half of the canvas jumps', async function () {
    quiet(7);
    var b = box();
    await T.input.touch(b.w / 2, b.h * 0.2);
    G().step(1);
    expect(S().player.onGround).toBe(false);
  });

  T.test('b14-t14', 'screenshot of the running game', async function () {
    fresh(8);
    G().step(120);
    await T.wait(50);
    var d = await T.screenshot('b14-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
