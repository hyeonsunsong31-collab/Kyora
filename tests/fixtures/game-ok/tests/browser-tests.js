/* Acceptance tests for Block Catcher, run by the Kyora browser harness (KyoraTest API). */
(function () {
  'use strict';
  var T = window.KyoraTest;
  var expect = T.expect;

  T.test('at-01', 'canvas exists', function () {
    var canvas = document.querySelector('canvas');
    expect(!!canvas).toBeTruthy();
    expect(canvas.width).toBeGreaterThan(0);
  });

  T.test('at-02', 'Game API present', function () {
    expect(typeof window.Game).toBe('object');
    expect(typeof Game.start).toBe('function');
    expect(typeof Game.getState).toBe('function');
    expect(typeof Game.setSeed).toBe('function');
    expect(typeof Game.input.left).toBe('function');
    expect(typeof Game.input.right).toBe('function');
    expect(typeof Game.input.action).toBe('function');
  });

  T.test('at-03', 'start switches to play', async function () {
    Game.setSeed(42);
    Game.start();
    await T.frames(2);
    expect(Game.getState().phase).toBe('play');
    expect(Game.getState().lives).toEqual(3);
  });

  T.test('at-04', 'action increments the score while playing', function () {
    Game.start();
    var before = Game.getState().score;
    Game.input.action();
    expect(Game.getState().score).toBeGreaterThan(before);
  });

  T.test('at-05', 'touch on the canvas keeps a valid phase', async function () {
    await T.input.touch(40, 400);
    await T.input.key('ArrowRight', 30);
    await T.wait(50);
    var phase = Game.getState().phase;
    expect(['menu', 'play', 'over'].indexOf(phase) >= 0).toBeTruthy();
  });

  T.test('at-06', 'screenshot of the canvas', async function () {
    await T.waitFor(function () { return Game.getState().phase === 'play'; }, 2000);
    var shot = T.screenshot('play');
    expect(typeof shot === 'string' && shot.indexOf('data:image/png') === 0).toBeTruthy();
  });
})();
