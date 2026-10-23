// Runs before the store is drawn. When this page was opened by tapping a category (static/looks.js left a note),
// marks the page so it starts covered: the curtain closed, or the picture filling the screen. If static/looks.js
// has not started within 2.5 seconds, the mark goes so the page is never left covered.
(function () {
  'use strict';
  var root = document.documentElement, go = null;
  if (typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches) { return; }
  try {
    go = JSON.parse(window.sessionStorage.getItem('trttt-look-go') || 'null');
    window.sessionStorage.removeItem('trttt-look-go');
  } catch (e) { go = null; }
  if (go === null || typeof go !== 'object' || ['curtain', 'zoom', 'expand'].indexOf(go.look) < 0) { return; }
  var cat = /[?&]cat=([^&#]+)/.exec(window.location.search);
  if (cat === null || decodeURIComponent(cat[1]) !== go.cat || Date.now() - (go.at || 0) > 8000) { return; }
  root.classList.add('arrive-' + go.look);
  window.trtttArrive = go;
  window.setTimeout(function () {
    if (window.trtttLooks === undefined) { root.classList.remove('arrive-' + go.look); }
  }, 2500);
})();
