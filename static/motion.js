// Motion for every page, built on GSAP (with ScrollTrigger) and Lenis, all served from this site.
// - Lenis smooths mouse-wheel scrolling; phones keep their own touch scrolling.
// - Each page has one entrance: the store's name arrives word by word, the invoice prints like a ticket, a paid
//   one gets its seal with a burst of gold, the sign-in card rises, and the owner's panels rise in place.
// - Nothing here is needed to buy, pay or work: with reduced motion, a failed download or an error, the page
//   simply shows as it is.
(function () {
  'use strict';
  window.trtttMotion = {};   // tells motion-early.js the animation has started
  var root = document.documentElement;
  var gsap = window.gsap, ScrollTrigger = window.ScrollTrigger, Lenis = window.Lenis;
  var animate = root.classList.contains('motion');
  var EASE = 'power3.out';
  var HIDDEN = '.shop-head > *, .item, .shop .contact, .owner-link, .fine, .receipt, .stamp, .gate, .wrap > *';

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

  // Once per tab for each key, so a refresh or the next visit does not replay it.
  function firstTime(key) {
    try {
      if (window.sessionStorage.getItem(key)) { return false; }
      window.sessionStorage.setItem(key, '1');
    } catch (e) { /* storage blocked: animate every time */ }
    return true;
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

  // The store: the name word by word, then the top-up cards as they come into view.
  function store() {
    var head = document.querySelector('.shop-head');
    var title = head.querySelector('h1');
    var words = splitWords(title);
    var tl = gsap.timeline({ defaults: { ease: EASE } });
    var logo = head.querySelector('img.logo');
    if (logo) { tl.fromTo(logo, { autoAlpha: 0, y: 12 }, { autoAlpha: 1, y: 0, duration: 0.5, clearProps: 'transform' }); }
    tl.set(title, { autoAlpha: 1 })
      .fromTo(words, { autoAlpha: 0, y: 22 }, { autoAlpha: 1, y: 0, duration: 0.6, stagger: 0.09, clearProps: 'transform' })
      .add(function () {      // the line under the name, when the store has one
        var lines = head.querySelectorAll('p');
        if (lines.length) { gsap.fromTo(lines, { autoAlpha: 0, y: 10 }, { autoAlpha: 1, y: 0, duration: 0.5, clearProps: 'transform' }); }
      }, '-=0.35')
      .add(function () {
        // What is on screen, or above it after a back or a reload, comes in now; the rest as it scrolls in.
        var all = gsap.utils.toArray('.item, .shop .contact, .owner-link, .fine');   // two cards to a row
        var now = all.filter(function (el) { return el.getBoundingClientRect().top < window.innerHeight; });
        var later = all.filter(function (el) { return now.indexOf(el) < 0; });
        if (now.length) { show(now, { y: 24, duration: 0.55, stagger: 0.06 }); }
        if (later.length) {
          ScrollTrigger.batch(later, {
            start: 'top bottom', once: true,
            onEnter: function (batch) { show(batch, { y: 24, duration: 0.55, stagger: 0.06 }); },
            onEnterBack: function (batch) { show(batch, { y: 0, duration: 0.3 }); }
          });
        }
      }, '-=0.3');

    // Buying tears the bottom of the card off: feedback while the order is being opened. The form is
    // never held back for it.
    document.querySelectorAll('.item-buy form').forEach(function (form) {
      form.addEventListener('submit', function () {
        gsap.to(form.closest('.item-buy'), { y: 8, rotation: 1.5, duration: 0.3, ease: 'power2.out' });
      });
    });
    window.addEventListener('pageshow', function (event) {
      if (event.persisted) { gsap.set('.item-buy', { clearProps: 'transform' }); }
    });
  }

  // Gold sparks fly out from the seal when it lands.
  function sparks(paper, seal) {
    var box = paper.getBoundingClientRect(), at = seal.getBoundingClientRect();
    var x = at.left - box.left + at.width / 2, y = at.top - box.top + at.height / 2;
    for (var i = 0; i < 14; i++) {
      var spark = document.createElement('span');
      spark.className = 'spark';
      paper.appendChild(spark);
      var angle = (i / 14) * Math.PI * 2 + gsap.utils.random(-0.2, 0.2), reach = gsap.utils.random(60, 120);
      gsap.fromTo(spark, { x: x - 3, y: y - 3, scale: 1, autoAlpha: 1 },
        { x: x - 3 + Math.cos(angle) * reach, y: y - 3 + Math.sin(angle) * reach * 0.7, scale: 0, autoAlpha: 0,
          duration: gsap.utils.random(0.55, 0.85), ease: 'power2.out', onComplete: spark.remove.bind(spark) });
    }
  }

  // The invoice prints from the top like a ticket; a paid one then gets its seal.
  function receipt() {
    var paper = document.querySelector('.receipt');
    var seal = paper.querySelector('.stamp');
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
    if (seal) {
      tl.fromTo(seal, { autoAlpha: 0, scale: 1.9, rotation: -16 },
        { autoAlpha: 1, scale: 1, rotation: -6, duration: 0.42, ease: 'back.out(2.2)' }, '-=0.3')
        .add(function () { sparks(paper, seal); })
        .to(paper, { x: 2, duration: 0.05, repeat: 3, yoyo: true, ease: 'none' });
    }
  }

  // Sign in, the email code and messages: the card rises.
  function gate() {
    var card = document.querySelector('.gate');
    var tl = gsap.timeline({ onComplete: function () {
      root.classList.remove('motion');
      gsap.set(card, { clearProps: 'all' });
    } });
    tl.fromTo(card, { autoAlpha: 0, y: 24 }, { autoAlpha: 1, y: 0, duration: 0.6, ease: EASE });
  }

  // Numbers count up to their value; "1,250.00" keeps its commas and decimals. Only the number's own text
  // changes, so a unit beside it (like %) stays as it is.
  function countUp(el) {
    var node = el.firstChild;
    if (node === null || node.nodeType !== 3) { return; }
    var text = node.nodeValue.trim(), match = /^([\d,]+)(\.(\d+))?$/.exec(text);
    if (match === null) { return; }
    var end = parseFloat(text.replace(/,/g, '')), places = match[3] ? match[3].length : 0;
    if (end <= 0) { return; }
    var state = { value: 0 };
    gsap.to(state, { value: end, duration: 1.1, ease: 'power2.out',
      onUpdate: function () {
        node.nodeValue = state.value.toLocaleString('en-US', { minimumFractionDigits: places, maximumFractionDigits: places });
      },
      onComplete: function () { node.nodeValue = text; } });
  }

  // The owner's pages: the panels rise in place, quickly, so work is never kept waiting.
  function owner() {
    var panels = document.querySelectorAll('.wrap > *');
    gsap.fromTo(panels, { autoAlpha: 0, y: 14 },
      { autoAlpha: 1, y: 0, duration: 0.45, stagger: 0.06, ease: EASE, clearProps: 'transform' });
    var ledger = document.querySelector('.ledger');
    if (ledger && firstTime('trttt-ledger')) {
      ledger.querySelectorAll('.lead b, dd').forEach(countUp);
    }
  }

  try {
    if (document.querySelector('.shop')) { store(); }
    else if (document.querySelector('.receipt')) { receipt(); }
    else if (document.querySelector('.gate')) { gate(); }
    else if (document.querySelector('.wrap')) { owner(); }
    else { root.classList.remove('motion'); }
  } catch (error) {
    // Never leave the page hidden because of an animation problem.
    gsap.globalTimeline.clear();
    gsap.set(document.querySelectorAll(HIDDEN), { clearProps: 'all' });
    root.classList.remove('motion');
  }
})();
