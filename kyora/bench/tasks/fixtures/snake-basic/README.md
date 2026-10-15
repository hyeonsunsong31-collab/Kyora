# Snake

A small dependency-free canvas Snake game.

- Open `index.html` in a browser. Space, Enter or a tap starts a game.
- Arrow keys or WASD steer; on touch screens swipe on the board.
- Eating food scores 10 points and grows the snake; hitting a wall or the snake's own body ends the game.

## Test API (`window.Game`)

| Call | Meaning |
|---|---|
| `Game.start()` | new game, phase `'play'` |
| `Game.getState()` | `{phase, score, length, head:{x,y}, dir, food:{x,y}, cols, rows, ticksPerMove}` |
| `Game.setSeed(n)` | seed for the food placement PRNG (applies at the next `start()`) |
| `Game.step(n)` | advance `n` fixed 1/60 s ticks; the first call switches to a manual clock |
| `Game.input.turn(dir)` | queue a turn (`'up'`, `'down'`, `'left'`, `'right'`); reversing is ignored |
| `Game.debug.setFood(x, y)` | move the food to a cell |

The board is 20 x 20 cells and the snake moves one cell every 8 ticks.
