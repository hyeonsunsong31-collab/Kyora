/* Kyora API client: JSON fetch wrapper + resilient event stream.
   No framework, no build step. Exposes window.Kyora.api and window.Kyora.EventStream. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  class ApiError extends Error {
    constructor(status, message, body, path) {
      super(message || ('HTTP ' + status));
      this.name = 'ApiError';
      this.status = status;
      this.body = body;
      this.path = path;
    }
    get notFound() { return this.status === 404; }
    get offline() { return this.status === 0; }
  }

  function buildQuery(query) {
    if (!query) return '';
    const parts = [];
    Object.keys(query).forEach((k) => {
      const v = query[k];
      if (v === undefined || v === null || v === '') return;
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  async function request(method, path, opts) {
    opts = opts || {};
    const url = path + buildQuery(opts.query);
    const init = { method, headers: {}, credentials: 'same-origin' };
    if (opts.json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.json);
    } else if (opts.form) {
      init.body = opts.form; // FormData: browser sets the multipart boundary
    }
    if (opts.signal) init.signal = opts.signal;
    let res;
    try {
      res = await fetch(url, init);
    } catch (err) {
      throw new ApiError(0, 'Network error: ' + (err && err.message ? err.message : 'fetch failed'), null, path);
    }
    const ctype = res.headers.get('content-type') || '';
    let body = null;
    if (ctype.indexOf('application/json') !== -1) {
      try { body = await res.json(); } catch (_) { body = null; }
    } else if (opts.raw) {
      body = await res.blob();
    } else {
      try { body = await res.text(); } catch (_) { body = null; }
    }
    if (!res.ok) {
      const msg = (body && typeof body === 'object' && body.error) ? body.error
        : (typeof body === 'string' && body.length < 300 && body) || (res.status + ' ' + res.statusText);
      throw new ApiError(res.status, msg, body, path);
    }
    return body;
  }

  const api = {
    ApiError,
    request,
    get: (path, query, opts) => request('GET', path, Object.assign({ query }, opts || {})),
    post: (path, json, opts) => request('POST', path, Object.assign({ json }, opts || {})),
    put: (path, json, opts) => request('PUT', path, Object.assign({ json }, opts || {})),
    del: (path, opts) => request('DELETE', path, opts || {}),
    postForm: (path, form, opts) => request('POST', path, Object.assign({ form }, opts || {})),
    buildQuery,
    /* Resolve a request that may legitimately 404 while a backend module is missing.
       Returns {ok:true,data} or {ok:false,error,missing:boolean}. */
    async maybe(promise) {
      try {
        return { ok: true, data: await promise };
      } catch (err) {
        if (err instanceof ApiError) return { ok: false, error: err, missing: err.status === 404 || err.status === 405 || err.status === 501 };
        return { ok: false, error: err, missing: false };
      }
    }
  };

  /* ---------------------------------------------------------------- EventStream
     Connects to /api/events (SSE) with ?since=<last id> and optional project/run
     filters. Reconnects with backoff using the last seen id. Falls back to polling
     /api/events.json when EventSource is unavailable or keeps failing. */
  class EventStream {
    constructor(opts) {
      opts = opts || {};
      this.filters = { project: opts.project || '', run: opts.run || '', types: opts.types || '' };
      this.lastId = opts.since || 0;
      this.onEvent = opts.onEvent || function () {};
      this.onState = opts.onState || function () {};
      this.state = 'idle';
      this.es = null;
      this.pollTimer = null;
      this.reconnectTimer = null;
      this.backoff = 1000;
      this.failures = 0;
      this.mode = (typeof window.EventSource === 'undefined') ? 'poll' : 'sse';
      this.pollInterval = opts.pollInterval || 2500;
      this.closed = false;
      this._seen = new Set(); // small LRU of ids to dedupe replay/live overlap
      this._seenOrder = [];
    }

    setState(s, detail) {
      if (this.state !== s) {
        this.state = s;
        this.onState(s, detail || '');
      }
    }

    url(base) {
      const q = { since: this.lastId || 0 };
      if (this.filters.project) q.project = this.filters.project;
      if (this.filters.run) q.run = this.filters.run;
      if (this.filters.types) q.types = this.filters.types;
      return base + buildQuery(q);
    }

    start() {
      this.closed = false;
      if (this.mode === 'sse') this.openSSE(); else this.startPolling();
      return this;
    }

    restart(filters) {
      if (filters) Object.assign(this.filters, filters);
      this.stopTransport();
      this.start();
    }

    close() {
      this.closed = true;
      this.stopTransport();
      this.setState('closed');
    }

    stopTransport() {
      if (this.es) { try { this.es.close(); } catch (_) {} this.es = null; }
      if (this.pollTimer) { clearTimeout(this.pollTimer); this.pollTimer = null; }
      if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
    }

    accept(ev) {
      if (!ev || typeof ev !== 'object') return;
      const id = Number(ev.id || 0);
      if (id > 0) {
        if (id <= this.lastId && this._seen.has(id)) return;
        if (!this._seen.has(id)) {
          this._seen.add(id);
          this._seenOrder.push(id);
          if (this._seenOrder.length > 4000) {
            const drop = this._seenOrder.splice(0, 2000);
            drop.forEach((d) => this._seen.delete(d));
          }
        }
        if (id > this.lastId) this.lastId = id;
      }
      try { this.onEvent(ev); } catch (err) { console.error('event handler failed', ev.type, err); }
    }

    openSSE() {
      if (this.closed) return;
      const self = this;
      this.setState(this.failures ? 'reconnecting' : 'connecting');
      let es;
      try {
        es = new EventSource(this.url('/api/events'));
      } catch (err) {
        this.mode = 'poll';
        this.startPolling();
        return;
      }
      this.es = es;
      let gotOpen = false;
      es.onopen = function () {
        gotOpen = true;
        self.failures = 0;
        self.backoff = 1000;
        self.setState('live');
      };
      es.onmessage = function (m) {
        let ev = null;
        try { ev = JSON.parse(m.data); } catch (_) { return; }
        self.accept(ev);
      };
      // Named events (server sets event: <type>) are not delivered to onmessage;
      // register a catch-all for the documented types via a generic listener.
      EVENT_TYPES.forEach(function (t) {
        es.addEventListener(t, function (m) {
          let ev = null;
          try { ev = JSON.parse(m.data); } catch (_) { return; }
          self.accept(ev);
        });
      });
      es.onerror = function () {
        if (self.closed) return;
        try { es.close(); } catch (_) {}
        if (self.es === es) self.es = null;
        self.failures += 1;
        if (!gotOpen && self.failures >= 3) {
          // Browser cannot hold an EventSource here (proxies, Safari background): poll instead.
          self.mode = 'poll';
          self.startPolling();
          // retry SSE later
          self.reconnectTimer = setTimeout(function () { if (!self.closed) { self.mode = 'sse'; self.stopTransport(); self.openSSE(); } }, 60000);
          return;
        }
        self.setState('reconnecting');
        self.reconnectTimer = setTimeout(function () { self.openSSE(); }, self.backoff);
        self.backoff = Math.min(self.backoff * 2, 15000);
      };
    }

    startPolling() {
      if (this.closed) return;
      const self = this;
      this.setState('polling');
      const tick = async function () {
        if (self.closed || self.mode !== 'poll') return;
        try {
          const res = await api.get('/api/events.json', {
            since: self.lastId || 0,
            project: self.filters.project || undefined,
            run: self.filters.run || undefined,
            types: self.filters.types || undefined,
            limit: 500
          });
          (res && res.events || []).forEach((ev) => self.accept(ev));
          if (res && res.last_id && !self.lastId) self.lastId = res.last_id;
          self.setState('polling');
        } catch (err) {
          self.setState('offline', err && err.message);
        }
        self.pollTimer = setTimeout(tick, self.pollInterval);
      };
      tick();
    }
  }

  const EVENT_TYPES = [
    'run.created', 'run.state', 'run.blocked', 'run.completed', 'run.failed', 'run.cancelled', 'run.awaiting_approval',
    'stage.started', 'stage.progress', 'stage.finished',
    'model.request', 'model.delta', 'model.metrics', 'model.done', 'model.error', 'model.fallback',
    'engine.session', 'engine.model_resolved', 'engine.text', 'engine.thinking', 'engine.tool_use', 'engine.tool_result',
    'engine.result', 'engine.error', 'engine.compat_retry', 'engine.stderr',
    'judge.request', 'judge.verdict', 'judge.unavailable',
    'test.static', 'test.node', 'test.await_browser', 'test.browser_result', 'gate.result', 'fix.iteration', 'review.decision',
    'slide.created', 'deck.updated', 'tokkio.narration', 'tokkio.view', 'tokkio.clear', 'selfcheck.update',
    'bench.progress', 'bench.done', 'settings.changed', 'hub.job', 'hub.status'
  ];

  K.api = api;
  K.EventStream = EventStream;
  K.EVENT_TYPES = EVENT_TYPES;
})();
