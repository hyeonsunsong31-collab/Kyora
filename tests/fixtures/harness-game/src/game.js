/* Tiny deterministic canvas game used by the Kyora harness e2e test. */
(function () {
  'use strict';
  var canvas = document.getElementById('game');
  var ctx = canvas.getContext('2d');
  var state;
  var swipeStart = null;

  function fresh() {
    return { score: 0, lives: 3, phase: 'menu', x: 160, taps: 0, clicks: 0, swipes: 0, keys: 0, frames: 0 };
  }

  function start() {
    state.phase = 'play';
    return state.phase;
  }

  function draw() {
    ctx.fillStyle = state.phase === 'play' ? '#1b2a4a' : '#2a1b4a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#ffd166';
    ctx.fillRect(state.x - 15, 420, 30, 30);
    ctx.fillStyle = '#ffffff';
    ctx.font = '16px sans-serif';
    ctx.fillText('score ' + state.score + '  lives ' + state.lives, 10, 24);
  }

  function loop() {
    state.frames += 1;
    draw();
    window.requestAnimationFrame(loop);
  }

  window.addEventListener('keydown', function (e) {
    state.keys += 1;
    if (e.code === 'ArrowLeft') state.x = Math.max(15, state.x - 10);
    if (e.code === 'ArrowRight') state.x = Math.min(305, state.x + 10);
    if (e.code === 'Space' && state.phase === 'menu') start();
  });

  canvas.addEventListener('pointerdown', function (e) {
    if (e.pointerType === 'touch') state.taps += 1; else state.clicks += 1;
    swipeStart = { x: e.clientX, y: e.clientY };
    if (state.phase === 'play') state.score += 1;
  });
  canvas.addEventListener('pointerup', function (e) {
    if (swipeStart && Math.abs(e.clientX - swipeStart.x) > 50) state.swipes += 1;
    swipeStart = null;
  });

  state = fresh();
  window.Game = {
    start: start,
    reset: function () { state = fresh(); return state.phase; },
    getState: function () { return JSON.parse(JSON.stringify(state)); }
  };
  draw();
  window.requestAnimationFrame(loop);
})();
