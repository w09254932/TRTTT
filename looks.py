"""How the store shows a category once a customer taps it (the owner picks one of a few looks and can try each
on the store before keeping it), and a picture for each product, used by the «big pictures» look. Pictures are
kept in Firestore apart from the products, so listing the store never loads them, and are served from this site
(the content security policy stays strict), like the category pictures."""
import hashlib
import time

from flask import Blueprint, Response, abort, current_app, flash, redirect, request, url_for

from brand import image_type
from security import signed_in, too_many

LOOKS = {
    "plain": ("العادي", "منتجات التصنيف تظهر تحت صف التصنيفات."),
    "expand": ("البطاقة تتمدد عرضياً", "بطاقة التصنيف تتمدد لعرض الشاشة ومنتجاته داخلها، وبقية التصنيفات تحتها."),
    "curtain": ("ستارة تنفتح", "ستارة تنغلق عند الضغط ثم تنفتح على منتجات التصنيف."),
    "zoom": ("تكبير من الصورة", "صورة التصنيف تكبر حتى تملأ الشاشة، ثم تستقر غلافاً أعلى منتجاته."),
    "pictures": ("شبكة بصور كبيرة", "كل منتج ببطاقة عليها صورة كبيرة: صورة المنتج، أو صورة تصنيفه إن لم تكن له صورة."),
}
DEFAULT = "plain"
MAX_IMAGE = 300 * 1024
MAX_UPLOAD = 12 * 1024 * 1024
NO_IMAGE = "صورة المنتج يجب أن تكون PNG أو JPG أو WebP بحجم أقل من 300 كيلوبايت."


class Looks:
    """The look the store uses, kept beside the brand settings and cached briefly per worker."""

    def __init__(self, client, ttl=30):
        self.ref, self.ttl = client.collection("meta").document("store_look"), ttl
        self._look, self._at = None, 0.0

    def get(self, fresh=False):
        if fresh or self._look is None or time.time() - self._at > self.ttl:
            look = (self.ref.get().to_dict() or {}).get("look")
            self._look, self._at = (look if look in LOOKS else DEFAULT), time.time()
        return self._look

    def save(self, look):
        self.ref.set({"look": look}, merge=True)
        return self.get(fresh=True)


class Pictures:
    """One picture per product, in its own collection; the product keeps only the picture's version."""

    def __init__(self, client):
        self.col, self.products = client.collection("product_images"), client.collection("products")

    def get(self, pid):
        data = bytes((self.col.document(pid).get().to_dict() or {}).get("image") or b"")
        kind = image_type(data) if data else None
        return (data, kind) if kind else (None, None)

    def save(self, pid, data):
        self.col.document(pid).set({"image": data})
        self.products.document(pid).update({"image_v": hashlib.sha256(data).hexdigest()[:12]})

    def remove(self, pid):
        self.col.document(pid).delete()
        self.products.document(pid).update({"image_v": ""})


def svc():
    return current_app.extensions["svc"]


def current(owner):
    """(look, preview): the owner may try a look on the store with ?look= before keeping it."""
    asked = request.args.get("look", "")
    if owner and asked in LOOKS:
        return asked, asked
    return svc().looks.get(), None


# ---------------------------------------------------------------- the owner's side
looks_admin = Blueprint("looks_admin", __name__, url_prefix="/admin")


@looks_admin.before_request
def protect():
    from views_admin import guard
    if request.method == "POST":
        request.max_content_length = MAX_UPLOAD
    return guard()


@looks_admin.post("/look")
def save():
    look = request.form.get("look", "")
    if look not in LOOKS:
        abort(400)
    svc().looks.save(look)
    flash(f"صار شكل عرض التصنيف: «{LOOKS[look][0]}».")
    if request.form.get("back") == "store":
        return redirect(url_for("shop.store", cat=request.form.get("cat") or None), 303)
    return redirect(url_for("cats_admin.categories") + "#look", 303)


@looks_admin.post("/products/<pid>/picture")
def picture(pid):
    if not svc().shop.get(pid):
        abort(404)
    back = url_for("shop_admin.product", pid=pid) + "#picture"
    if request.form.get("remove") == "1":
        svc().pictures.remove(pid)
        flash("تم حذف صورة المنتج.")
        return redirect(back, 303)
    upload = request.files.get("image")
    data = upload.read(MAX_UPLOAD) if upload and upload.filename else b""
    if not data or not image_type(data):
        flash(NO_IMAGE, "error")
    elif len(data) > MAX_IMAGE:
        flash(f"حجم الصورة {len(data) / 1024 / 1024:.1f} ميجابايت، والحد 300 كيلوبايت. صغّرها أو اختر صورة أصغر.", "error")
    else:
        svc().pictures.save(pid, data)
        flash("تم حفظ صورة المنتج.")
    return redirect(back, 303)


# ---------------------------------------------------------------- the picture, for the store page
prod_images = Blueprint("prod_images", __name__)


@prod_images.get("/pi/<pid>/image")
def image(pid):
    if too_many("public", 120):
        abort(429)
    product = svc().shop.get(pid)
    if not product or not product.get("image") or (not product["active"] and not signed_in(svc().cfg)):
        abort(404)
    data, kind = svc().pictures.get(pid)
    if not data:
        abort(404)
    lasting = request.args.get("v") == product["image"]
    return Response(data, mimetype=kind,
                    headers={"Cache-Control": "public, max-age=604800, immutable" if lasting else "no-store"})
