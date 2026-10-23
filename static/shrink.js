// Category pictures: a photo straight from the phone is often a few megabytes. Before the form is sent, the
// picture is redrawn in the browser at most 512 pixels wide and saved small enough for the store (under 300 KB).
// A browser that cannot do this sends the file as it is, and the server explains if it is too big.
(function () {
  'use strict';
  var LIMIT = 290 * 1024, SIDE = 512;

  function encode(canvas, type, quality) {
    return new Promise(function (done) { canvas.toBlob(done, type, quality); });
  }

  async function shrink(file) {
    var picture = await createImageBitmap(file);
    var scale = Math.min(1, SIDE / Math.max(picture.width, picture.height));
    var canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(picture.width * scale));
    canvas.height = Math.max(1, Math.round(picture.height * scale));
    var paint = canvas.getContext('2d');
    var blob = null;
    for (var quality = 0.86; quality >= 0.4; quality -= 0.12) {
      paint.clearRect(0, 0, canvas.width, canvas.height);
      paint.drawImage(picture, 0, 0, canvas.width, canvas.height);
      blob = await encode(canvas, 'image/webp', quality);
      if (blob === null || (blob.type === 'image/webp') === false) {     // this browser cannot write WebP: JPEG on the store's dark
        paint.fillStyle = '#1c1838';
        paint.fillRect(0, 0, canvas.width, canvas.height);
        paint.drawImage(picture, 0, 0, canvas.width, canvas.height);
        blob = await encode(canvas, 'image/jpeg', quality);
      }
      if (blob && blob.size <= LIMIT) { break; }
    }
    return blob;
  }

  document.querySelectorAll('input[type=file][data-shrink]').forEach(function (input) {
    var note = document.getElementById(input.getAttribute('data-shrink'));
    input.addEventListener('change', async function () {
      var file = input.files && input.files[0];
      var able = typeof createImageBitmap === 'function' && typeof DataTransfer === 'function';
      if (file === undefined || file === null || able === false) { return; }
      if (note) { note.textContent = 'نجهّز الصورة…'; }
      try {
        var blob = await shrink(file);
        if (blob === null) { throw new Error('no picture'); }
        var ext = blob.type === 'image/webp' ? 'webp' : 'jpg';
        var list = new DataTransfer();
        list.items.add(new File([blob], 'category.' + ext, { type: blob.type }));
        input.files = list.files;
        if (note) { note.textContent = 'جاهزة: ' + Math.round(blob.size / 1024) + ' كيلوبايت (كانت ' + Math.round(file.size / 1024) + ').'; }
      } catch (error) {
        if (note) { note.textContent = 'تعذر تجهيز هذه الصورة. اختر صورة PNG أو JPG.'; }
      }
    });
  });
})();
