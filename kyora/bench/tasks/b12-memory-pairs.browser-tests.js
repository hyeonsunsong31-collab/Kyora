/* Kyora Bench b12-memory-pairs - acceptance tests owned by the bench.
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
  function partnerOf(faces, i) {
    for (var j = 0; j < faces.length; j++) if (j !== i && faces[j] === faces[i]) return j;
    return -1;
  }
  function strangerOf(faces, i) {
    for (var j = 0; j < faces.length; j++) if (faces[j] !== faces[i]) return j;
    return -1;
  }

  T.test('b12-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.flip', 'cardCenter',
      'debug.faces'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b12-t02', 'start() deals 16 face-down cards: 8 pairs', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.score).toBe(0);
    expect(s.moves).toBe(0);
    expect(s.matched).toBe(0);
    expect(s.cards.length).toBe(16);
    s.cards.forEach(function (c) { expect(c.state).toBe('down'); });
    var faces = G().debug.faces();
    expect(faces.length).toBe(16);
    var counts = {};
    faces.forEach(function (f) { counts[f] = (counts[f] || 0) + 1; });
    expect(Object.keys(counts).sort()).toEqual(['0', '1', '2', '3', '4', '5', '6', '7']);
    Object.keys(counts).forEach(function (k) { expect(counts[k]).toBe(2); });
  });

  T.test('b12-t03', 'flipping turns a card up; flipping it again is ignored', async function () {
    fresh(1);
    expect(G().input.flip(0)).toBe(true);
    expect(S().cards[0].state).toBe('up');
    expect(G().input.flip(0)).toBe(false);
    expect(S().cards[0].state).toBe('up');
    expect(S().moves).toBe(0);
  });

  T.test('b12-t04', 'a matching pair stays matched and scores', async function () {
    fresh(1);
    var faces = G().debug.faces();
    var j = partnerOf(faces, 0);
    G().input.flip(0);
    G().input.flip(j);
    var s = S();
    expect(s.cards[0].state).toBe('matched');
    expect(s.cards[j].state).toBe('matched');
    expect(s.matched).toBe(1);
    expect(s.moves).toBe(1);
    expect(s.score).toBeGreaterThan(0);
    expect(G().input.flip(0)).toBe(false);
  });

  T.test('b12-t05', 'a mismatched pair turns back face down after 45 ticks', async function () {
    fresh(1);
    var faces = G().debug.faces();
    var k = strangerOf(faces, 0);
    var other = -1;
    for (var m = 1; m < 16; m++) if (m !== k) { other = m; break; }
    G().input.flip(0);
    G().input.flip(k);
    var s = S();
    expect(s.cards[0].state).toBe('up');
    expect(s.cards[k].state).toBe('up');
    expect(s.moves).toBe(1);
    expect(G().input.flip(other)).toBe(false);
    G().step(60);
    s = S();
    expect(s.cards[0].state).toBe('down');
    expect(s.cards[k].state).toBe('down');
    expect(s.cards[other].state).toBe('down');
    expect(s.score).toBe(0);
  });

  T.test('b12-t06', 'matching all pairs ends the game', async function () {
    fresh(2);
    var faces = G().debug.faces();
    var done = {};
    for (var i = 0; i < 16; i++) {
      if (done[i]) continue;
      var j = partnerOf(faces, i);
      G().input.flip(i);
      G().input.flip(j);
      done[i] = done[j] = true;
    }
    var s = S();
    expect(s.matched).toBe(8);
    expect(s.moves).toBe(8);
    expect(s.phase).toBe('over');
    s.cards.forEach(function (c) { expect(c.state).toBe('matched'); });
  });

  T.test('b12-t07', 'the layout depends on the seed and only on the seed', async function () {
    fresh(3);
    var a = G().debug.faces().join(',');
    fresh(3);
    var b = G().debug.faces().join(',');
    expect(a).toBe(b);
    var layouts = {};
    [3, 4, 5, 6].forEach(function (seed) { fresh(seed); layouts[G().debug.faces().join(',')] = true; });
    expect(Object.keys(layouts).length).toBeGreaterThan(1);
  });

  T.test('b12-t08', 'clicking a card flips it', async function () {
    fresh(4);
    var p = G().cardCenter(5);
    await T.input.click(p.x, p.y);
    await T.wait(30);
    expect(S().cards[5].state).toBe('up');
  });

  T.test('b12-t09', 'tapping a card flips it', async function () {
    fresh(5);
    var p = G().cardCenter(10);
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().cards[10].state).toBe('up');
  });

  T.test('b12-t10', 'start() deals a new game', async function () {
    fresh(6);
    var faces = G().debug.faces();
    G().input.flip(0);
    G().input.flip(partnerOf(faces, 0));
    G().start();
    var s = S();
    expect(s.score).toBe(0);
    expect(s.matched).toBe(0);
    s.cards.forEach(function (c) { expect(c.state).toBe('down'); });
  });

  T.test('b12-t11', 'screenshot of the running game', async function () {
    fresh(7);
    G().input.flip(3);
    await T.wait(50);
    var d = await T.screenshot('b12-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
