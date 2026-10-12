"""The shop: products, each with a stock of codes, the public store page, and one code handed out per paid
order. Codes are encrypted like everything private. A product counts its own stock, and a code is held for an
order from the moment its customer goes to pay, so two customers can never pay for the same last code."""
import re
import secrets
import time

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from google.cloud import firestore

from db import ID_CHARS, _read, now
from security import signed_in, too_many
from validate import clean_amount, clean_text

ORDER_MINUTES = 60      # a shop order, and the code held for it while it is being paid, stays open this long
MAX_ADD = 400           # codes added in one go
CODE_LIMIT = 500        # characters in one code (a code can also be a link)
PID_RE = re.compile(r"^[A-Z2-9]{10}$")
CID_RE = re.compile(r"^\d{20}[a-z0-9]{4}$")


def _live(holds, moment, skip=None):
    """The holds of orders still being paid."""
    return {inv: until for inv, until in (holds or {}).items() if until > moment and inv != skip}


class Shop:
    def __init__(self, client, vault, store):
        self.db, self.vault, self.store = client, vault, store
        self.products = client.collection("products")

    def _run(self, work):
        return firestore.transactional(work)(self.db.transaction())

    def _codes(self, pid):
        return self.products.document(pid).collection("codes")

    def _first_free(self, pid):
        """The oldest code not sold yet (codes are named by the time they were added)."""
        return next(iter(self._codes(pid).where("state", "==", "free").limit(1).stream()), None)

    def _open_code(self, pid, snap):
        return self.vault.open((snap.to_dict() or {}).get("enc", ""), f"code:{pid}:{snap.id}").get("code", "")

    # ---- products
    def _view(self, snap):
        if not snap.exists:
            return None
        data, moment = snap.to_dict() or {}, now()
        stock, held = data.get("stock", 0), len(_live(data.get("holds"), moment))
        return {"id": snap.id, "name": data.get("name", ""), "description": data.get("description", ""),
                "note": data.get("note", ""), "price": data.get("price", 0.0), "active": data.get("active", True),
                "stock": stock, "held": held, "available": max(0, stock - held), "sold": data.get("sold", 0),
                "created_at": data.get("created_at")}

    def get(self, pid):
        return self._view(self.products.document(pid).get()) if PID_RE.match(pid or "") else None

    def all(self):
        rows = [self._view(snap) for snap in self.products.stream()]
        return sorted((r for r in rows if r), key=lambda r: r["created_at"] or now())

    def catalog(self):
        return [p for p in self.all() if p["active"]]

    def create(self, name, description, note, price, active=True):
        pid = "".join(secrets.choice(ID_CHARS) for _ in range(10))
        self.products.document(pid).set({"name": name, "description": description, "note": note, "price": price,
                                         "active": active, "stock": 0, "sold": 0, "holds": {}, "created_at": now()})
        return pid

    def update(self, pid, name, description, note, price, active):
        self.products.document(pid).update({"name": name, "description": description, "note": note,
                                            "price": price, "active": active})

    def delete(self, pid):
        """Only a product with no codes left and no order being paid can go; returns False otherwise."""
        product = self.get(pid)
        if not product or product["stock"] or product["held"]:
            return False
        for snap in self._codes(pid).stream():
            self._codes(pid).document(snap.id).delete()
        self.products.document(pid).delete()
        return True

    # ---- codes
    def add_codes(self, pid, text):
        """Adds one code per line. Returns (added, already there). A code is recognised again by a keyed
        fingerprint, so the same code can never be in stock twice."""
        seen = {(snap.to_dict() or {}).get("fp") for snap in self._codes(pid).stream()}  # sold ones too
        fresh, repeated = [], 0
        for line in (text or "").replace("\r", "\n").split("\n"):
            code = " ".join(line.split())[:CODE_LIMIT]
            if not code:
                continue
            mark = self.vault.index(f"code:{pid}:{code}")
            if mark in seen:
                repeated += 1
                continue
            seen.add(mark)
            fresh.append((code, mark))
        fresh = fresh[:MAX_ADD]
        ref = self.products.document(pid)

        def work(tx):
            if not _read(tx, ref).exists:
                return 0
            for number, (code, mark) in enumerate(fresh):
                cid = f"{time.time_ns() + number:020d}{secrets.token_hex(2)}"
                tx.set(self._codes(pid).document(cid), {"enc": self.vault.seal({"code": code}, f"code:{pid}:{cid}"),
                                                         "fp": mark, "state": "free", "added_at": now()})
            if fresh:
                tx.update(ref, {"stock": firestore.Increment(len(fresh))})
            return len(fresh)

        return (self._run(work) if fresh else 0), repeated

    def codes(self, pid, state, limit=100):
        """[{id, code, invoice, at}] for the owner: free codes oldest first, sold codes newest first."""
        rows = []
        for snap in self._codes(pid).where("state", "==", state).limit(limit if state == "free" else 1000).stream():
            data = snap.to_dict() or {}
            rows.append({"id": snap.id, "code": self._open_code(pid, snap), "invoice": data.get("invoice", ""),
                         "at": data.get("sold_at") or data.get("added_at")})
        if state != "free":
            rows.sort(key=lambda r: r["at"], reverse=True)
        return rows[:limit]

    def remove_code(self, pid, cid):
        """Removes a code that was not sold. Returns False when it is gone or already sold."""
        ref, code_ref = self.products.document(pid), self._codes(pid).document(cid)

        def work(tx):
            snap = _read(tx, code_ref)
            if not snap.exists or (snap.to_dict() or {}).get("state") != "free":
                return False
            tx.delete(code_ref)
            tx.update(ref, {"stock": firestore.Increment(-1)})
            return True

        return self._run(work)

    # ---- orders
    def order(self, pid):
        """Opens an invoice for one of this product. Returns its token, or None when it cannot be bought."""
        product = self.get(pid)
        if not product or not product["active"] or product["available"] <= 0:
            return None
        inv_id = self.store.create_invoice(product["price"], {"title": product["name"]}, 0,
                                           extra={"product": pid}, minutes=ORDER_MINUTES)
        return self.store.get(inv_id)["token"]

    def hold(self, inv):
        """Keeps a code aside for this order while its customer pays. False when none is left for it."""
        ref = self.products.document(inv["product"])

        def work(tx):
            snap, moment = _read(tx, ref), now()
            data = snap.to_dict() or {}
            if not snap.exists or not data.get("active", True):
                return False
            holds = _live(data.get("holds"), moment, skip=inv["id"])
            if data.get("stock", 0) - len(holds) <= 0:
                return False
            holds[inv["id"]] = inv["expires_at"]
            tx.update(ref, {"holds": holds})
            return True

        return self._run(work)

    def release(self, inv):
        """A cancelled order gives back the code it was holding."""
        ref = self.products.document(inv["product"])

        def work(tx):
            snap = _read(tx, ref)
            holds = (snap.to_dict() or {}).get("holds") or {}
            if snap.exists and inv["id"] in holds:
                tx.update(ref, {"holds": _live(holds, now(), skip=inv["id"])})

        self._run(work)

    def assign(self, inv_id):
        """Gives a paid shop order its code, once: the code becomes the order emailed to the customer.
        Returns 'assigned', 'already', 'empty' (no code left), 'unpaid' or 'none' (not a shop order)."""
        inv_ref = self.store.invoices.document(inv_id)
        for _ in range(6):
            pid = (inv_ref.get().to_dict() or {}).get("product")
            if not pid or not PID_RE.match(pid):
                return "none"
            free = self._first_free(pid)
            ref = self.products.document(pid)

            def work(tx):
                inv = _read(tx, inv_ref).to_dict() or {}
                if inv.get("code_id"):
                    return "already"
                if inv.get("status") != "paid":
                    return "unpaid"
                snap = _read(tx, ref)
                holds = _live((snap.to_dict() or {}).get("holds"), now(), skip=inv_id)
                if not free:
                    if snap.exists:
                        tx.update(ref, {"holds": holds})
                    return "empty"
                code_ref = self._codes(pid).document(free.id)
                code = _read(tx, code_ref)
                if not code.exists or (code.to_dict() or {}).get("state") != "free":
                    return "retry"     # sold to another order a moment ago: take the next one
                text = self._open_code(pid, code)
                note = (snap.to_dict() or {}).get("note", "")
                tx.update(code_ref, {"state": "sold", "invoice": inv_id, "sold_at": now()})
                tx.update(inv_ref, {"code_id": free.id, "goods": self.store._seal_goods(
                    f"{text}\n\n{note}" if note else text, inv_id)})
                tx.update(ref, {"stock": firestore.Increment(-1), "sold": firestore.Increment(1), "holds": holds})
                return "assigned"

            result = self._run(work)
            if result != "retry":
                return result
        return "empty"


