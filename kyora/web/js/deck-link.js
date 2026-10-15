/* Deck link: keeps the Slides view's "Open deck" button pointing at /projects/<pid>/deck
   (the per-project HTML presentation, print-to-PDF) and disables it without a project. */
(function () {
  'use strict';
  const K = (window.Kyora = window.Kyora || {});

  function update(s) {
    const a = document.getElementById('btn-deck');
    if (!a) return;
    const pid = s && s.projectId;
    if (pid && !s.projectsMissing) {
      a.setAttribute('href', '/projects/' + encodeURIComponent(pid) + '/deck');
      a.setAttribute('target', '_blank');
      a.removeAttribute('aria-disabled');
      a.removeAttribute('tabindex');
      a.setAttribute('title', 'Open the presentation deck of this project (print it to PDF from the browser)');
    } else {
      a.setAttribute('href', '#/slides');
      a.removeAttribute('target');
      a.setAttribute('aria-disabled', 'true');
      a.setAttribute('tabindex', '-1');
      a.setAttribute('title', s && s.projectsMissing ? 'Decks need the projects API, which is not available on this server' : 'Select a project first');
    }
  }

  function init() {
    const a = document.getElementById('btn-deck');
    if (a) a.addEventListener('click', (e) => {
      if (a.getAttribute('aria-disabled') === 'true') {
        e.preventDefault();
        if (K.toast) K.toast(a.getAttribute('title') || 'Select a project first.', 'warn');
      }
    });
    K.store.subscribe((s, keys) => {
      if (keys.indexOf('projectId') >= 0 || keys.indexOf('projectsMissing') >= 0) update(s);
    });
    update(K.store.state);
  }

  (K.modules = K.modules || {}).deckLink = { init, update };
})();
