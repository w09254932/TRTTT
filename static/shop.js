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
  var pick = document.getElementById('pick-cat');
  if (find) {
    var cards = Array.prototype.slice.call(document.querySelectorAll('.item[data-find]'));
    var pickFirst = pick ? pick.hidden : true;
    var apply = function () {
      var words = find.value.toLowerCase().split(/\s+/).filter(Boolean), left = 0;
      cards.forEach(function (card) {
        var text = card.getAttribute('data-find');
        // with nothing typed, products inside a category wait for their category again
        var hit = words.length ? words.every(function (w) { return text.indexOf(w) !== -1; }) : card.hasAttribute('data-shelved') === false;
        card.hidden = hit === false;
        if (hit) { left += 1; }
      });
      if (pick) { pick.hidden = words.length ? true : pickFirst; }
      if (none) { none.hidden = left > 0 || cards.length === 0 || words.length === 0; }
    };
    find.addEventListener('input', apply);
    find.form.addEventListener('submit', function (event) {
      if (cards.length > 0) { event.preventDefault(); apply(); }
    });
  }

  // The door (static/category.css) plays only from the front page into a category, where it opens, and from a
  // category back to the front page, where it closes. Anything else (a search, the same page) goes without it.
  window.addEventListener('pageswap', function (event) {
    if (event.viewTransition === null || event.viewTransition === undefined) { return; }
    var entry = event.activation && event.activation.entry;
    var to = null;
    try { to = entry && entry.url ? new URL(entry.url) : null; } catch (e) { to = null; }
    var here = location.pathname;
    var into = to !== null && to.pathname.indexOf('/c/') === 0 && here === '/';
    var back = to !== null && to.pathname === '/' && here.indexOf('/c/') === 0;
    if (to === null || to.origin !== location.origin || (into || back) === false) {
      event.viewTransition.skipTransition();
    }
  });

  // Each card takes the colour of its picture: the picture drawn small, its colours averaged. A card whose picture
  // cannot be read keeps the store's own colour.
  function tint(img) {
    try {
      var canvas = document.createElement('canvas');
      canvas.width = 8; canvas.height = 8;
      var pen = canvas.getContext('2d');
      pen.drawImage(img, 0, 0, 8, 8);
      var dots = pen.getImageData(0, 0, 8, 8).data, r = 0, g = 0, b = 0, count = 0;
      for (var i = 0; i < dots.length; i += 4) {
        if (dots[i + 3] > 127) { r += dots[i]; g += dots[i + 1]; b += dots[i + 2]; count += 1; }
      }
      if (count > 0) {
        img.closest('.item').style.setProperty('--tc', 'rgb(' + Math.round(r / count) + ',' + Math.round(g / count) + ',' + Math.round(b / count) + ')');
      }
    } catch (e) { /* the store's colour stays */ }
  }
  document.querySelectorAll('img[data-tint]').forEach(function (img) {
    if (img.complete && img.naturalWidth > 0) { tint(img); } else { img.addEventListener('load', function () { tint(img); }); }
  });

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
