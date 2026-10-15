/* Snake - a small canvas game.
   window.Game exposes a deterministic API (seeded PRNG + fixed 1/60 s ticks) used by automated tests. */
(function () {
  'use strict';

  var COLS = 20;
  var ROWS = 20;
  var TICKS_PER_MOVE = 8;
  var DIRS = { up: { x: 0, y: -1 }, down: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };
  var OPPOSITE = { up: 'down', down: 'up', left: 'right', right: 'left' };
  var KEYMAP = { ArrowUp: 'up', KeyW: 'up', ArrowDown: 'down', KeyS: 'down', ArrowLeft: 'left', KeyA: 'left',
    ArrowRight: 'right', KeyD: 'right' };

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
  var snake = [];
  var dir = 'right';
  var nextDir = 'right';
  var food = { x: 0, y: 0 };
  var tick = 0;
  var manual = false;

  function occupied(x, y) {
    for (var i = 0; i < snake.length; i++) if (snake[i].x === x && snake[i].y === y) return true;
    return false;
  }

  function placeFood() {
    var free = [];
    for (var y = 0; y < ROWS; y++) for (var x = 0; x < COLS; x++) if (!occupied(x, y)) free.push({ x: x, y: y });
    food = free.length ? free[Math.floor(random() * free.length)] : { x: -1, y: -1 };
  }

  function start() {
    rng = seed;
    var cx = Math.floor(COLS / 2);
    var cy = Math.floor(ROWS / 2);
    snake = [{ x: cx, y: cy }, { x: cx - 1, y: cy }, { x: cx - 2, y: cy }];
    dir = 'right';
    nextDir = 'right';
    score = 0;
    tick = 0;
    phase = 'play';
    placeFood();
  }

  function turn(d) {
    if (phase !== 'play' || !DIRS[d]) return;
    if (d === OPPOSITE[dir]) return;
    nextDir = d;
  }

  function move() {
    dir = nextDir;
    var head = { x: snake[0].x + DIRS[dir].x, y: snake[0].y + DIRS[dir].y };
    if (head.x < 0 || head.y < 0 || head.x >= COLS || head.y >= ROWS) {
      phase = 'over';
      return;
    }
    var eating = head.x === food.x && head.y === food.y;
    var body = eating ? snake : snake.slice(0, snake.length - 1);
    for (var i = 0; i < body.length; i++) {
      if (body[i].x === head.x && body[i].y === head.y) {
        phase = 'over';
        return;
      }
    }
    snake.unshift(head);
    if (eating) {
      score += 10;
      placeFood();
    } else {
      snake.pop();
    }
  }

  function update() {
    if (phase !== 'play') return;
    tick++;
    if (tick % TICKS_PER_MOVE === 0) move();
  }

  // ---------------------------------------------------------------- rendering
  function render() {
    var w = canvas.width;
    var h = canvas.height;
    var cw = w / COLS;
    var ch = h / ROWS;
    ctx.fillStyle = '#18202b';
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = '#1d2733';
    for (var y = 0; y < ROWS; y++) for (var x = (y % 2); x < COLS; x += 2) ctx.fillRect(x * cw, y * ch, cw, ch);
    if (phase !== 'menu') {
      ctx.fillStyle = '#ff5d73';
      ctx.beginPath();
      ctx.arc((food.x + 0.5) * cw, (food.y + 0.5) * ch, Math.min(cw, ch) * 0.38, 0, Math.PI * 2);
      ctx.fill();
      for (var i = snake.length - 1; i >= 0; i--) {
        ctx.fillStyle = i === 0 ? '#9be564' : '#5fbf3a';
        ctx.fillRect(snake[i].x * cw + 1, snake[i].y * ch + 1, cw - 2, ch - 2);
      }
    }
    ctx.fillStyle = '#e8eef5';
    ctx.font = 'bold 16px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('Score ' + score, 8, 20);
    if (phase !== 'play') {
      ctx.fillStyle = 'rgba(0,0,0,0.55)';
      ctx.fillRect(0, h / 2 - 40, w, 80);
      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.font = 'bold 24px system-ui, sans-serif';
      ctx.fillText(phase === 'menu' ? 'SNAKE' : 'GAME OVER', w / 2, h / 2 - 6);
      ctx.font = '15px system-ui, sans-serif';
      ctx.fillText('Press Space or tap to ' + (phase === 'menu' ? 'start' : 'play again'), w / 2, h / 2 + 22);
    }
  }

  // ---------------------------------------------------------------- input
  var held = {};
  window.addEventListener('keydown', function (e) {
    if (held[e.code]) return;
    held[e.code] = true;
    if (KEYMAP[e.code]) {
      e.preventDefault();
      turn(KEYMAP[e.code]);
    } else if ((e.code === 'Space' || e.code === 'Enter') && phase !== 'play') {
      e.preventDefault();
      start();
    }
  });
  window.addEventListener('keyup', function (e) { delete held[e.code]; });
  window.addEventListener('blur', function () { held = {}; });

  var swipe = null;
  function swipeStart(x, y) { swipe = { x: x, y: y }; }
  function swipeEnd(x, y) {
    if (!swipe) return;
    var dx = x - swipe.x;
    var dy = y - swipe.y;
    swipe = null;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 30) {
      if (phase !== 'play') start();
      return;
    }
    if (Math.abs(dx) > Math.abs(dy)) turn(dx > 0 ? 'right' : 'left');
    else turn(dy > 0 ? 'down' : 'up');
  }
  canvas.addEventListener('touchstart', function (e) {
    e.preventDefault();
    var t = e.changedTouches[0];
    swipeStart(t.clientX, t.clientY);
  }, { passive: false });
  canvas.addEventListener('touchmove', function (e) { e.preventDefault(); }, { passive: false });
  canvas.addEventListener('touchend', function (e) {
    e.preventDefault();
    var t = e.changedTouches[0];
    swipeEnd(t.clientX, t.clientY);
  }, { passive: false });
  canvas.addEventListener('mousedown', function (e) { swipeStart(e.clientX, e.clientY); });
  window.addEventListener('mouseup', function (e) { swipeEnd(e.clientX, e.clientY); });

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
  requestAnimationFrame(frame);
  render();

  // ---------------------------------------------------------------- public API
  window.Game = {
    start: start,
    getState: function () {
      return {
        phase: phase,
        score: score,
        length: snake.length,
        head: snake.length ? { x: snake[0].x, y: snake[0].y } : null,
        dir: dir,
        food: { x: food.x, y: food.y },
        cols: COLS,
        rows: ROWS,
        ticksPerMove: TICKS_PER_MOVE
      };
    },
    setSeed: function (n) { seed = (Number(n) >>> 0) || 1; },
    step: function (n) {
      manual = true;
      var count = Math.max(0, Math.floor(Number(n) || 0));
      for (var i = 0; i < count; i++) update();
    },
    input: { turn: turn },
    debug: {
      setFood: function (x, y) { food = { x: x, y: y }; }
    }
  };
})();