def svc():
    return current_app.extensions["svc"]


# ---------------------------------------------------------------- the public store
shop = Blueprint("shop", __name__)


@shop.before_request
def limit():
    if too_many("public", 120):
        abort(429)


@shop.get("/")
def store():
    return render_template("shop.html", products=svc().shop.catalog(), brand=svc().brand.get(),
                           owner=signed_in(svc().cfg))


@shop.post("/order/<pid>")
def order(pid):
    if too_many("order", 10, per=600):
        abort(429)
    token = svc().shop.order(pid) if PID_RE.match(pid) else None
    if not token:
        flash("هذا المنتج غير متاح الآن. اختر منتجاً آخر.", "error")
        return redirect(url_for(".store"), 303)
    return redirect(url_for("pub.invoice", token=token), 303)


# Links from before the dashboard moved to /admin (owner emails, bookmarks).
@shop.get("/login")
def old_login():
    return redirect(url_for("admin.login"), 301)


@shop.get("/invoices/<inv_id>")
def old_invoice(inv_id):
    return redirect(url_for("admin.invoice", inv_id=inv_id), 301)


# ---------------------------------------------------------------- the owner's side
shop_admin = Blueprint("shop_admin", __name__, url_prefix="/admin")


@shop_admin.before_request
def protect():
    from views_admin import guard
    return guard()


