/*
 * Kyora in-page test API (window.KyoraTest).
 *
 * Injected by the Kyora browser harness (/runner/<pid>/<ref>) into the game's
 * preview iframe before tests/browser-tests.js. Vanilla ES2017, no dependencies;
 * works in Safari iOS 15+, Chrome Android and desktop browsers.
 *
 *   KyoraTest.test('at-01', 'boots to the menu', async (t) => {
 *     await t.waitFor(() => window.Game && window.Game.getState().phase === 'menu');
 *     t.expect(window.Game.getState().score).toBe(0);
 *     await t.input.key('Space');
 *     await t.frames(5);
 *   });
 *
 * Coordinates for input.click / input.touch are CSS pixels relative to the
 * top-left corner of the first <canvas> (viewport pixels when there is none).
 */
(function () {
  'use strict';
  if (window.KyoraTest && window.KyoraTest.__version) return;

  var tests = [];
  var logs = [];
  var ids = Object.create(null);
  var startedAt = Date.now();

  function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

  function fmt(v) {
    try {
      if (typeof v === 'string') return JSON.stringify(v);
      if (typeof v === 'function') return '[function ' + (v.name || 'anonymous') + ']';
      if (v instanceof Error) return v.name + ': ' + v.message;
      var s = JSON.stringify(v, function (k, x) {
        if (typeof x === 'number' && !isFinite(x)) return String(x);
        if (x && typeof x === 'object' && x.nodeType) return '[' + (x.nodeName || 'node') + ']';
        return x;
      });
      if (s === undefined) return String(v);
      return s.length > 200 ? s.slice(0, 197) + '...' : s;
    } catch (e) {
      return String(v);
    }
  }

  function log(msg) {
    var line = typeof msg === 'string' ? msg : fmt(msg);
    logs.push('[' + Math.round(Date.now() - startedAt) + 'ms] ' + line);
    if (logs.length > 500) logs.shift();
  }

  window.addEventListener('error', function (e) {
    log('uncaught error: ' + (e && e.message ? e.message : 'error') +
        (e && e.filename ? ' (' + e.filename.split('/').slice(-2).join('/') + ':' + e.lineno + ')' : ''));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason;
    log('unhandled rejection: ' + (r && r.message ? r.message : fmt(r)));
  });

  /* ------------------------------------------------------------ registration */
  function test(id, name, fn) {
    if (typeof name === 'function' && fn === undefined) { fn = name; name = id; }
    var sid = typeof id === 'string' ? id.trim() : '';
    var entry = { id: sid || ('unnamed-' + (tests.length + 1)), name: String(name == null ? sid : name), fn: fn, problem: null };
    if (!sid) entry.problem = 'test id must be a non-empty string';
    else if (typeof fn !== 'function') entry.problem = 'test "' + sid + '" has no test function';
    else if (ids[sid]) {
      ids[sid] += 1;
      entry.id = sid + '#dup' + ids[sid];
      entry.problem = 'duplicate test id "' + sid + '" (ids must be unique)';
    }
    if (sid && !ids[sid]) ids[sid] = 1;
    tests.push(entry);
    return entry;
  }

  /* ------------------------------------------------------------ assertions */
  function deepEqual(a, b, seen) {
    if (a === b) return true;
    if (typeof a === 'number' && typeof b === 'number') return a !== a && b !== b;
    if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
    if (Object.getPrototypeOf(a) !== Object.getPrototypeOf(b)) return false;
    seen = seen || [];
    for (var i = 0; i < seen.length; i++) if (seen[i][0] === a && seen[i][1] === b) return true;
    seen.push([a, b]);
    if (a instanceof Date) return a.getTime() === b.getTime();
    if (a instanceof RegExp) return String(a) === String(b);
    if (typeof Map !== 'undefined' && a instanceof Map) {
      if (a.size !== b.size) return false;
      var okm = true;
      a.forEach(function (v, k) { if (!b.has(k) || !deepEqual(v, b.get(k), seen)) okm = false; });
      return okm;
    }
    if (typeof Set !== 'undefined' && a instanceof Set) {
      if (a.size !== b.size) return false;
      var oks = true;
      a.forEach(function (v) { if (!b.has(v)) oks = false; });
      return oks;
    }
    if (Array.isArray(a)) {
      if (a.length !== b.length) return false;
      for (var j = 0; j < a.length; j++) if (!deepEqual(a[j], b[j], seen)) return false;
      return true;
    }
    if (ArrayBuffer.isView && ArrayBuffer.isView(a)) {
      if (a.length !== b.length) return false;
      for (var t = 0; t < a.length; t++) if (a[t] !== b[t]) return false;
      return true;
    }
    var ka = Object.keys(a), kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (var k = 0; k < ka.length; k++) {
      if (!Object.prototype.hasOwnProperty.call(b, ka[k])) return false;
      if (!deepEqual(a[ka[k]], b[ka[k]], seen)) return false;
    }
    return true;
  }

  function AssertionError(message) {
    var e = new Error(message);
    e.name = 'AssertionError';
    return e;
  }

  var MATCHERS = {
    toBe: function (a, b) { return [Object.is ? Object.is(a, b) : a === b, 'to be ' + fmt(b)]; },
    toEqual: function (a, b) { return [deepEqual(a, b), 'to equal ' + fmt(b)]; },
    toBeTruthy: function (a) { return [!!a, 'to be truthy']; },
    toBeFalsy: function (a) { return [!a, 'to be falsy']; },
    toBeDefined: function (a) { return [a !== undefined, 'to be defined']; },
    toBeUndefined: function (a) { return [a === undefined, 'to be undefined']; },
    toBeNull: function (a) { return [a === null, 'to be null']; },
    toBeGreaterThan: function (a, b) { return [a > b, 'to be greater than ' + fmt(b)]; },
    toBeGreaterThanOrEqual: function (a, b) { return [a >= b, 'to be greater than or equal to ' + fmt(b)]; },
    toBeLessThan: function (a, b) { return [a < b, 'to be less than ' + fmt(b)]; },
    toBeLessThanOrEqual: function (a, b) { return [a <= b, 'to be less than or equal to ' + fmt(b)]; },
    toBeCloseTo: function (a, b, digits) {
      var d = digits == null ? 2 : digits;
      return [Math.abs(a - b) < Math.pow(10, -d) / 2, 'to be close to ' + fmt(b) + ' (' + d + ' digits)'];
    },
    toContain: function (a, b) {
      var ok = false;
      if (typeof a === 'string') ok = a.indexOf(String(b)) !== -1;
      else if (a && typeof a.length === 'number') {
        for (var i = 0; i < a.length; i++) if (a[i] === b || deepEqual(a[i], b)) { ok = true; break; }
      } else if (a && typeof a.has === 'function') ok = a.has(b);
      return [ok, 'to contain ' + fmt(b)];
    },
    toHaveLength: function (a, n) { return [!!a && a.length === n, 'to have length ' + n]; },
    toMatch: function (a, re) {
      var ok = typeof a === 'string' && (re instanceof RegExp ? re.test(a) : a.indexOf(String(re)) !== -1);
      return [ok, 'to match ' + String(re)];
    },
    toThrow: function (fn, expected) {
      var threw = false, err = null;
      try { fn(); } catch (e) { threw = true; err = e; }
      var ok = threw;
      if (threw && expected !== undefined) {
        var msg = err && err.message ? err.message : String(err);
        ok = expected instanceof RegExp ? expected.test(msg) : msg.indexOf(String(expected)) !== -1;
      }
      return [ok, 'to throw' + (expected !== undefined ? ' ' + String(expected) : '')];
    }
  };

  function buildMatchers(actual, negate) {
    var m = {};
    Object.keys(MATCHERS).forEach(function (name) {
      m[name] = function () {
        var args = [actual].concat(Array.prototype.slice.call(arguments));
        var res = MATCHERS[name].apply(null, args);
        if (!!res[0] === !!negate) {
          var shown = name === 'toThrow' ? '[function]' : fmt(actual);
          throw AssertionError('expected ' + shown + (negate ? ' not ' : ' ') + res[1]);
        }
        return true;
      };
    });
    return m;
  }

  function expect(actual) {
    var m = buildMatchers(actual, false);
    m.not = buildMatchers(actual, true);
    return m;
  }

  /* ------------------------------------------------------------ timing */
  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, Math.max(0, Number(ms) || 0)); });
  }

  function waitFor(pred, timeoutMs, intervalMs) {
    var timeout = timeoutMs == null ? 5000 : timeoutMs;
    var interval = intervalMs == null ? 50 : intervalMs;
    var t0 = now();
    var lastErr = null;
    return new Promise(function (resolve, reject) {
      function tick() {
        Promise.resolve().then(function () { return pred(); }).then(function (v) {
          if (v) { resolve(v); return; }
          retry();
        }, function (e) { lastErr = e; retry(); });
      }
      function retry() {
        if (now() - t0 >= timeout) {
          reject(new Error('waitFor timed out after ' + timeout + 'ms' + (lastErr ? ' (last error: ' + (lastErr.message || lastErr) + ')' : '')));
          return;
        }
        setTimeout(tick, interval);
      }
      tick();
    });
  }

  // requestAnimationFrame-based; falls back to a 100 ms timer per frame when rAF is
  // throttled (e.g. the harness is not rendered) so tests never hang on frames().
  function frames(n) {
    var count = Math.max(1, Math.floor(Number(n) || 1));
    return new Promise(function (resolve) {
      var left = count;
      function one() {
        if (left <= 0) { resolve(count); return; }
        left -= 1;
        var done = false;
        var timer = setTimeout(function () { if (!done) { done = true; one(); } }, 100);
        if (window.requestAnimationFrame) {
          window.requestAnimationFrame(function () {
            if (!done) { done = true; clearTimeout(timer); one(); }
          });
        }
      }
      one();
    });
  }

  /* ------------------------------------------------------------ input */
  function firstCanvas() { return document.querySelector('canvas'); }

  var KEY_FOR_CODE = {
    Space: ' ', Enter: 'Enter', Escape: 'Escape', Tab: 'Tab', Backspace: 'Backspace',
    ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight', ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown',
    ShiftLeft: 'Shift', ShiftRight: 'Shift', ControlLeft: 'Control', ControlRight: 'Control',
    AltLeft: 'Alt', AltRight: 'Alt', MetaLeft: 'Meta', MetaRight: 'Meta'
  };
  var KEYCODE = {
    ' ': 32, Enter: 13, Escape: 27, Tab: 9, Backspace: 8, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
    Shift: 16, Control: 17, Alt: 18, Meta: 91
  };

  function keyInfo(input) {
    var s = String(input);
    var code = s, key = s;
    if (KEY_FOR_CODE[s]) key = KEY_FOR_CODE[s];
    else if (/^Key[A-Z]$/.test(s)) key = s.charAt(3).toLowerCase();
    else if (/^Digit[0-9]$/.test(s)) key = s.charAt(5);
    else if (s === ' ') { code = 'Space'; key = ' '; }
    else if (/^[a-zA-Z]$/.test(s)) { code = 'Key' + s.toUpperCase(); key = s; }
    else if (/^[0-9]$/.test(s)) { code = 'Digit' + s; key = s; }
    else if (s === 'Up' || s === 'Down' || s === 'Left' || s === 'Right') { code = 'Arrow' + s; key = code; }
    var kc = KEYCODE[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    return { key: key, code: code, keyCode: kc };
  }

  function makeKeyEvent(type, info) {
    var ev;
    var init = { key: info.key, code: info.code, keyCode: info.keyCode, which: info.keyCode, bubbles: true, cancelable: true, composed: true };
    try { ev = new KeyboardEvent(type, init); } catch (e) {
      ev = document.createEvent('Event');
      ev.initEvent(type, true, true);
      try { Object.defineProperty(ev, 'key', { get: function () { return info.key; } }); } catch (e2) { /* read-only */ }
      try { Object.defineProperty(ev, 'code', { get: function () { return info.code; } }); } catch (e3) { /* read-only */ }
    }
    // keyCode/which are legacy and ignored by some KeyboardEvent constructors; games still read them.
    try { if (ev.keyCode !== info.keyCode) Object.defineProperty(ev, 'keyCode', { get: function () { return info.keyCode; } }); } catch (e4) { /* ignore */ }
    try { if (ev.which !== info.keyCode) Object.defineProperty(ev, 'which', { get: function () { return info.keyCode; } }); } catch (e5) { /* ignore */ }
    return ev;
  }

  // One bubbling event dispatched at the canvas (or the focused element / body)
  // reaches listeners on the canvas, document and window exactly once each.
  function keyTarget() {
    var c = firstCanvas();
    if (c) return c;
    var a = document.activeElement;
    return (a && a !== document.body) ? a : (document.body || document.documentElement);
  }

  function key(code, holdMs) {
    var info = keyInfo(code);
    var hold = holdMs == null ? 50 : holdMs;
    var target = keyTarget();
    target.dispatchEvent(makeKeyEvent('keydown', info));
    return wait(hold).then(function () {
      target.dispatchEvent(makeKeyEvent('keyup', info));
      return true;
    });
  }

  function toClient(x, y) {
    var c = firstCanvas();
    var r = c ? c.getBoundingClientRect() : { left: 0, top: 0 };
    return { x: r.left + Number(x || 0), y: r.top + Number(y || 0) };
  }

  function targetAt(pt) {
    var el = document.elementFromPoint ? document.elementFromPoint(pt.x, pt.y) : null;
    return el || firstCanvas() || document.body || document.documentElement;
  }

  var pointerSupported = typeof window.PointerEvent === 'function';

  function pointer(type, target, pt, pointerType, id, buttons) {
    var init = {
      bubbles: true, cancelable: true, composed: true, clientX: pt.x, clientY: pt.y, screenX: pt.x, screenY: pt.y,
      pageX: pt.x + (window.pageXOffset || 0), pageY: pt.y + (window.pageYOffset || 0),
      button: 0, buttons: buttons, pointerId: id, pointerType: pointerType, isPrimary: true, width: 1, height: 1,
      pressure: buttons ? 0.5 : 0, view: window
    };
    var ev;
    if (pointerSupported) {
      try { ev = new PointerEvent(type, init); } catch (e) { ev = null; }
    }
    if (!ev) {
      try { ev = new MouseEvent(type, init); } catch (e2) {
        ev = document.createEvent('MouseEvents');
        ev.initMouseEvent(type, true, true, window, 0, pt.x, pt.y, pt.x, pt.y, false, false, false, false, 0, null);
      }
      try { Object.defineProperty(ev, 'pointerType', { get: function () { return pointerType; } }); } catch (e3) { /* ignore */ }
      try { Object.defineProperty(ev, 'pointerId', { get: function () { return id; } }); } catch (e4) { /* ignore */ }
    }
    return target.dispatchEvent(ev);
  }

  function mouse(type, target, pt, buttons) {
    var init = { bubbles: true, cancelable: true, composed: true, clientX: pt.x, clientY: pt.y, screenX: pt.x, screenY: pt.y,
                 button: 0, buttons: buttons, view: window, detail: type === 'click' ? 1 : 0 };
    var ev;
    try { ev = new MouseEvent(type, init); } catch (e) {
      ev = document.createEvent('MouseEvents');
      ev.initMouseEvent(type, true, true, window, init.detail, pt.x, pt.y, pt.x, pt.y, false, false, false, false, 0, null);
    }
    return target.dispatchEvent(ev);
  }

  function click(x, y) {
    var pt = toClient(x, y);
    var target = targetAt(pt);
    pointer('pointerdown', target, pt, 'mouse', 1, 1);
    mouse('mousedown', target, pt, 1);
    return wait(10).then(function () {
      pointer('pointerup', target, pt, 'mouse', 1, 0);
      mouse('mouseup', target, pt, 0);
      mouse('click', target, pt, 0);
      return true;
    });
  }

  var touchCtor = (function () {
    try {
      if (typeof window.Touch !== 'function' || typeof window.TouchEvent !== 'function') return false;
      var t = new Touch({ identifier: 1, target: document.documentElement, clientX: 0, clientY: 0 });
      new TouchEvent('touchstart', { touches: [t], targetTouches: [t], changedTouches: [t], bubbles: true });
      return true;
    } catch (e) {
      return false;
    }
  })();
  var touchSeq = 100;

  function touchEvent(type, target, pt, id, active) {
    if (!touchCtor) return true;
    var t = new Touch({ identifier: id, target: target, clientX: pt.x, clientY: pt.y, screenX: pt.x, screenY: pt.y,
                        pageX: pt.x + (window.pageXOffset || 0), pageY: pt.y + (window.pageYOffset || 0),
                        radiusX: 1, radiusY: 1, force: 0.5 });
    var ev = new TouchEvent(type, {
      touches: active ? [t] : [], targetTouches: active ? [t] : [], changedTouches: [t],
      bubbles: true, cancelable: true, composed: true, view: window
    });
    return target.dispatchEvent(ev);
  }

  // Mirrors a real tap/swipe: pointer events (pointerType 'touch') plus TouchEvents when
  // constructible, and compatibility mouse events + click for an unprevented tap.
  function touch(x, y, moveToX, moveToY, holdMs) {
    var hold = holdMs == null ? 50 : holdMs;
    var start = toClient(x, y);
    var hasMove = moveToX != null && moveToY != null;
    var end = hasMove ? toClient(moveToX, moveToY) : start;
    var target = targetAt(start);
    var id = ++touchSeq;
    var prevented = false;
    pointer('pointerdown', target, start, 'touch', id, 1);
    if (touchEvent('touchstart', target, start, id, true) === false) prevented = true;
    var steps = hasMove ? 5 : 0;
    var chain = Promise.resolve();
    for (var i = 1; i <= steps; i++) {
      (function (k) {
        chain = chain.then(function () { return wait(hold / (steps + 1)); }).then(function () {
          var pt = { x: start.x + (end.x - start.x) * k / steps, y: start.y + (end.y - start.y) * k / steps };
          pointer('pointermove', target, pt, 'touch', id, 1);
          if (touchEvent('touchmove', target, pt, id, true) === false) prevented = true;
        });
      })(i);
    }
    return chain.then(function () { return wait(hasMove ? hold / (steps + 1) : hold); }).then(function () {
      pointer('pointerup', target, end, 'touch', id, 0);
      if (touchEvent('touchend', target, end, id, false) === false) prevented = true;
      if (!hasMove && !prevented) {
        mouse('mousedown', target, end, 1);
        mouse('mouseup', target, end, 0);
        mouse('click', target, end, 0);
      }
      return true;
    });
  }

  /* ------------------------------------------------------------ screenshots */
  function screenshot(name) {
    var c = firstCanvas();
    if (!c) { log('screenshot ' + (name || '') + ': no <canvas> on the page'); return null; }
    try {
      var url = c.toDataURL('image/png');
      return (typeof url === 'string' && url.indexOf('data:image/') === 0) ? url : null;
    } catch (e) {
      log('screenshot ' + (name || '') + ': ' + (e && e.message ? e.message : e) + ' (tainted canvas?)');
      return null;
    }
  }

  /* ------------------------------------------------------------ runner */
  function errText(e) {
    if (!e) return 'failed';
    var msg = (e && e.message) ? e.message : String(e);
    if (e && e.name && e.name !== 'Error' && msg.indexOf(e.name) !== 0) msg = e.name + ': ' + msg;
    var stack = e && typeof e.stack === 'string' ? e.stack.split('\n') : [];
    var first = null;
    for (var i = 0; i < stack.length; i++) {
      var ln = stack[i].trim();
      if (!ln || ln.indexOf(msg) !== -1 || /^(\w*Error|AssertionError)\b/.test(ln)) continue;
      if (/kyora-test-api\.js/.test(ln)) continue;
      first = ln;
      break;
    }
    return first ? msg + ' (' + first + ')' : msg;
  }

  function runOne(entry, api, timeoutMs) {
    var t0 = now();
    if (entry.problem) {
      return Promise.resolve({ id: entry.id, name: entry.name, status: 'fail', ms: 0, error: entry.problem });
    }
    var timer = null;
    var timeout = new Promise(function (_, reject) {
      timer = setTimeout(function () { reject(new Error('test timed out after ' + timeoutMs + 'ms')); }, timeoutMs);
    });
    var body = Promise.resolve().then(function () { return entry.fn(api); });
    return Promise.race([body, timeout]).then(function () {
      clearTimeout(timer);
      return { id: entry.id, name: entry.name, status: 'pass', ms: Math.round(now() - t0), error: null };
    }, function (e) {
      clearTimeout(timer);
      return { id: entry.id, name: entry.name, status: 'fail', ms: Math.round(now() - t0), error: errText(e) };
    });
  }

  function run(opts) {
    var timeoutMs = (opts && opts.timeoutMs) || 10000;
    var onResult = opts && typeof opts.onResult === 'function' ? opts.onResult : null;
    var results = [];
    var chain = Promise.resolve();
    tests.forEach(function (entry) {
      chain = chain.then(function () {
        log('run ' + entry.id);
        return runOne(entry, api, timeoutMs).then(function (r) {
          results.push(r);
          if (onResult) { try { onResult(r); } catch (e) { /* display only */ } }
        });
      });
    });
    return chain.then(function () { return { results: results, logs: logs.slice() }; });
  }

  var api = {
    __version: 1,
    test: test,
    expect: expect,
    wait: wait,
    waitFor: waitFor,
    frames: frames,
    input: { key: key, click: click, touch: touch },
    screenshot: screenshot,
    log: log,
    __tests: tests,
    __logs: logs,
    __run: run
  };
  window.KyoraTest = api;
})();
