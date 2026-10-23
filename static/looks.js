// The look the store uses for a tapped category (main.shop data-look, chosen by the owner in looks.py):
// - curtain: two curtains close over the store, the category opens behind them, and they part again.
// - zoom: the category's picture grows from its tile to fill the screen, then settles as the cover on top.
// - expand: the tile stretches across the page, and the category opens as a wide card with its products inside.
// The page still changes by following the tile's link, so it all works without script, with reduced motion, or
// if GSAP fails to load: then the category simply opens.
(function () {
  'use strict';
  window.trtttLooks = {};
  var root = document.documentElement;
  var shop = document.querySelector('main.shop');
  var gsap = window.gsap;
  var arrive = window.trtttArrive || null;
  var KINDS = ['curtain', 'zoom', 'expand'];

  function unmark() { KINDS.forEach(function (k) { root.classList.remove('arrive-' + k); }); }
  if (shop === null || gsap === undefined || typeof window.matchMedia !== 'function' ||
      window.matchMedia('(prefers-reduced-motion: reduce)').matches) { unmark(); return; }
  var look = shop.getAttribute('data-look');

  function rectOf(el) {
    var r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, width: r.width, height: r.height };
  }

  function fixedClone(el, extra, at) {
    var copy = el.cloneNode(true);
    copy.removeAttribute('id');
    copy.removeAttribute('href');
    copy.setAttribute('aria-hidden', 'true');
    copy.classList.add(extra);
    gsap.set(copy, { top: at.top, left: at.left, width: at.width, height: at.height });
    document.body.appendChild(copy);
    return copy;
  }

  function curtain(name) {
    var box = document.createElement('div');
    box.className = 'move-curtain';
    box.setAttribute('aria-hidden', 'true');
    box.appendChild(document.createElement('i')).className = 'c-r';
    box.appendChild(document.createElement('i')).className = 'c-l';
    box.appendChild(document.createElement('b')).textContent = name || '';
    document.body.appendChild(box);
    return box;
  }

  // Leaves for the category, with a note for looks-early.js to start the next page covered.
  function leave(href, cat) {
    try { window.sessionStorage.setItem('trttt-look-go', JSON.stringify({ look: look, cat: cat, at: Date.now() })); } catch (e) { /* no note: it opens plainly */ }
    window.location.href = href;
  }

  // ---- leaving: the tile that was tapped
  var busy = false;
  document.querySelectorAll('a[data-cat-go]').forEach(function (tile) {
    tile.addEventListener('click', function (event) {
      if (KINDS.indexOf(look) < 0 || busy || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
      event.preventDefault();
      busy = true;
      var href = tile.href, cat = tile.getAttribute('data-cat-go');
      var go = function () { leave(href, cat); };
      var safety = window.setTimeout(go, 1500);   // whatever happens, the category opens
      var done = function () { window.clearTimeout(safety); go(); };
      if (look === 'curtain') {
        var box = curtain(tile.getAttribute('data-cat-name'));
        gsap.timeline({ onComplete: done })
          .fromTo(box.querySelector('.c-r'), { xPercent: 100 }, { xPercent: 0, duration: 0.5, ease: 'power3.in' }, 0)
          .fromTo(box.querySelector('.c-l'), { xPercent: -100 }, { xPercent: 0, duration: 0.5, ease: 'power3.in' }, 0)
          .fromTo(box.querySelector('b'), { autoAlpha: 0, scale: 0.9 }, { autoAlpha: 1, scale: 1, duration: 0.25 }, 0.35);
      } else if (look === 'zoom') {
        var pic = tile.querySelector('.cat-pic');
        var big = fixedClone(pic, 'move-zoom', rectOf(pic));
        gsap.set(big, { borderRadius: 12 });
        gsap.to(big, { top: 0, left: 0, width: window.innerWidth, height: window.innerHeight, borderRadius: 0,
          duration: 0.55, ease: 'power3.inOut', onComplete: done });
      } else {
        var from = rectOf(tile), page = rectOf(shop);
        var wide = fixedClone(tile, 'move-grow', from);
        gsap.to(wide, { left: page.left, width: page.width, top: Math.max(8, from.top - 10),
          height: Math.min(240, window.innerHeight * 0.4), borderRadius: 20, duration: 0.5, ease: 'power3.inOut', onComplete: done });
        gsap.to(wide.querySelector('.cat-pic'), { scale: 1.12, duration: 0.5, ease: 'power3.inOut' });
      }
    });
  });

  // Back with the browser's back button: the page comes back from its cache as it was left, so tidy it.
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      busy = false;
      document.querySelectorAll('.move-curtain, .move-zoom, .move-grow').forEach(function (el) { el.remove(); });
    }
  });

  // ---- arriving: only when this page was opened by tapping its category
  if (arrive === null || arrive.look !== look || shop.getAttribute('data-cat') !== arrive.cat) { unmark(); return; }
  try {
    if (look === 'curtain') {
      var head = document.querySelector('#cat-name');
      var box = curtain(head ? head.textContent : '');
      gsap.set(box.querySelector('b'), { autoAlpha: 1 });
      unmark();
      gsap.timeline({ delay: 0.15, onComplete: function () { box.remove(); } })
        .to(box.querySelector('b'), { autoAlpha: 0, duration: 0.2 }, 0)
        .to(box.querySelector('.c-r'), { xPercent: 100, duration: 0.85, ease: 'power3.inOut' }, 0.1)
        .to(box.querySelector('.c-l'), { xPercent: -100, duration: 0.85, ease: 'power3.inOut' }, 0.1);
    } else if (look === 'zoom') {
      var cover = document.querySelector('.cat-head.is-zoom .cat-head-pic');
      var text = document.querySelector('.cat-head.is-zoom .cat-head-text');
      cover.classList.add('zoom-wait');
      var full = fixedClone(cover, 'move-zoom', { top: 0, left: 0, width: window.innerWidth, height: window.innerHeight });
      full.classList.remove('zoom-wait');
      gsap.set(full, { borderRadius: 0 });
      unmark();
      var to = rectOf(cover);
      gsap.timeline({ delay: 0.1, onComplete: function () { cover.classList.remove('zoom-wait'); full.remove(); } })
        .to(full, { top: to.top, left: to.left, width: to.width, height: to.height, borderRadius: 20, duration: 0.75, ease: 'power3.inOut' })
        .fromTo(text, { autoAlpha: 0, y: 14 }, { autoAlpha: 1, y: 0, duration: 0.4, clearProps: 'transform' }, '-=0.15');
    } else {
      var open = document.querySelector('.cat-open');
      unmark();
      gsap.fromTo(open, { clipPath: 'inset(0% 0% 72% 68% round 20px)', autoAlpha: 1 },
        { clipPath: 'inset(0% 0% 0% 0% round 20px)', duration: 0.75, ease: 'power3.inOut', clearProps: 'clipPath' });
    }
  } catch (error) {
    unmark();
    document.querySelectorAll('.move-curtain, .move-zoom, .zoom-wait').forEach(function (el) {
      if (el.classList.contains('zoom-wait')) { el.classList.remove('zoom-wait'); } else { el.remove(); }
    });
  }
})();
