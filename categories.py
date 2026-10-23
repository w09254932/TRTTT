"""Store categories: a name and a picture each, shown as a grid on the store page. Every product may sit in one
category or in none. Pictures are kept in Firestore and served from this site (the content security policy stays
strict), like the shop's logo."""
import hashlib
import secrets
import time

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for

from brand import image_type
from db import ID_CHARS, now
from security import too_many
from validate import clean_text

MAX_IMAGE = 300 * 1024
MAX_UPLOAD = 12 * 1024 * 1024     # a phone photo arrives whole when the browser could not shrink it, and is refused kindly
MAX_CATEGORIES = 40
NO_IMAGE = "صورة التصنيف يجب أن تكون PNG أو JPG أو WebP بحجم أقل من 300 كيلوبايت."
CAT_RE_LEN = 8


class Categories:
    def __init__(self, client, ttl=30):
        self.col = client.collection("categories")
        self.ttl, self._cache, self._at = ttl, None, 0.0

    def _view(self, snap):
        data = snap.to_dict() or {}
        image = bytes(data.get("image") or b"")
        kind = image_type(image) if image else None
        return {"id": snap.id, "name": data.get("name", ""), "order": data.get("order", 0),
                "image": image if kind else None, "image_type": kind,
                "version": hashlib.sha256(data.get("name", "").encode() + image).hexdigest()[:12],
                "created_at": data.get("created_at")}

    def all(self, fresh=False):
        """Every category, in the order the owner set (then oldest first). Cached briefly per worker."""
        if fresh or self._cache is None or time.time() - self._at > self.ttl:
            rows = [self._view(snap) for snap in self.col.stream()]
            self._cache = sorted(rows, key=lambda c: (c["order"], c["created_at"] or now()))
            self._at = time.time()
        return self._cache

    def get(self, cid):
        return next((c for c in self.all() if c["id"] == cid), None) if cid else None

    def create(self, name, image=None):
        cid = "".join(secrets.choice(ID_CHARS) for _ in range(CAT_RE_LEN))
        order = max([c["order"] for c in self.all(fresh=True)] + [0]) + 1
        self.col.document(cid).set({"name": name, "image": image, "order": order, "created_at": now()})
        self.all(fresh=True)
        return cid

    def update(self, cid, name, image=None, remove_image=False, order=None):
        changes = {"name": name}
        if image:
            changes["image"] = image
        elif remove_image:
            changes["image"] = None
        if order is not None:
            changes["order"] = order
        self.col.document(cid).update(changes)
        self.all(fresh=True)

    def delete(self, cid, products):
        """The category goes; its products stay in the store without a category."""
        for snap in products.where("category", "==", cid).stream():
            products.document(snap.id).update({"category": ""})
        self.col.document(cid).delete()
        self.all(fresh=True)


def svc():
    return current_app.extensions["svc"]


# ---------------------------------------------------------------- the owner's page
cats_admin = Blueprint("cats_admin", __name__, url_prefix="/admin")


@cats_admin.before_request
def protect():
    from views_admin import guard
    if request.method == "POST":
        request.max_content_length = MAX_UPLOAD
    return guard()


def _image():
    """(bytes or None, error) of the uploaded picture."""
    upload = request.files.get("image")
    data = upload.read(MAX_UPLOAD) if upload and upload.filename else b""
    if data and not image_type(data):
        return None, NO_IMAGE
    if len(data) > MAX_IMAGE:
        return None, (f"حجم الصورة {len(data) / 1024 / 1024:.1f} ميجابايت، والحد 300 كيلوبايت. "
                      "صغّرها أو اختر صورة أصغر، أو افتح الصفحة من متصفح حديث فيصغّرها وحده.")
    return data or None, None


def _counts():
    counts = {}
    for p in svc().shop.all():
        if p.get("category"):
            counts[p["category"]] = counts.get(p["category"], 0) + 1
    return counts


@cats_admin.get("/categories")
def categories(error=None, code=200):
    from looks import LOOKS
    return render_template("categories.html", rows=svc().categories.all(fresh=True), counts=_counts(),
                           error=error, looks=LOOKS, look=svc().looks.get(fresh=True)), code


@cats_admin.post("/categories")
def create():
    name = clean_text(request.form.get("name", ""), 40)
    image, error = _image()
    if len(name) < 2:
        error = "اكتب اسم التصنيف."
    elif len(svc().categories.all(fresh=True)) >= MAX_CATEGORIES:
        error = f"وصلت إلى {MAX_CATEGORIES} تصنيفاً."
    if error:
        return categories(error, 400)
    svc().categories.create(name, image)
    flash(f"تمت إضافة تصنيف «{name}». اختره الآن في صفحة كل منتج يدخل فيه.")
    return redirect(url_for(".categories"), 303)


@cats_admin.post("/categories/<cid>")
def edit(cid):
    if not svc().categories.get(cid):
        abort(404)
    name = clean_text(request.form.get("name", ""), 40)
    image, error = _image()
    order = request.form.get("order", "")
    if len(name) < 2:
        error = "اكتب اسم التصنيف."
    if error:
        return categories(error, 400)
    svc().categories.update(cid, name, image, request.form.get("remove_image") == "1",
                            int(order) if order.isdigit() and len(order) < 4 else None)
    flash("تم حفظ التصنيف.")
    return redirect(url_for(".categories"), 303)


@cats_admin.post("/categories/<cid>/delete")
def delete(cid):
    if not svc().categories.get(cid):
        abort(404)
    svc().categories.delete(cid, svc().shop.products)
    flash("تم حذف التصنيف، وبقيت منتجاته في المتجر بلا تصنيف.")
    return redirect(url_for(".categories"), 303)


# ---------------------------------------------------------------- the picture, for the store page
cat_images = Blueprint("cat_images", __name__)


@cat_images.get("/c/<cid>/image")
def image(cid):
    if too_many("public", 120):
        abort(429)
    cat = svc().categories.get(cid)
    if not cat or not cat["image"]:
        abort(404)
    lasting = request.args.get("v") == cat["version"]
    return Response(cat["image"], mimetype=cat["image_type"],
                    headers={"Cache-Control": "public, max-age=604800, immutable" if lasting else "no-store"})
