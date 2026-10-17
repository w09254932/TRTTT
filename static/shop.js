// The store: the eye on a card opens the product's full details in a window over the page. The close button, a
// tap on the dim area around it or the Esc key closes it. A browser without windows of this kind hides the eye:
// the pay page shows the same details.
(function () {
  'use strict';
  function lenis() { return window.trtttMotion && window.trtttMotion.lenis; }

  document.querySelectorAll('[data-peek]').forEach(function (button) {
    var box = document.getElementById(button.getAttribute('data-peek'));
    if (box === null || typeof box.showModal !== 'function') { button.hidden = true; return; }
    button.addEventListener('click', function () {
      box.showModal();
      if (lenis()) { lenis().stop(); }      // the page behind stays where it is
    });
    box.addEventListener('click', function (event) {
      if (event.target === box) { box.close(); }
    });
    box.addEventListener('close', function () {
      if (lenis()) { lenis().start(); }
    });
  });
})();
