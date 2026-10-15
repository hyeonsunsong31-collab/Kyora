/* Pure SVG renderer for the Kyora pipeline chart.
   renderPipelineChart(stages, opts) -> SVG markup string.
   stages: { intake:{status,pct,message,ms}, plan:{...}, ... }
   status: idle|running|done|failed|blocked|skipped. No DOM access; safe to unit test. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  const MAIN = [
    { id: 'intake', label: 'Intake', who: 'Kimi K3' },
    { id: 'plan', label: 'Plan', who: 'Kimi K3' },
    { id: 'design', label: 'Design', who: 'Nemotron' },
    { id: 'build', label: 'Build', who: 'Claude Code' },
    { id: 'test', label: 'Test', who: 'browser + node' },
    { id: 'gate', label: 'Gate', who: '>= 94 %' },
    { id: 'finalize', label: 'Finalize', who: 'slide + deck' }
  ];
  const LOOP = [
    { id: 'review', label: 'Review', who: 'Kimi K3' },
    { id: 'fix', label: 'Fix', who: 'Claude Code' }
  ];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function norm(st) {
    st = st || {};
    const status = st.status || 'idle';
    let pct = st.pct;
    if (pct == null) pct = status === 'done' ? 100 : 0;
    pct = Math.max(0, Math.min(100, Number(pct) || 0));
    return { status, pct, message: st.message || '', ms: st.ms };
  }

  function fmtMs(ms) {
    if (ms == null || isNaN(ms)) return '';
    if (ms < 1000) return Math.round(ms) + ' ms';
    if (ms < 60000) return (ms / 1000).toFixed(1) + ' s';
    return Math.floor(ms / 60000) + 'm ' + Math.round((ms % 60000) / 1000) + 's';
  }

  function node(x, y, w, h, def, st, compact) {
    const r = 10;
    const fillW = Math.max(0, Math.min(w, (w * st.pct) / 100));
    const pctText = st.status === 'idle' ? '' : st.status === 'skipped' ? 'skipped' : st.status === 'done' && st.ms != null ? fmtMs(st.ms) : Math.round(st.pct) + '%';
    const msg = compact ? '' : (st.message || def.who);
    return (
      '<g class="pc-node" data-stage="' + def.id + '" data-state="' + st.status + '" transform="translate(' + x + ',' + y + ')">' +
      '<title>' + esc(def.label) + ': ' + esc(st.status) + (pctText ? ' ' + esc(pctText) : '') + (st.message ? ' - ' + esc(st.message) : '') + '</title>' +
      '<clipPath id="pc-clip-' + def.id + '"><rect width="' + w + '" height="' + h + '" rx="' + r + '"/></clipPath>' +
      '<rect class="pc-node-bg" width="' + w + '" height="' + h + '" rx="' + r + '"/>' +
      '<rect class="pc-fill" clip-path="url(#pc-clip-' + def.id + ')" width="' + fillW.toFixed(1) + '" height="' + h + '"/>' +
      '<circle class="pc-pulse" cx="14" cy="15" r="4"/>' +
      '<text class="pc-label" x="' + (compact ? 24 : 24) + '" y="20">' + esc(def.label) + '</text>' +
      '<text class="pc-pct" x="' + (w - 10) + '" y="20" text-anchor="end">' + esc(pctText) + '</text>' +
      (msg ? '<text class="pc-msg" x="12" y="' + (h - 10) + '">' + esc(msg.length > 34 ? msg.slice(0, 33) + '…' : msg) + '</text>' : '') +
      '</g>'
    );
  }

  function arrowDefs() {
    return '<defs><marker id="pc-arrowhead" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto" markerUnits="userSpaceOnUse"><path class="pc-arrow" d="M0,0 L8,4 L0,8 z"/></marker></defs>';
  }

  function horizontal(stages, width) {
    const n = MAIN.length;
    const gap = 18;
    const padX = 8;
    const nodeW = Math.floor((width - padX * 2 - gap * (n - 1)) / n);
    const nodeH = 54;
    const topY = 16;
    const loopY = topY + nodeH + 52;
    const height = loopY + nodeH + 20;
    let out = '<svg class="pipe-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + ' ' + height + '" width="100%" role="img" aria-label="Pipeline stages">' + arrowDefs();
    const xs = [];
    MAIN.forEach((def, i) => {
      const x = padX + i * (nodeW + gap);
      xs.push(x);
      if (i > 0) {
        const px = xs[i - 1] + nodeW;
        out += '<path class="pc-edge" marker-end="url(#pc-arrowhead)" d="M' + px + ' ' + (topY + nodeH / 2) + ' L' + (x - 1) + ' ' + (topY + nodeH / 2) + '"/>';
      }
    });
    // loop: gate -> review -> fix -> test
    const gateI = 5, testI = 4;
    const loopW = nodeW;
    const reviewX = xs[gateI] - Math.round((nodeW + gap) / 2);
    const fixX = reviewX - (loopW + gap);
    const midY = loopY + nodeH / 2;
    out += '<path class="pc-edge loop" marker-end="url(#pc-arrowhead)" d="M' + (xs[gateI] + nodeW / 2) + ' ' + (topY + nodeH) + ' V' + (midY) + ' H' + (reviewX + loopW + 2) + '"/>';
    out += '<path class="pc-edge loop" marker-end="url(#pc-arrowhead)" d="M' + reviewX + ' ' + midY + ' H' + (fixX + loopW + 2) + '"/>';
    out += '<path class="pc-edge loop" marker-end="url(#pc-arrowhead)" d="M' + fixX + ' ' + midY + ' H' + (xs[testI] + nodeW / 2) + ' V' + (topY + nodeH + 2) + '"/>';
    out += '<text class="pc-loop-label" x="' + (xs[gateI] + nodeW / 2 + 6) + '" y="' + (topY + nodeH + 30) + '">below 94 %</text>';
    MAIN.forEach((def, i) => { out += node(xs[i], topY, nodeW, nodeH, def, norm(stages[def.id]), nodeW < 96); });
    out += node(reviewX, loopY, loopW, nodeH, LOOP[0], norm(stages.review), loopW < 96);
    out += node(fixX, loopY, loopW, nodeH, LOOP[1], norm(stages.fix), loopW < 96);
    out += '</svg>';
    return out;
  }

  function vertical(stages, width) {
    const padX = 6;
    const nodeW = width - padX * 2 - 70; // room for the loop on the right
    const nodeH = 46;
    const gap = 14;
    const order = MAIN.slice(0, 6).concat([LOOP[0], LOOP[1], MAIN[6]]);
    const height = 10 + order.length * (nodeH + gap) + 6;
    let out = '<svg class="pipe-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + ' ' + height + '" width="100%" role="img" aria-label="Pipeline stages">' + arrowDefs();
    const ys = {};
    order.forEach((def, i) => {
      const y = 10 + i * (nodeH + gap);
      ys[def.id] = y;
      const isLoop = def.id === 'review' || def.id === 'fix';
      const x = isLoop ? padX + 24 : padX;
      const w = isLoop ? nodeW - 24 : nodeW;
      if (i > 0) {
        const prev = order[i - 1];
        const prevIsLoop = prev.id === 'review' || prev.id === 'fix';
        const cx = (prevIsLoop || isLoop) ? padX + 24 + (nodeW - 24) / 2 : padX + nodeW / 2;
        out += '<path class="pc-edge' + ((isLoop || def.id === 'finalize') ? ' loop' : '') + '" marker-end="url(#pc-arrowhead)" d="M' + cx + ' ' + (y - gap) + ' V' + (y - 1) + '"/>';
      }
      out += node(x, y, w, nodeH, def, norm(stages[def.id]), true);
    });
    // loop back arrow from fix to test along the right edge
    const rx = padX + nodeW + 30;
    out += '<path class="pc-edge loop" marker-end="url(#pc-arrowhead)" d="M' + (padX + nodeW) + ' ' + (ys.fix + nodeH / 2) + ' H' + rx + ' V' + (ys.test + nodeH / 2) + ' H' + (padX + nodeW + 2) + '"/>';
    out += '<text class="pc-loop-label" x="' + (rx + 4) + '" y="' + (ys.gate + nodeH / 2 + 4) + '" transform="rotate(90 ' + (rx + 4) + ' ' + (ys.gate + nodeH / 2 + 4) + ')">fix loop</text>';
    // finalize edge from gate (skip the loop visually)
    out += '</svg>';
    return out;
  }

  function renderPipelineChart(stages, opts) {
    stages = stages || {};
    opts = opts || {};
    const width = Math.max(280, Math.floor(opts.width || 720));
    if (width < 640) return vertical(stages, width);
    return horizontal(stages, width);
  }

  /* Gauge: semicircular arc, value 0..1, threshold marker. */
  function renderGauge(passRate, threshold, opts) {
    opts = opts || {};
    const w = 150, h = 90, cx = 75, cy = 80, r = 62;
    const known = typeof passRate === 'number' && !isNaN(passRate);
    const v = known ? Math.max(0, Math.min(1, passRate)) : 0;
    const thr = typeof threshold === 'number' ? Math.max(0, Math.min(1, threshold)) : 0.94;
    const circ = Math.PI * r;
    const tone = !known ? 'unknown' : v >= thr ? 'ok' : v >= thr - 0.1 ? 'warn' : 'danger';
    const ang = Math.PI * (1 - thr);
    const tx1 = cx + (r - 9) * Math.cos(ang), ty1 = cy - (r - 9) * Math.sin(ang);
    const tx2 = cx + (r + 9) * Math.cos(ang), ty2 = cy - (r + 9) * Math.sin(ang);
    const num = known ? (v * 100).toFixed(v * 100 >= 99.95 ? 0 : 1) : '-';
    return (
      '<svg class="gauge" data-tone="' + tone + '" viewBox="0 0 ' + w + ' ' + h + '" role="img" aria-label="Pass rate ' + (known ? num + ' percent' : 'unknown') + ', threshold ' + Math.round(thr * 100) + ' percent">' +
      '<path class="g-track" d="M' + (cx - r) + ' ' + cy + ' A' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy + '" fill="none" stroke-width="12" stroke-linecap="round"/>' +
      '<path class="g-val" d="M' + (cx - r) + ' ' + cy + ' A' + r + ' ' + r + ' 0 0 1 ' + (cx + r) + ' ' + cy + '" fill="none" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + (circ * v).toFixed(1) + ' ' + circ.toFixed(1) + '"/>' +
      '<line class="g-thr" x1="' + tx1.toFixed(1) + '" y1="' + ty1.toFixed(1) + '" x2="' + tx2.toFixed(1) + '" y2="' + ty2.toFixed(1) + '"/>' +
      '<text class="g-num" x="' + cx + '" y="' + (cy - 6) + '" text-anchor="middle">' + esc(num) + (known ? '<tspan class="g-unit" dx="2">%</tspan>' : '') + '</text>' +
      '<text class="g-unit" x="' + cx + '" y="' + (cy + 8) + '" text-anchor="middle">gate ' + Math.round(thr * 100) + ' %</text>' +
      '</svg>'
    );
  }

  K.renderPipelineChart = renderPipelineChart;
  K.renderGauge = renderGauge;
  K.PIPELINE_STAGES = MAIN.concat(LOOP).map((d) => d.id);
  K.fmtMs = fmtMs;
})();
