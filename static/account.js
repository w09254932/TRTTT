// The account's photo: a picture from the phone is made small (a 256 px square, JPEG) in the browser before it is
// sent, so a large camera photo is accepted. Choosing a picture sends it at once; the button stays for browsers
// without scripts.
(function () {
  'use strict';
  var form = document.querySelector('form[data-photo]');
  if (form === null) { return; }
  var input = form.querySelector('input[type=file]');
  var button = form.querySelector('[data-photo-send]');
  if (button) { button.hidden = true; }

  function load(file) {
    if (window.createImageBitmap) { return createImageBitmap(file); }
    return new Promise(function (done, fail) {
      var reader = new FileReader();
      reader.onload = function () { var img = new Image(); img.onload = function () { done(img); }; img.onerror = fail; img.src = reader.result; };
      reader.onerror = fail;
      reader.readAsDataURL(file);
    });
  }

  input.addEventListener('change', function () {
    var file = input.files && input.files[0];
    if (file === undefined || file === null) { return; }
    load(file).then(function (img) {
      var side = Math.min(img.width, img.height), size = Math.min(256, side);
      var canvas = document.createElement('canvas');
      canvas.width = canvas.height = size;
      canvas.getContext('2d').drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, size, size);
      canvas.toBlob(function (blob) {
        if (blob === null || typeof window.DataTransfer === 'undefined') { form.submit(); return; }
        var box = new DataTransfer();
        box.items.add(new File([blob], 'photo.jpg', { type: 'image/jpeg' }));
        input.files = box.files;
        form.submit();
      }, 'image/jpeg', 0.86);
    }).catch(function () { form.submit(); });
  });
})();
