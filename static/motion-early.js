// Runs before the page is drawn. Marks the page for its entrance animation (static/motion.js), unless the
// visitor's device asks for less motion. If the animation has not started within 2.5 seconds (a slow or
// blocked download), the mark is removed again so nothing on the page stays hidden.
(function () {
  'use strict';
  var root = document.documentElement;
  if (typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches) { return; }
  root.classList.add('motion');
  window.setTimeout(function () {
    if (window.trtttMotion === undefined) { root.classList.remove('motion'); }
  }, 2500);
})();