def _fields(form):
    """(fields, error) from the product form."""
    name = clean_text(form.get("name", ""), 80)
    price, error = clean_amount(form.get("price", ""))
    if len(name) < 2:
        error = "اكتب اسم المنتج."
    fields = {"name": name, "price": price, "description": clean_text(form.get("description", ""), 300),
              "note": "\n".join(" ".join(line.split()) for line in form.get("note", "").splitlines()).strip()[:1000],
              "active": form.get("active") == "1"}
    return fields, error


@shop_admin.get("/products")
def products(form=None, error=None, code=200):
    return render_template("products.html", rows=svc().shop.all(), form=form or {"active": "1"}, error=error,
                           store_link=svc().cfg.base_url + "/", mailing=svc().mailer.ready), code


@shop_admin.post("/products")
def create():
    fields, error = _fields(request.form)
    if error:
        return products(request.form, error, 400)
    pid = svc().shop.create(fields["name"], fields["description"], fields["note"], fields["price"], fields["active"])
    flash("تمت إضافة المنتج. أضف أكواده الآن.")
    return redirect(url_for(".product", pid=pid), 303)


def _product(pid):
    product = svc().shop.get(pid)
    if not product:
        abort(404)
    return product


@shop_admin.get("/products/<pid>")
def product(pid, error=None, code=200, form=None):
    item = _product(pid)
    return render_template("product.html", p=item, error=error, form=form, free=svc().shop.codes(pid, "free"),
                           sold=svc().shop.codes(pid, "sold", 50)), code


@shop_admin.post("/products/<pid>")
def edit(pid):
    _product(pid)
    fields, error = _fields(request.form)
    if error:
        return product(pid, error, 400, request.form)
    svc().shop.update(pid, fields["name"], fields["description"], fields["note"], fields["price"], fields["active"])
    flash("تم حفظ المنتج.")
    return redirect(url_for(".product", pid=pid), 303)


@shop_admin.post("/products/<pid>/codes")
def add_codes(pid):
    _product(pid)
    added, repeated = svc().shop.add_codes(pid, request.form.get("codes", ""))
    text = f"تمت إضافة {added} كود." if added else "لم يُضف أي كود."
    if repeated:
        text += f" تُرك {repeated} لأنه موجود من قبل."
    flash(text, "info" if added else "error")
    return redirect(url_for(".product", pid=pid), 303)


@shop_admin.post("/products/<pid>/codes/<cid>/delete")
def remove_code(pid, cid):
    _product(pid)
    done = CID_RE.match(cid) and svc().shop.remove_code(pid, cid)
    flash("تم حذف الكود." if done else "لا يمكن حذف هذا الكود لأنه بيع أو حُذف من قبل.", "info" if done else "error")
    return redirect(url_for(".product", pid=pid), 303)


@shop_admin.post("/products/<pid>/delete")
def delete(pid):
    _product(pid)
    if svc().shop.delete(pid):
        flash("تم حذف المنتج.")
        return redirect(url_for(".products"), 303)
    flash("لا يمكن حذف منتج فيه أكواد أو عليه طلب قيد الدفع. أوقف عرضه بدلاً من ذلك.", "error")
    return redirect(url_for(".product", pid=pid), 303)
