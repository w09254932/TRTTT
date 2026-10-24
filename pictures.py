"""A picture for each product, shown on its card in the store (a product without one shows its category's
picture). Pictures are kept in Firestore apart from the products, so listing the store never loads them, and are
served from this site (the content security policy stays strict), like the category pictures."""
import hashlib

from flask import Blueprint, Response, abort, current_app, flash, redirect, request, url_for

from brand import image_type
from security import signed_in, too_many

MAX_IMAGE = 300 * 1024
MAX_UPLOAD = 12 * 1024 * 1024     # a phone photo arrives whole when the browser could not shrink it, and is refused kindly
NO_IMAGE = "صورة المنتج يجب أن تكون PNG أو JPG أو WebP بحجم أقل من 300 كيلوبايت."


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


# ---------------------------------------------------------------- the owner's side
pictures_admin = Blueprint("pictures_admin", __name__, url_prefix="/admin")


@pictures_admin.before_request
def protect():
    from views_admin import guard
    if request.method == "POST":
        request.max_content_length = MAX_UPLOAD
    return guard()


@pictures_admin.post("/products/<pid>/picture")
def picture(pid):
    if not svc().shop.get(pid):
        abort(404)
    back = url_for("shop_admin.product", pid=pid) + "#picture"
    if request.form.get("remove") == "1":
        svc().pictures.remove(pid)
        flash("تم حذف صورة المنتج، ويظهر الآن بصورة تصنيفه.")
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
