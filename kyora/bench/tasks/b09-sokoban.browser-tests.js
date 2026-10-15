/* Kyora Bench b09-sokoban - acceptance tests owned by the bench.
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
  function level(lines) {
    fresh(1);
    G().loadLevel(lines.join('\n'));
    return S();
  }
  var CORRIDOR = ['#######', '#@ $ .#', '#######'];

  T.test('b09-t01', 'canvas and the Game contract exist; the page loads in the menu', async function () {
    expect(canvas()).toBeTruthy();
    expect(canvas().width).toBeGreaterThan(0);
    expect(missing(window.Game, ['start', 'getState', 'setSeed', 'step', 'input.move', 'input.undo', 'input.restart',
      'loadLevel'])).toEqual([]);
    expect(S().phase).toBe('menu');
  });

  T.test('b09-t02', 'start() loads built-in level 1', async function () {
    var s = fresh(1);
    expect(s.phase).toBe('play');
    expect(s.level).toBe(1);
    expect(s.moves).toBe(0);
    expect(s.pushes).toBe(0);
    expect(s.solved).toBe(false);
    expect(s.boxes.length).toBeGreaterThanOrEqual(1);
    expect(s.boxes.length).toBe(s.goals.length);
    expect(s.player.x).toBeGreaterThanOrEqual(0);
    expect(s.player.x).toBeLessThan(s.cols);
    expect(s.player.y).toBeLessThan(s.rows);
  });

  T.test('b09-t03', 'loadLevel() parses standard Sokoban text', async function () {
    var s = level(CORRIDOR);
    expect(s.cols).toBe(7);
    expect(s.rows).toBe(3);
    expect(s.player).toEqual({ x: 1, y: 1 });
    expect(s.boxes).toEqual([{ x: 3, y: 1 }]);
    expect(s.goals).toEqual([{ x: 5, y: 1 }]);
    expect(s.solved).toBe(false);
    expect(s.phase).toBe('play');
  });

  T.test('b09-t04', 'walking and pushing update moves and pushes', async function () {
    level(CORRIDOR);
    expect(G().input.move('right')).toBe(true);
    var s = S();
    expect(s.player).toEqual({ x: 2, y: 1 });
    expect(s.moves).toBe(1);
    expect(s.pushes).toBe(0);
    expect(G().input.move('right')).toBe(true);
    s = S();
    expect(s.player).toEqual({ x: 3, y: 1 });
    expect(s.boxes).toEqual([{ x: 4, y: 1 }]);
    expect(s.pushes).toBe(1);
  });

  T.test('b09-t05', 'pushing the last box onto its goal solves the level', async function () {
    level(CORRIDOR);
    G().input.move('right');
    G().input.move('right');
    G().input.move('right');
    var s = S();
    expect(s.boxes).toEqual([{ x: 5, y: 1 }]);
    expect(s.solved).toBe(true);
    expect(s.score).toBe(1);
    expect(s.phase).toBe('over');
  });

  T.test('b09-t06', 'walls block the player', async function () {
    level(['######', '#@#$.#', '######']);
    expect(G().input.move('right')).toBe(false);
    expect(G().input.move('up')).toBe(false);
    var s = S();
    expect(s.player).toEqual({ x: 1, y: 1 });
    expect(s.moves).toBe(0);
  });

  T.test('b09-t07', 'a box cannot be pushed into a wall or another box', async function () {
    level(['########', '#@$$ ..#', '########']);
    expect(G().input.move('right')).toBe(false);
    expect(S().player).toEqual({ x: 1, y: 1 });
    level(['#####', '#.@$#', '#####']);
    expect(G().input.move('right')).toBe(false);
    var s = S();
    expect(s.player).toEqual({ x: 2, y: 1 });
    expect(s.boxes).toEqual([{ x: 3, y: 1 }]);
  });

  T.test('b09-t08', 'undo reverts the last move including the box', async function () {
    level(CORRIDOR);
    G().input.move('right');
    G().input.move('right');
    expect(G().input.undo()).toBe(true);
    var s = S();
    expect(s.player).toEqual({ x: 2, y: 1 });
    expect(s.boxes).toEqual([{ x: 3, y: 1 }]);
    expect(s.moves).toBe(1);
    expect(s.pushes).toBe(0);
  });

  T.test('b09-t09', 'restart puts the level back to its initial layout', async function () {
    level(CORRIDOR);
    G().input.move('right');
    G().input.move('right');
    G().input.restart();
    var s = S();
    expect(s.player).toEqual({ x: 1, y: 1 });
    expect(s.boxes).toEqual([{ x: 3, y: 1 }]);
    expect(s.moves).toBe(0);
  });

  T.test('b09-t10', 'y grows downward: pushing down along a column', async function () {
    level(['###', '#@#', '# #', '#$#', '#.#', '###']);
    G().input.move('down');
    expect(S().player).toEqual({ x: 1, y: 2 });
    G().input.move('down');
    var s = S();
    expect(s.boxes).toEqual([{ x: 1, y: 4 }]);
    expect(s.solved).toBe(true);
  });

  T.test('b09-t11', 'arrow keys move the player', async function () {
    level(CORRIDOR);
    await T.input.key('ArrowRight', 60);
    expect(S().player.x).toBeGreaterThan(1);
    expect(S().moves).toBeGreaterThanOrEqual(1);
  });

  T.test('b09-t12', 'a swipe on the canvas moves the player', async function () {
    level(CORRIDOR);
    var b = box();
    await T.input.touch(b.w * 0.3, b.h / 2, b.w * 0.7, b.h / 2, 150);
    expect(S().player.x).toBeGreaterThan(1);
  });

  T.test('b09-t13', 'start() after a solved level begins level 1 again; screenshot', async function () {
    level(CORRIDOR);
    G().input.move('right');
    G().input.move('right');
    G().input.move('right');
    expect(S().phase).toBe('over');
    G().start();
    var s = S();
    expect(s.phase).toBe('play');
    expect(s.level).toBe(1);
    expect(s.solved).toBe(false);
    await T.wait(50);
    var d = await T.screenshot('b09-play');
    expect(typeof d).toBe('string');
    expect(d.indexOf('data:image/')).toBe(0);
    expect(d.length).toBeGreaterThan(200);
  });
})();
