"""The shop: products, each with a stock of codes, the public store page, and the codes handed out for a paid
order (several products and quantities, from the cart: cart.py). Codes are encrypted like everything private. A product counts its own stock, and a code is held for an
order from the moment its customer goes to pay, so two customers can never pay for the same last code."""
import re
import secrets
import time

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from google.cloud import firestore

from db import ID_CHARS, _read, now
from manual import MAX_FIELDS, is_manual, manual_block, open_answers, seal_answers
from security import signed_in, too_many
from validate import clean_amount, clean_text

ORDER_MINUTES = 60      # a shop order, and the code held for it while it is being paid, stays open this long
MAX_ADD = 400           # codes added in one go
CODE_LIMIT = 500        # characters in one code (a code can also be a link)
PID_RE = re.compile(r"^[A-Z2-9]{10}$")
CID_RE = re.compile(r"^\d{20}[a-z0-9]{4}$")
MANUAL_STOCK = 99       # a product done by hand never runs out; the cart still caps one order's quantity


def _live(holds, moment, skip=None):
    """The holds of orders still being paid (one per code), leaving out those of the order `skip`."""
    return {key: until for key, until in (holds or {}).items() if until > moment and not (skip and _mine(key, skip))}


def _mine(key, inv_id):
    return key == inv_id or key.startswith(inv_id + "#")


def _hold_keys(inv_id, qty):
    """One hold per code: the order's id for the first, then id#1, id#2..."""
    return [inv_id if n == 0 else f"{inv_id}#{n}" for n in range(max(0, qty))]


def lines_of(inv):
    """The order's lines [{pid, qty, name, price, note}]; an order from before the cart is one of its product."""
    if inv.get("lines"):
        return inv["lines"]
    return [{"pid": inv["product"], "qty": 1, "name": "", "price": 0, "note": ""}] if inv.get("product") else []


def order_title(lines):
    parts = [line["name"] + (f" × {line['qty']}" if line["qty"] > 1 else "") for line in lines]
    return " + ".join(parts)[:120]


