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

  // Choosing a saved customer fills the customer fields; "new customer" clears them.
  function fillFrom(select) {
    var option = select.options[select.selectedIndex];
    ['name', 'phone', 'email'].forEach(function (field) {
      var input = document.getElementById(field);
      if (input) { input.value = option.getAttribute('data-' + field) || ''; }
    });
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
