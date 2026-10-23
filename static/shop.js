// The store: the eye on a card opens the product's full details in a window over the page. The close button, a
// tap on the dim area around it or the Esc key closes it. A browser without windows of this kind hides the eye:
// the pay page shows the same details.
(function () {
  'use strict';
  function lenis() { return window.trtttMotion && window.trtttMotion.lenis; }

  // Search as you type: cards whose name or description does not hold the words are hidden. Pressing enter
  // still searches on the server (and works without script).
  var find = document.querySelector('[data-find-input]');
  var none = document.getElementById('no-match');
  if (find) {
    var cards = Array.prototype.slice.call(document.querySelectorAll('.item[data-find]'));
    var apply = function () {
      var words = find.value.toLowerCase().split(/\s+/).filter(Boolean), left = 0;
      cards.forEach(function (card) {
        var text = card.getAttribute('data-find');
        var hit = words.every(function (w) { return text.indexOf(w) !== -1; });
        card.hidden = hit === false;
        if (hit) { left += 1; }
      });
      if (none) { none.hidden = left > 0 || cards.length === 0; }
    };
    find.addEventListener('input', apply);
    find.form.addEventListener('submit', function (event) {
      if (cards.length > 0) { event.preventDefault(); apply(); }
    });
  }

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
