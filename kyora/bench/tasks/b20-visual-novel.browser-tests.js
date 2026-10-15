/* Kyora Bench b20-visual-novel - acceptance tests owned by the bench.
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
  function pos(s) { return s.scene + '#' + s.line; }
  // advance until choices are shown or the story ends
  function toChoices(limit) {
    for (var i = 0; i < (limit || 400); i++) {
      var s = S();
      if (s.phase !== 'play' || s.choices.length > 0) return s;
      G().input.advance();
      G().step(1);
    }
    return S();
  }
  // play to an ending, always picking choice index pick(choices)
  function playthrough(seed, pick) {
    fresh(seed);
    var choicePoints = 0;
    for (var i = 0; i < 3000; i++) {
      var s = S();
      if (s.phase === 'over') break;
      if (s.choices.length > 0) {
        G().input.choose(pick(s.choices));
        choicePoints++;
      } else {
        G().input.advance();
      }
      G().step(1);
    }
    var e = S();
    return { ending: e.ending, phase: e.phase, score: e.score, choicePoints: choicePoints };
  }
  function first() { return 0; }
  function last(c) { return c.length - 1; }

  KyoraTest.test('b20-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.advance', 'input.choose', 'save', 'load',
      'choiceCenter'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  KyoraTest.test('b20-t02', 'start() shows the first line with a speaker', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(typeof s.speaker).toBe('string');
    expect(s.speaker.length).toBeGreaterThan(0);
    expect(typeof s.text).toBe('string');
    expect(s.text.length).toBeGreaterThan(2);
    expect(s.visibleChars).toBeLessThan(s.text.length);
    expect(s.choices).toEqual([]);
    expect(s.ending).toBe(null);
  });

  KyoraTest.test('b20-t03', 'text types out over ticks and advance() completes it first', async function () {
    var s = fresh(1);
    G().step(1);
    var v1 = S().visibleChars;
    expect(v1).toBeGreaterThan(s.visibleChars);
    expect(v1).toBeLessThanOrEqual(s.visibleChars + 2);
    G().input.advance();
    var a = S();
    expect(pos(a)).toBe(pos(s));
    expect(a.visibleChars).toBe(a.text.length);
    G().input.advance();
    var b = S();
    expect(pos(b) !== pos(a) || b.choices.length > 0).toBe(true);
  });

  KyoraTest.test('b20-t04', 'choices appear and advance() does not skip them', async function () {
    fresh(1);
    var s = toChoices();
    expect(s.phase).toBe('play');
    expect(s.choices.length).toBeGreaterThanOrEqual(2);
    expect(s.choices.length).toBeLessThanOrEqual(3);
    s.choices.forEach(function (c) { expect(c.text.length).toBeGreaterThan(0); });
    G().input.advance();
    G().step(5);
    var a = S();
    expect(pos(a)).toBe(pos(s));
    expect(a.choices.length).toBe(s.choices.length);
  });

  KyoraTest.test('b20-t05', 'choose() takes a valid option and rejects an invalid one', async function () {
    fresh(1);
    var s = toChoices();
    expect(G().input.choose(s.choices.length + 3)).toBe(false);
    expect(S().choices.length).toBe(s.choices.length);
    expect(G().input.choose(0)).toBe(true);
    var a = S();
    expect(a.choices.length).toBe(0);
    expect(a.phase === 'over' || pos(a) !== pos(s)).toBe(true);
  });

  KyoraTest.test('b20-t06', 'different choices reach different endings', async function () {
    var a = playthrough(2, first);
    var b = playthrough(2, last);
    expect(a.phase).toBe('over');
    expect(b.phase).toBe('over');
    expect(typeof a.ending).toBe('string');
    expect(typeof b.ending).toBe('string');
    expect(a.ending.length).toBeGreaterThan(0);
    expect(a.ending === b.ending).toBe(false);
    expect(a.choicePoints).toBeGreaterThanOrEqual(2);
    expect(b.choicePoints).toBeGreaterThanOrEqual(2);
  });

  KyoraTest.test('b20-t07', 'a middle path reaches a third ending', async function () {
    var seen = {};
    seen[playthrough(3, first).ending] = true;
    seen[playthrough(3, last).ending] = true;
    var alternate = 0;
    seen[playthrough(3, function (c) { alternate++; return alternate % 2 ? c.length - 1 : 0; }).ending] = true;
    var alternate2 = 0;
    seen[playthrough(3, function (c) { alternate2++; return alternate2 % 2 ? 0 : c.length - 1; }).ending] = true;
    seen[playthrough(3, function (c) { return Math.floor((c.length - 1) / 2); }).ending] = true;
    expect(Object.keys(seen).length).toBeGreaterThanOrEqual(3);
  });

  KyoraTest.test('b20-t08', 'save() and load() restore the position through localStorage', async function () {
    fresh(4);
    for (var i = 0; i < 3; i++) { G().input.advance(); G().input.advance(); }
    var saved = S();
    expect(G().save(1)).toBe(true);
    expect(localStorage.getItem('kyora-vn-1')).toBeTruthy();
    toChoices();
    G().input.choose(0);
    for (var j = 0; j < 4; j++) { G().input.advance(); G().input.advance(); }
    expect(G().load(1)).toBe(true);
    var back = S();
    expect(pos(back)).toBe(pos(saved));
    expect(back.text).toBe(saved.text);
    expect(G().load(9)).toBe(false);
  });

  KyoraTest.test('b20-t09', 'the history grows as lines are read', async function () {
    var s = fresh(5);
    for (var i = 0; i < 3; i++) { G().input.advance(); G().input.advance(); }
    expect(S().history).toBeGreaterThan(s.history);
  });

  KyoraTest.test('b20-t10', 'the same choices give the same story', async function () {
    var a = playthrough(6, last);
    var b = playthrough(6, last);
    expect(a).toEqual(b);
  });

  KyoraTest.test('b20-t11', 'Space advances', async function () {
    var s = fresh(7);
    await T.input.key('Space', 60);
    var a = S();
    expect(pos(a) !== pos(s) || a.visibleChars > s.visibleChars).toBe(true);
  });

  KyoraTest.test('b20-t12', 'tapping a choice box chooses it', async function () {
    fresh(8);
    var s = toChoices();
    expect(s.choices.length).toBeGreaterThanOrEqual(2);
    var p = G().choiceCenter(0);
    await T.input.touch(p.x, p.y);
    await T.wait(30);
    expect(S().choices.length).toBe(0);
  });

  KyoraTest.test('b20-t13', 'screenshot of the running game', async function () {
    fresh(9);
    G().step(40);
    await T.wait(50);
    var d = await T.screenshot('b20-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
