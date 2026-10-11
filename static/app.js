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

  // Phone numbers: the country code is typed in its own box; the flag shows in the box and the country's
  // name under it. A number typed with + or 00 puts its code in the box.
  var phoneCodes = null;
  function codes() {
    if (!phoneCodes) {
      var data = document.getElementById('phone-codes');
      phoneCodes = data ? JSON.parse(data.textContent) : {};
    }
    return phoneCodes;
  }
  function flagOf(iso) {
    return String.fromCodePoint(0x1F1E6 + iso.charCodeAt(0) - 65, 0x1F1E6 + iso.charCodeAt(1) - 65);
  }
  function westernDigits(text) {
    // Arabic (U+0660) and Persian (U+06F0) digits both start on a multiple of 16.
    return text.replace(/[\u0660-\u0669\u06F0-\u06F9]/g, function (digit) {
      return String(digit.charCodeAt(0) % 16);
    });
  }
  function showCode(input) {
    var digits = westernDigits(input.value).replace(/\D/g, '').replace(/^0+/, '').slice(0, 4);
    if (digits !== input.value) { input.value = digits; }
    var info = codes()[digits], row = input.closest('.tel');
    var flag = row.querySelector('.flag'), phone = row.querySelector('[data-phone]');
    var name = document.getElementById(input.id.replace(/-cc$/, '-country'));
    if (flag) { flag.textContent = info ? flagOf(info[0]) : ''; }
    if (phone) { phone.placeholder = info ? info[2] : ''; }
    if (name) {
      name.textContent = info ? info[1] : (digits ? 'رمز دولة غير معروف' : 'اكتب رمز الدولة');
      name.classList.toggle('bad', !info);
    }
  }
  function codeFromNumber(input) {
    var found = /^(?:\+|00)(\d{1,3})/.exec(westernDigits(input.value).replace(/[\s\-().\u200e\u200f]/g, ''));
    var box = input.closest('.tel').querySelector('input[data-cc]');
    if (!found || !box) { return; }
    for (var size = found[1].length; size > 0; size--) {
      var code = found[1].slice(0, size);
      if (codes()[code]) {
        if (box.value !== code) { box.value = code; showCode(box); }
        return;
      }
    }
  }
  document.querySelectorAll('input[data-cc]').forEach(showCode);
  document.addEventListener('input', function (event) {
    if (event.target.matches('input[data-cc]')) { showCode(event.target); }
    if (event.target.matches('[data-phone]')) { codeFromNumber(event.target); }
  });

  // Choosing a saved customer fills the customer fields; "new customer" clears them.
  function fillFrom(select) {
    var option = select.options[select.selectedIndex];
    ['name', 'phone', 'email'].forEach(function (field) {
      var input = document.getElementById(field);
      if (input) { input.value = option.getAttribute('data-' + field) || ''; }
    });
    var code = document.getElementById('phone-cc');
    if (code && option.getAttribute('data-code')) {
      code.value = option.getAttribute('data-code');
      showCode(code);
    }
  }
  document.addEventListener('change', function (event) {
    if (event.target.matches('[data-fill]')) { fillFrom(event.target); }
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
