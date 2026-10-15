/* Intentionally broken fixture: syntax error below. */
(function () {
  'use strict';
  window.Game = {
    start: function () { return 1; },
    getState: function () { return { score: 0, lives: 3, phase: 'menu' } ;
  };
})(;
