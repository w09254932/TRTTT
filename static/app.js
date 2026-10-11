(function () {
  'use strict';

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var field = document.getElementById('link');
    if (field) { field.select(); document.execCommand('copy'); }
    return Promise.resolve();
  }

  document.addEventListener('click', function (event) {
    var copy = event.target.closest('[data-copy]');
    if (copy) {
      var label = copy.textContent;
      copyText(copy.getAttribute('data-copy')).then(function () {
        copy.textContent = 'تم النسخ';
        setTimeout(function () { copy.textContent = label; }, 1800);
      });
    }
    var share = event.target.closest('[data-share]');
    if (share) { navigator.share({ url: share.getAttribute('data-share') }).catch(function () {}); }
  });

  if (navigator.share) {
    document.querySelectorAll('[data-share]').forEach(function (button) { button.hidden = false; });
  }

  // Phone numbers: the country picker shows the flag and the code of the chosen country,
  // and a number typed with + or 00 picks the country of its code.
  function flagOf(iso) {
    return String.fromCodePoint(0x1F1E6 + iso.charCodeAt(0) - 65, 0x1F1E6 + iso.charCodeAt(1) - 65);
  }
  function showCountry(select) {
    var option = select.options[select.selectedIndex];
    var face = select.parentNode.querySelector('.face');
    var phone = select.closest('.tel').querySelector('[data-phone]');
    if (!option) { return; }
    if (face) { face.textContent = flagOf(option.value) + ' +' + option.getAttribute('data-dial'); }
    if (phone) { phone.placeholder = option.getAttribute('data-example') || ''; }
  }
  function westernDigits(text) {
    // Arabic (U+0660) and Persian (U+06F0) digits both start on a multiple of 16.
    return text.replace(/[\u0660-\u0669\u06F0-\u06F9]/g, function (digit) {
      return String(digit.charCodeAt(0) % 16);
    });
  }
  function countryFromNumber(input) {
    var select = input.closest('.tel').querySelector('select[data-cc]');
    var found = /^(?:\+|00)(\d+)/.exec(westernDigits(input.value).replace(/[\s\-().\u200e\u200f]/g, ''));
    if (!select || !found) { return; }
    var digits = found[1], current = select.options[select.selectedIndex], best = null;
    if (current && digits.indexOf(current.getAttribute('data-dial')) === 0) { return; }
    Array.prototype.forEach.call(select.options, function (option) {
      var dial = option.getAttribute('data-dial');
      if (digits.indexOf(dial) !== 0) { return; }
      var bestDial = best ? best.getAttribute('data-dial') : '';
      if (dial.length > bestDial.length || (dial.length === bestDial.length && option.hasAttribute('data-main'))) {
        best = option;
      }
    });
    if (best) { select.value = best.value; showCountry(select); }
  }
  document.documentElement.classList.add('js');
  document.querySelectorAll('select[data-cc]').forEach(showCountry);
  document.addEventListener('input', function (event) {
    if (event.target.matches('[data-phone]')) { countryFromNumber(event.target); }
  });

  // Choosing a saved customer fills the customer fields; "new customer" clears them.
  function fillFrom(select) {
    var option = select.options[select.selectedIndex];
    ['name', 'phone', 'email'].forEach(function (field) {
      var input = document.getElementById(field);
      if (input) { input.value = option.getAttribute('data-' + field) || ''; }
    });
    var country = document.getElementById('phone-cc');
    if (country && option.getAttribute('data-country')) {
      country.value = option.getAttribute('data-country');
      showCountry(country);
    }
  }
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-fill]')) { fillFrom(event.target); }
    if (event.target.matches('select[data-cc]')) { showCountry(event.target); }
  });
  document.querySelectorAll('[data-fill]').forEach(function (select) {
    var phone = document.getElementById('phone');
    if (select.value && phone && phone.value === '') { fillFrom(select); }
  });

  // Ask before destructive actions and stop double submits.
  document.addEventListener('submit', function (event) {
    var question = event.target.getAttribute('data-confirm');
    if (question && !window.confirm(question)) { event.preventDefault(); return; }
    event.target.querySelectorAll('button[type=submit]').forEach(function (button) { button.disabled = true; });
  });
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('button[type=submit]').forEach(function (button) { button.disabled = false; });
  });
})();
