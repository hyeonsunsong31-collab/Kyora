/* Block Catcher: a tiny deterministic test game exposing window.Game. */
(function () {
  'use strict';
  var W = 320, H = 480, PADDLE_W = 64, PADDLE_H = 10;
  var seed = 1;
  var state = { score: 0, lives: 3, phase: 'menu' };
  var paddleX = (W - PADDLE_W) / 2;
  var blocks = [];
  var spawnIn = 0;
  var canvas = null, ctx = null, last = 0;

  function rand() {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  function hud() {
    var s = document.getElementById('score');
    var l = document.getElementById('lives');
    var p = document.getElementById('phase');
    if (s) s.textContent = String(state.score);
    if (l) l.textContent = String(state.lives);
    if (p) p.textContent = state.phase;
  }

  function reset() {
    state.score = 0;
    state.lives = 3;
    paddleX = (W - PADDLE_W) / 2;
    blocks = [];
    spawnIn = 0;
  }

  function start() {
    reset();
    state.phase = 'play';
    hud();
  }

  function move(dx) {
    paddleX = Math.max(0, Math.min(W - PADDLE_W, paddleX + dx));
  }

  function action() {
    if (state.phase === 'play') {
      state.score += 1; // bonus point for an action press while playing
    } else {
      start();
    }
    hud();
  }

  function update(dt) {
    if (state.phase !== 'play') return;
    spawnIn -= dt;
    if (spawnIn <= 0) {
      blocks.push({ x: Math.floor(rand() * (W - 20)), y: -20, v: 80 + rand() * 60 });
      spawnIn = 0.9;
    }
    for (var i = blocks.length - 1; i >= 0; i--) {
      var b = blocks[i];
      b.y += b.v * dt;
      var caught = b.y + 20 >= H - 30 && b.y <= H - 30 + PADDLE_H && b.x + 20 >= paddleX && b.x <= paddleX + PADDLE_W;
      if (caught) {
        state.score += 1;
        blocks.splice(i, 1);
      } else if (b.y > H) {
        blocks.splice(i, 1);
        state.lives -= 1;
        if (state.lives <= 0) {
          state.phase = 'over';
        }
      }
    }
    hud();
  }

  function draw() {
    if (!ctx) return;
    ctx.fillStyle = '#1b2030';
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#76b900';
    for (var i = 0; i < blocks.length; i++) ctx.fillRect(blocks[i].x, blocks[i].y, 20, 20);
    ctx.fillStyle = '#e9edf5';
    ctx.fillRect(paddleX, H - 30, PADDLE_W, PADDLE_H);
    if (state.phase !== 'play') {
      ctx.textAlign = 'center';
      ctx.font = '20px sans-serif';
      ctx.fillText(state.phase === 'over' ? 'Game over - tap to retry' : 'Tap or press Space', W / 2, H / 2);
    }
  }

  function frame(t) {
    var dt = last ? Math.min(0.05, (t - last) / 1000) : 0;
    last = t;
    update(dt);
    draw();
    window.requestAnimationFrame(frame);
  }

  function pointerAt(clientX) {
    var r = canvas.getBoundingClientRect();
    var x = (clientX - r.left) * (W / (r.width || W));
    if (state.phase !== 'play') {
      start();
      return;
    }
    move(x < W / 2 ? -24 : 24);
  }

  function boot() {
    canvas = document.getElementById('game') || document.querySelector('canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    window.addEventListener('keydown', function (e) {
      if (e.code === 'ArrowLeft') move(-16);
      else if (e.code === 'ArrowRight') move(16);
      else if (e.code === 'Space') action();
    });
    canvas.addEventListener('touchstart', function (e) {
      if (e.touches && e.touches[0]) pointerAt(e.touches[0].clientX);
      if (e.cancelable) e.preventDefault();
    }, { passive: false });
    canvas.addEventListener('mousedown', function (e) { pointerAt(e.clientX); });
    hud();
    draw();
    window.requestAnimationFrame(frame);
  }

  window.Game = {
    start: start,
    getState: function () { return { score: state.score, lives: state.lives, phase: state.phase }; },
    input: {
      left: function () { move(-16); },
      right: function () { move(16); },
      action: action
    },
    setSeed: function (n) { seed = (Number(n) >>> 0) || 1; }
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
