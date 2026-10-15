/* Browser acceptance tests for the harness fixture game (run by the Kyora harness). */
KyoraTest.test('at-01', 'boots to the menu with a canvas', async (t) => {
  await t.waitFor(() => window.Game);
  t.expect(document.querySelector('canvas')).toBeTruthy();
  t.expect(window.Game.getState().phase).toBe('menu');
  t.expect(window.Game.getState().lives).toEqual(3);
});

KyoraTest.test('at-02', 'Space starts the game', async (t) => {
  window.Game.reset();
  await t.input.key('Space');
  t.expect(window.Game.getState().phase).toBe('play');
});

KyoraTest.test('at-03', 'arrow keys move the player', async (t) => {
  const before = window.Game.getState().x;
  await t.input.key('ArrowRight', 30);
  await t.input.key('ArrowRight', 30);
  t.expect(window.Game.getState().x).toBeGreaterThan(before);
  t.expect(window.Game.getState().x).toBe(before + 20);
});

KyoraTest.test('at-04', 'a tap is a touch pointer and scores', async (t) => {
  const s = window.Game.getState();
  await t.input.touch(40, 60);
  const after = window.Game.getState();
  t.expect(after.taps).toBe(s.taps + 1);
  t.expect(after.score).toBeGreaterThanOrEqual(s.score + 1);
});

KyoraTest.test('at-05', 'a click is a mouse pointer', async (t) => {
  const s = window.Game.getState();
  await t.input.click(100, 100);
  t.expect(window.Game.getState().clicks).toBe(s.clicks + 1);
});

KyoraTest.test('at-06', 'the loop advances frames', async (t) => {
  const f = window.Game.getState().frames;
  await t.frames(3);
  t.expect(window.Game.getState().frames).toBeGreaterThan(f);
});

KyoraTest.test('at-07', 'a swipe is recognised', async (t) => {
  const s = window.Game.getState().swipes;
  await t.input.touch(20, 200, 200, 200, 80);
  t.expect(window.Game.getState().swipes).toBe(s + 1);
});

KyoraTest.test('at-08', 'the canvas can be captured', async (t) => {
  const url = t.screenshot('mid');
  t.expect(url).toContain('data:image/png');
  t.log('screenshot bytes ~' + url.length);
});

KyoraTest.test('at-10', 'the module helpers load', async (t) => {
  await t.waitFor(() => window.GameUtil, 3000);
  t.expect(window.GameUtil.clamp(5, 0, 3)).toBe(3);
  t.expect([1, 2, 3]).toContain(2);
  t.expect({ a: [1, { b: 2 }] }).toEqual({ a: [1, { b: 2 }] });
  t.expect(1).not.toBe(2);
});