def goods_text(lines, picked, answers=None):
    """The order emailed to the customer: each product's codes, then how to use them; a product done by hand
    says what the customer typed for it and that the store carries it out."""
    if len(lines) == 1 and lines[0]["qty"] == 1 and not is_manual(lines[0]):
        codes, note = picked.get(lines[0]["pid"], []), lines[0].get("note", "")
        return f"{codes[0]}\n\n{note}" if codes and note else (codes[0] if codes else "")
    blocks = []
    for line in lines:
        if is_manual(line):
            blocks.append(manual_block(line, answers or {}))
            continue
        codes = picked.get(line["pid"], [])
        if not codes:
            continue
        head = f"{line['name']} × {line['qty']}" if line["name"] else ""
        blocks.append("\n".join(([head] if head else []) + codes + ([line["note"]] if line.get("note") else [])))
    return "\n\n".join(blocks)


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
        manual = data.get("delivery") == "manual"
        return {"id": snap.id, "name": data.get("name", ""), "description": data.get("description", ""),
                "note": data.get("note", ""), "price": data.get("price", 0.0), "active": data.get("active", True),
                "stock": stock, "held": held, "available": MANUAL_STOCK if manual else max(0, stock - held),
                "sold": data.get("sold", 0), "created_at": data.get("created_at"),
                "manual": manual, "eta": data.get("eta", ""), "fields": data.get("fields") or []}

    def get(self, pid):
        return self._view(self.products.document(pid).get()) if PID_RE.match(pid or "") else None

    def all(self):
        rows = [self._view(snap) for snap in self.products.stream()]
        return sorted((r for r in rows if r), key=lambda r: r["created_at"] or now())

    def catalog(self):
        return [p for p in self.all() if p["active"]]

    def create(self, name, description, note, price, active=True, delivery="codes", eta="", fields=()):
        pid = "".join(secrets.choice(ID_CHARS) for _ in range(10))
        self.products.document(pid).set({"name": name, "description": description, "note": note, "price": price,
                                         "active": active, "stock": 0, "sold": 0, "holds": {}, "created_at": now(),
                                         "delivery": delivery, "eta": eta, "fields": list(fields)})
        return pid

    def update(self, pid, name, description, note, price, active, delivery="codes", eta="", fields=()):
        self.products.document(pid).update({"name": name, "description": description, "note": note,
                                            "price": price, "active": active, "delivery": delivery, "eta": eta,
                                            "fields": list(fields)})

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
    def order(self, lines, person, buyer, customer=None, answers=None):
        """Opens an invoice for the cart's lines [(pid, qty)] of a signed-in customer, with their details written in.
        answers holds what the customer typed for each product done by hand. Returns its token, or (None, why)
        when a product cannot be bought in that quantity."""
        picked = []
        for pid, qty in lines:
            product = self.get(pid)
            if not product or not product["active"] or product["available"] <= 0:
                return None, f"«{product['name']}» غير متاح الآن." if product else "منتج في سلتك لم يعد موجوداً."
            if qty > product["available"]:
                return None, f"المتوفر من «{product['name']}» {product['available']} فقط."
            line = {"pid": pid, "qty": qty, "name": product["name"], "price": product["price"], "note": product["note"]}
            if product["manual"]:
                line.update(manual=True, eta=product["eta"])
            picked.append(line)
        if not picked:
            return None, "سلتك فارغة."
        amount = round(sum(line["price"] * line["qty"] for line in picked), 2)
        customer = customer() if callable(customer) else customer
        inv_id = self.store.create_invoice(amount, {"title": order_title(picked), **person}, 0, customer=customer,
                                           extra={"product": picked[0]["pid"], "lines": picked, "buyer": buyer},
                                           minutes=ORDER_MINUTES)
        mine = {pid: pairs for pid, pairs in (answers or {}).items() if any(l["pid"] == pid and is_manual(l) for l in picked)}
        if mine:
            self.store.invoices.document(inv_id).update({"answers": seal_answers(self.vault, inv_id, mine)})
        return self.store.get(inv_id)["token"], None

    def hold(self, inv):
        """Keeps the order's codes aside while its customer pays. False when one product has too few left.
        A product done by hand has no codes to keep."""
        lines = [line for line in lines_of(inv) if not is_manual(line)]
        if not lines:
            return True
        refs = {line["pid"]: self.products.document(line["pid"]) for line in lines}

        def work(tx):
            moment, changes = now(), {}
            snaps = {pid: _read(tx, ref) for pid, ref in refs.items()}
            for line in lines:
                snap = snaps[line["pid"]]
                data = snap.to_dict() or {}
                if not snap.exists or not data.get("active", True):
                    return False
                holds = _live(data.get("holds"), moment, skip=inv["id"])
                if data.get("stock", 0) - len(holds) < line["qty"]:
                    return False
                for key in _hold_keys(inv["id"], line["qty"]):
                    holds[key] = inv["expires_at"]
                changes[line["pid"]] = holds
            for pid, holds in changes.items():
                tx.update(refs[pid], {"holds": holds})
            return True

        return self._run(work)

    def release(self, inv):
        """A cancelled order gives back the codes it was holding."""
        for line in lines_of(inv):
            ref = self.products.document(line["pid"])

            def work(tx):
                snap = _read(tx, ref)
                holds = (snap.to_dict() or {}).get("holds") or {}
                if snap.exists and any(_mine(key, inv["id"]) for key in holds):
                    tx.update(ref, {"holds": _live(holds, now(), skip=inv["id"])})

            self._run(work)

    def missing(self, inv):
        """How many codes a paid shop order is still owed."""
        given = inv.get("given") or {}
        return sum(max(0, line["qty"] - len(given.get(line["pid"], []))) for line in lines_of(inv) if not is_manual(line))

    def assign(self, inv_id):
        """Gives a paid shop order its codes, once each: they become the order emailed to the customer.
        Returns 'assigned', 'already', 'partial' (stock ran out on the way), 'empty' (no code at all),
        'unpaid' or 'none' (not a shop order)."""
        inv_ref = self.store.invoices.document(inv_id)
        data = inv_ref.get().to_dict() or {}
        lines = lines_of(data)
        if not lines:
            return "none"
        if not data.get("lines") and data.get("code_id"):
            return "already"     # an order from before the cart, handed out already
        handed, short, given = 0, False, data.get("given") or {}
        if any(is_manual(line) for line in lines) and self._queue(inv_ref, inv_id) == "queued":
            handed += 1      # the order's products done by hand wait in the owner's queue from now
        for line in lines:
            if is_manual(line):
                continue
            for _ in range(max(0, line["qty"] - len(given.get(line["pid"], [])))):
                result = self._give_one(inv_ref, inv_id, line)
                if result == "given":
                    handed += 1
                    continue
                if result == "unpaid":
                    return "unpaid"
                short = short or result == "empty"
                break
        if short:
            return "partial" if handed or (inv_ref.get().to_dict() or {}).get("given") else "empty"
        return "assigned" if handed else "already"

    def _queue(self, inv_ref, inv_id):
        """Puts a paid order's products done by hand in the owner's queue, once; its email says they are coming.
        'queued', 'already' or 'unpaid'."""
        def work(tx):
            inv = _read(tx, inv_ref).to_dict() or {}
            if inv.get("status") != "paid":
                return "unpaid"
            if inv.get("work"):
                return "already"
            inv["id"] = inv_id
            lines = lines_of(inv)
            manual = [line for line in lines if is_manual(line)]
            refs = {line["pid"]: self.products.document(line["pid"]) for line in manual}
            snaps = {pid: _read(tx, ref) for pid, ref in refs.items()}
            picked = self.vault.open(inv["picked"], "picked:" + inv_id) if inv.get("picked") else {}
            goods = goods_text(lines, picked, open_answers(self.vault, inv))
            tx.update(inv_ref, {"work": {"state": "queued", "queued_at": now()}, "work_open": True,
                                "goods": self.store._seal_goods(goods, inv_id)})
            for line in manual:
                if snaps[line["pid"]].exists:
                    tx.update(refs[line["pid"]], {"sold": firestore.Increment(line["qty"])})
            return "queued"

        return self._run(work)

    def _give_one(self, inv_ref, inv_id, line):
        """One code of this line to the order. 'given', 'full' (the line has all its codes), 'empty' or 'unpaid'."""
        pid, ref = line["pid"], self.products.document(line["pid"])
        for _ in range(6):
            free = self._first_free(pid)

            def work(tx):
                inv = _read(tx, inv_ref).to_dict() or {}
                if inv.get("status") != "paid":
                    return "unpaid"
                given = inv.get("given") or {}
                if len(given.get(pid, [])) >= line["qty"]:
                    return "full"
                snap = _read(tx, ref)
                if not free:
                    return "empty"
                code_ref = self._codes(pid).document(free.id)
                code = _read(tx, code_ref)
                if not code.exists or (code.to_dict() or {}).get("state") != "free":
                    return "retry"     # sold to another order a moment ago: take the next one
                picked = self.vault.open(inv.get("picked", ""), "picked:" + inv_id) if inv.get("picked") else {}
                picked.setdefault(pid, []).append(self._open_code(pid, code))
                given.setdefault(pid, []).append(free.id)
                left = line["qty"] - len(given[pid])     # this order's codes of the product still held
                holds = _live((snap.to_dict() or {}).get("holds"), now(), skip=inv_id)
                for key in _hold_keys(inv_id, left):
                    holds[key] = (inv.get("expires_at") or now())
                tx.update(code_ref, {"state": "sold", "invoice": inv_id, "sold_at": now()})
                lines = [dict(item) for item in lines_of(inv)]
                for item in lines:     # an order from before the cart takes the product's note now
                    if item["pid"] == pid and not item.get("note"):
                        item["note"] = (snap.to_dict() or {}).get("note", "")
                inv["id"] = inv_id
                changes = {"given": given, "picked": self.vault.seal(picked, "picked:" + inv_id),
                           "goods": self.store._seal_goods(goods_text(lines, picked, open_answers(self.vault, inv)), inv_id)}
                if not inv.get("code_id"):
                    changes["code_id"] = free.id
                tx.update(inv_ref, changes)
                tx.update(ref, {"stock": firestore.Increment(-1), "sold": firestore.Increment(1), "holds": holds})
                return "given"

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
              "active": form.get("active") == "1",
              "delivery": "manual" if form.get("delivery") == "manual" else "codes",
              "eta": clean_text(form.get("eta", ""), 40), "fields": []}
    if fields["delivery"] == "manual":
        for number in range(MAX_FIELDS):
            label = clean_text(form.get(f"field_{number}", ""), 40)
            if label:
                fields["fields"].append({"label": label, "required": form.get(f"field_{number}_req") == "1"})
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
    pid = svc().shop.create(fields["name"], fields["description"], fields["note"], fields["price"], fields["active"],
                            fields["delivery"], fields["eta"], fields["fields"])
    flash("تمت إضافة المنتج، وينفَّذ يدوياً." if fields["delivery"] == "manual" else "تمت إضافة المنتج. أضف أكواده الآن.")
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
    svc().shop.update(pid, fields["name"], fields["description"], fields["note"], fields["price"], fields["active"],
                      fields["delivery"], fields["eta"], fields["fields"])
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
