# Breakout

A dependency-free canvas Breakout game (480 x 640 field, 6 rows of 8 bricks, 3 lives).

- Open `index.html`. Space starts a game and launches the ball.
- Left / Right arrows (or A / D) move the paddle.
- Higher rows are worth more points. Clearing every brick wins; losing the third ball ends the game.

There are no touch controls yet.

## Test API (`window.Game`)

| Call | Meaning |
|---|---|
| `Game.start()` | new game, phase `'play'`, ball resting on the paddle |
| `Game.getState()` | `{phase, score, lives, won, width, height, paddleX, paddleW, ballX, ballY, ballVX, ballVY, ballAttached, bricks, brickRects}` |
| `Game.setSeed(n)` | seed for the launch-angle PRNG (applies at the next `start()`) |
| `Game.step(n)` | advance `n` fixed 1/60 s ticks; the first call switches to a manual clock |
| `Game.input.press(name)` / `Game.input.release(name)` | hold / release `'left'` or `'right'` |
| `Game.input.action()` | launch the ball |
| `Game.debug.setBall(x, y, vx, vy)` | detach the ball and place it with a velocity per tick |
| `Game.debug.keepBricks(n)` | keep only the first `n` bricks |
