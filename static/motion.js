// Motion for the customer pages, built on GSAP (with ScrollTrigger) and Lenis, all served from this site.
// - Lenis smooths mouse-wheel scrolling; phones keep their own touch scrolling.
// - Each page has one entrance: the store's name and vouchers arrive, the invoice prints like a receipt,
//   and a paid invoice gets its stamp.
// - Nothing here is needed to buy or pay: with reduced motion, a failed download or an error, the page
//   simply shows as it is.
(function () {
  'use strict';
  window.trtttMotion = {};   // tells motion-early.js the animation has started
  var root = document.documentElement;
  var gsap = window.gsap, ScrollTrigger = window.ScrollTrigger, Lenis = window.Lenis;
  var animate = root.classList.contains('motion');
  var EASE = 'power3.out';
  var HIDDEN = '.shop-head > *, .item, .shop .contact, .owner-link, .fine, .receipt, .stamp';

  if (gsap === undefined || ScrollTrigger === undefined) { root.classList.remove('motion'); return; }
  gsap.registerPlugin(ScrollTrigger);

  if (Lenis) {
    var lenis = new Lenis({ autoRaf: false, anchors: true });
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add(function (time) { lenis.raf(time * 1000); });
    gsap.ticker.lagSmoothing(0);
    window.trtttMotion.lenis = lenis;
  }
  if (animate === false) { return; }

  function show(targets, vars) {
    return gsap.fromTo(targets, { autoAlpha: 0, y: vars.y || 16 },
      { autoAlpha: 1, y: 0, duration: vars.duration || 0.55, stagger: vars.stagger || 0, ease: EASE,
        delay: vars.delay || 0, clearProps: 'transform' });
  }

  // Wraps each word in its own span so the words can arrive one by one. Words, never letters:
  // Arabic letters must stay joined inside their word.
  function splitWords(el) {
    var words = el.textContent.trim().split(/\s+/);
    el.textContent = '';
    words.forEach(function (word, i) {
      if (i) { el.appendChild(document.createTextNode(' ')); }
      var span = document.createElement('span');
      span.className = 'word';
      span.textContent = word;
      el.appendChild(span);
    });
    return el.querySelectorAll('.word');
  }

  // The store: the name arrives word by word, then the vouchers as they come into view.
  function store() {
    var head = document.querySelector('.shop-head');
    var title = head.querySelector('h1');
    var words = splitWords(title);
    var tl = gsap.timeline({ defaults: { ease: EASE } });
    var logo = head.querySelector('.logo');
    if (logo) { tl.fromTo(logo, { autoAlpha: 0, y: 12 }, { autoAlpha: 1, y: 0, duration: 0.5, clearProps: 'transform' }); }
    tl.set(title, { autoAlpha: 1 })
      .fromTo(words, { autoAlpha: 0, y: 22 }, { autoAlpha: 1, y: 0, duration: 0.6, stagger: 0.09, clearProps: 'transform' })
      .fromTo(head.querySelectorAll('p'), { autoAlpha: 0, y: 10 }, { autoAlpha: 1, y: 0, duration: 0.5, clearProps: 'transform' }, '-=0.35')
      .add(function () {
        // What is on screen, or above it after a back or a reload, comes in now; the rest as it scrolls in.
        var all = gsap.utils.toArray('.item, .shop .contact, .owner-link, .fine');
        var now = all.filter(function (el) { return el.getBoundingClientRect().top < window.innerHeight; });
        var later = all.filter(function (el) { return now.indexOf(el) < 0; });
        if (now.length) { show(now, { y: 28, duration: 0.6, stagger: 0.09 }); }
        if (later.length) {
          ScrollTrigger.batch(later, {
            start: 'top bottom', once: true,
            onEnter: function (batch) { show(batch, { y: 28, duration: 0.6, stagger: 0.09 }); },
            onEnterBack: function (batch) { show(batch, { y: 0, duration: 0.3 }); }
          });
        }
      }, '-=0.3');

    // Buying tears the stub off the voucher: feedback while the order is being opened. The form is
    // never held back for it.
    var wide = window.matchMedia('(min-width: 520px)');
    document.querySelectorAll('.item-buy form').forEach(function (form) {
      form.addEventListener('submit', function () {
        var stub = form.closest('.item-buy');
        gsap.to(stub, wide.matches ? { x: -10, rotation: -2.5, duration: 0.3, ease: 'power2.out' }
                                   : { y: 8, rotation: 1.5, duration: 0.3, ease: 'power2.out' });
      });
    });
    window.addEventListener('pageshow', function (event) {
      if (event.persisted) { gsap.set('.item-buy', { clearProps: 'transform' }); }
    });
  }

  // Shown once per invoice and state in this tab, so a refresh or a corrected form does not replay it.
  function firstTime(key) {
    try {
      if (window.sessionStorage.getItem(key)) { return false; }
      window.sessionStorage.setItem(key, '1');
    } catch (e) { /* storage blocked: animate every time */ }
    return true;
  }

  // The invoice prints from the top like a receipt; a paid one then gets its stamp.
  function receipt() {
    var paper = document.querySelector('.receipt');
    var stamp = paper.querySelector('.stamp');
    var key = 'trttt-printed-' + (paper.getAttribute('data-receipt') || location.pathname);
    var waiting = paper.querySelector('.pulse');
    if (waiting || firstTime(key) === false) { root.classList.remove('motion'); return; }
    // When it is over, the page goes back to its plain styles: nothing is left hidden or clipped.
    var tl = gsap.timeline({ onComplete: function () {
      root.classList.remove('motion');
      gsap.set(document.querySelectorAll(HIDDEN), { clearProps: 'all' });
    } });
    var tall = paper.offsetHeight + 40;   // px all the way, so the edge moves evenly from top to bottom
    tl.fromTo(paper, { clipPath: 'inset(-40px -40px ' + tall + 'px -40px)' },
      { clipPath: 'inset(-40px -40px -40px -40px)', duration: 0.8, ease: 'power2.out' })
      .fromTo('.fine', { autoAlpha: 0 }, { autoAlpha: 1, duration: 0.4 }, '-=0.2');
    if (stamp) {
      tl.fromTo(stamp, { autoAlpha: 0, scale: 1.9, rotation: -14 },
        { autoAlpha: 1, scale: 1, rotation: -5, duration: 0.42, ease: 'back.out(2.2)' }, '-=0.3')
        .to(paper, { x: 2, duration: 0.05, repeat: 3, yoyo: true, ease: 'none' });
    }
  }

  try {
    if (document.querySelector('.shop')) { store(); }
    else if (document.querySelector('.receipt')) { receipt(); }
    else { root.classList.remove('motion'); }
  } catch (error) {
    // Never leave the page hidden because of an animation problem.
    gsap.globalTimeline.clear();
    gsap.set(document.querySelectorAll(HIDDEN), { clearProps: 'all' });
    root.classList.remove('motion');
  }
})();
