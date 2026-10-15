/* Breakout - keyboard-controlled canvas game.
   window.Game exposes a deterministic API (seeded PRNG + fixed 1/60 s ticks) used by automated tests. */
(function () {
  'use strict';

  var W = 480;
  var H = 640;
  var PADDLE_W = 80;
  var PADDLE_H = 12;
  var PADDLE_Y = H - 40;          // centre line of the paddle
  var PADDLE_SPEED = 7;
  var BALL_R = 6;
  var BALL_SPEED = 6;
  var BRICK_COLS = 8;
  var BRICK_ROWS = 6;
  var BRICK_W = 52;
  var BRICK_H = 18;
  var BRICK_GAP = 4;
  var BRICK_TOP = 80;
  var BRICK_LEFT = (W - (BRICK_COLS * BRICK_W + (BRICK_COLS - 1) * BRICK_GAP)) / 2;
  var ROW_COLOURS = ['#f85149', '#f0883e', '#d29922', '#3fb950', '#58a6ff', '#bc8cff'];

  var canvas = document.getElementById('game');
  var ctx = canvas.getContext('2d');

  // ---------------------------------------------------------------- seeded PRNG (mulberry32)
  var seed = 1;
  var rng = 1;
  function random() {
    rng = (rng + 0x6D2B79F5) | 0;
    var t = Math.imul(rng ^ (rng >>> 15), 1 | rng);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  // ---------------------------------------------------------------- state
  var phase = 'menu';
  var score = 0;
  var lives = 3;
  var won = false;
  var paddle = { x: W / 2, y: PADDLE_Y, w: PADDLE_W, h: PADDLE_H };
  var ball = { x: W / 2, y: PADDLE_Y - PADDLE_H / 2 - BALL_R, vx: 0, vy: 0, attached: true };
  var bricks = [];
  var holding = { left: false, right: false };
  var manual = false;

  function buildBricks() {
    bricks = [];
    for (var r = 0; r < BRICK_ROWS; r++) {
      for (var c = 0; c < BRICK_COLS; c++) {
        bricks.push({ x: BRICK_LEFT + c * (BRICK_W + BRICK_GAP), y: BRICK_TOP + r * (BRICK_H + BRICK_GAP),
          w: BRICK_W, h: BRICK_H, row: r });
      }
    }
  }

  function attachBall() {
    ball.attached = true;
    ball.vx = 0;
    ball.vy = 0;
    ball.x = paddle.x;
    ball.y = paddle.y - paddle.h / 2 - BALL_R;
  }

  function start() {
    rng = seed;
    score = 0;
    lives = 3;
    won = false;
    paddle.x = W / 2;
    holding.left = holding.right = false;
    buildBricks();
    attachBall();
    phase = 'play';
  }

  function launch() {
    if (phase !== 'play' || !ball.attached) return;
    var angle = (random() - 0.5) * 1.0;
    ball.attached = false;
    ball.vx = BALL_SPEED * Math.sin(angle);
    ball.vy = -BALL_SPEED * Math.cos(angle);
  }

  function loseLife() {
    lives -= 1;
    if (lives <= 0) {
      lives = 0;
      phase = 'over';
      won = false;
    }
    attachBall();
  }

  function hitPaddle() {
    if (ball.vy <= 0) return false;
    var top = paddle.y - paddle.h / 2;
    var bottom = paddle.y + paddle.h / 2;
    if (ball.y + BALL_R < top || ball.y - BALL_R > bottom) return false;
    var halfW = paddle.w / 2;
    var left = paddle.x - halfW;
    var right = left + halfW;
    if (ball.x < left - BALL_R || ball.x > right + BALL_R) return false;
    var offset = Math.max(-1, Math.min(1, (ball.x - paddle.x) / halfW));
    var speed = Math.hypot(ball.vx, ball.vy) || BALL_SPEED;
    var angle = offset * 1.0;
    ball.vx = speed * Math.sin(angle);
    ball.vy = -speed * Math.cos(angle);
    ball.y = top - BALL_R;
    return true;
  }

  function hitBrick() {
    for (var i = 0; i < bricks.length; i++) {
      var b = bricks[i];
      var nx = Math.max(b.x, Math.min(ball.x, b.x + b.w));
      var ny = Math.max(b.y, Math.min(ball.y, b.y + b.h));
      var dx = ball.x - nx;
      var dy = ball.y - ny;
      if (dx * dx + dy * dy > BALL_R * BALL_R) continue;
      var overlapX = Math.min(ball.x + BALL_R - b.x, b.x + b.w - (ball.x - BALL_R));
      var overlapY = Math.min(ball.y + BALL_R - b.y, b.y + b.h - (ball.y - BALL_R));
      if (overlapX < overlapY) ball.vx = -ball.vx;
      else ball.vy = -ball.vy;
      score += 10 * (BRICK_ROWS - b.row);
      bricks.splice(i, 1);
      if (bricks.length === 0) {
        phase = 'over';
        won = true;
      }
      return true;
    }
    return false;
  }

  function update() {
    if (phase !== 'play') return;
    var dir = (holding.right ? 1 : 0) - (holding.left ? 1 : 0);
    paddle.x = Math.max(paddle.w / 2, Math.min(W - paddle.w / 2, paddle.x + dir * PADDLE_SPEED));
    if (ball.attached) {
      ball.x = paddle.x;
      ball.y = paddle.y - paddle.h / 2 - BALL_R;
      return;
    }
    ball.x += ball.vx;
    ball.y += ball.vy;
    if (ball.x - BALL_R < 0) { ball.x = BALL_R; ball.vx = Math.abs(ball.vx); }
    if (ball.x + BALL_R > W) { ball.x = W - BALL_R; ball.vx = -Math.abs(ball.vx); }
    if (ball.y - BALL_R < 0) { ball.y = BALL_R; ball.vy = Math.abs(ball.vy); }
    if (hitPaddle()) return;
    hitBrick();
    if (phase === 'play' && ball.y - BALL_R > H) loseLife();
  }

  // ---------------------------------------------------------------- rendering
  function render() {
    ctx.fillStyle = '#161b22';
    ctx.fillRect(0, 0, W, H);
    for (var i = 0; i < bricks.length; i++) {
      var b = bricks[i];
      ctx.fillStyle = ROW_COLOURS[b.row % ROW_COLOURS.length];
      ctx.fillRect(b.x, b.y, b.w, b.h);
    }
    ctx.fillStyle = '#e6edf3';
    ctx.fillRect(paddle.x - paddle.w / 2, paddle.y - paddle.h / 2, paddle.w, paddle.h);
    ctx.beginPath();
    ctx.arc(ball.x, ball.y, BALL_R, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = 'bold 18px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('Score ' + score, 12, 32);
    ctx.textAlign = 'right';
    ctx.fillText('Lives ' + lives, W - 12, 32);
    if (phase !== 'play') {
      ctx.fillStyle = 'rgba(0,0,0,0.6)';
      ctx.fillRect(0, H / 2 - 50, W, 100);
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.font = 'bold 30px system-ui, sans-serif';
      ctx.fillText(phase === 'menu' ? 'BREAKOUT' : (won ? 'YOU WIN' : 'GAME OVER'), W / 2, H / 2 - 4);
      ctx.font = '16px system-ui, sans-serif';
      ctx.fillText('Press Space to ' + (phase === 'menu' ? 'start' : 'play again'), W / 2, H / 2 + 28);
    }
  }

  // ---------------------------------------------------------------- keyboard
  var held = {};
  window.addEventListener('keydown', function (e) {
    var code = e.code;
    if (code === 'ArrowLeft' || code === 'KeyA') { holding.left = true; e.preventDefault(); return; }
    if (code === 'ArrowRight' || code === 'KeyD') { holding.right = true; e.preventDefault(); return; }
    if (code === 'Space') {
      e.preventDefault();
      if (held[code]) return;
      held[code] = true;
      if (phase !== 'play') start();
      else launch();
    }
  });
  window.addEventListener('keyup', function (e) {
    var code = e.code;
    delete held[code];
    if (code === 'ArrowLeft' || code === 'KeyA') holding.left = false;
    if (code === 'ArrowRight' || code === 'KeyD') holding.right = false;
  });
  window.addEventListener('blur', function () { held = {}; holding.left = holding.right = false; });

  // ---------------------------------------------------------------- loop
  var last = 0;
  var acc = 0;
  var STEP_MS = 1000 / 60;
  function frame(now) {
    if (!last) last = now;
    var dt = Math.min(250, now - last);
    last = now;
    if (!manual) {
      acc += dt;
      while (acc >= STEP_MS) {
        update();
        acc -= STEP_MS;
      }
    }
    render();
    requestAnimationFrame(frame);
  }
  buildBricks();
  requestAnimationFrame(frame);
  render();

  // ---------------------------------------------------------------- public API
  window.Game = {
    start: start,
    getState: function () {
      return {
        phase: phase, score: score, lives: lives, won: won, width: W, height: H,
        paddleX: paddle.x, paddleW: paddle.w,
        ballX: ball.x, ballY: ball.y, ballVX: ball.vx, ballVY: ball.vy, ballAttached: ball.attached,
        bricks: bricks.length,
        brickRects: bricks.map(function (b) { return { x: b.x, y: b.y, w: b.w, h: b.h }; })
      };
    },
    setSeed: function (n) { seed = (Number(n) >>> 0) || 1; },
    step: function (n) {
      manual = true;
      var count = Math.max(0, Math.floor(Number(n) || 0));
      for (var i = 0; i < count; i++) update();
    },
    input: {
      press: function (name) { if (name === 'left' || name === 'right') holding[name] = true; },
      release: function (name) { if (name === 'left' || name === 'right') holding[name] = false; },
      action: launch
    },
    debug: {
      setBall: function (x, y, vx, vy) {
        ball.attached = false;
        ball.x = x; ball.y = y; ball.vx = vx; ball.vy = vy;
      },
      keepBricks: function (n) { bricks = bricks.slice(0, Math.max(0, n)); }
    }
  };
})();
