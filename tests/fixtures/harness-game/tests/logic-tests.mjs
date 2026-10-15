import test from 'node:test';
import assert from 'node:assert/strict';
import { clamp, scoreFor } from '../src/util.mjs';

test('at-09 clamp keeps values in range', () => {
  assert.equal(clamp(5, 0, 3), 3);
  assert.equal(clamp(-1, 0, 3), 0);
});

test('score multiplies by combo', () => {
  assert.equal(scoreFor(4, 2), 8);
});
