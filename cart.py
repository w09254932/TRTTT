"""The cart: the customer puts products in it with a quantity, sees the total, then completes the order. Buying needs
a signed-in account with a name and a mobile, which are written into the order, so the pay page asks for nothing but
the two agreements. The cart lives in the browser's session (product ids and quantities only)."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from accounts import signed_in_customer
from manual import read_fields
from products import PID_RE
from security import check_csrf, too_many
from validate import clean_email, clean_text

cart = Blueprint("cart", __name__)
MAX_QTY = 10        # of one product in one order
MAX_LINES = 20


def svc():
    return current_app.extensions["svc"]


def contents():
    """{pid: qty} in the order they were added, cleaned. Kept as [[pid, qty], ...] in the session:
    the session cookie sorts dict keys, which shuffled the cart and the order title."""
    raw = session.get("cart") or []
    pairs = raw.items() if isinstance(raw, dict) else [p for p in raw if isinstance(p, list) and len(p) == 2]
    return {str(pid): min(MAX_QTY, int(qty)) for pid, qty in pairs
            if PID_RE.match(str(pid)) and str(qty).isdigit() and int(qty) > 0}


def count():
    """Items in the cart, for the top bar."""
    return sum(contents().values()) if "cart" in session else 0


def _save(items):
    if items:
        session["cart"] = [[pid, qty] for pid, qty in items.items()]
    else:
        session.pop("cart", None)


@cart.before_request
def limit():
    if too_many("cart", 90):
        abort(429)
    if request.method == "POST":
        check_csrf()


def _lines():
    """[(product, qty)] of the cart, dropping what is no longer sold; qty is capped by what is left."""
    rows, items, changed = [], contents(), False
    for pid, qty in list(items.items()):
        product = svc().shop.get(pid)
        if not product or not product["active"]:
            items.pop(pid)
            changed = True
            continue
        rows.append((product, qty))
    if changed:
        _save(items)
    return rows


def _back():
    target = request.form.get("back", "")
    return redirect(target if target.startswith("/") and not target.startswith("//") else url_for(".view"), 303)


@cart.post("/cart/add/<pid>")
def add(pid):
    product = svc().shop.get(pid) if PID_RE.match(pid) else None
    if not product or not product["active"] or product["available"] <= 0:
        flash("هذا المنتج غير متاح الآن.", "error")
        return redirect(url_for("shop.store"), 303)
    items = contents()
    if len(items) >= MAX_LINES and pid not in items:
        flash("سلتك ممتلئة. أكمل طلبك الحالي أولاً.", "error")
        return redirect(url_for(".view"), 303)
    want = items.get(pid, 0) + 1
    if want > min(MAX_QTY, product["available"]):
        flash(f"لا يمكن إضافة المزيد من «{product['name']}».", "error")
    else:
        items[pid] = want
        _save(items)
        flash(f"أُضيف «{product['name']}» إلى السلة.")
    return _back()


@cart.post("/cart/set/<pid>")
def set_qty(pid):
    items = contents()
    if pid not in items:
        return redirect(url_for(".view"), 303)
    qty = request.form.get("qty", "")
    qty = int(qty) if qty.isdigit() else items[pid]
    product = svc().shop.get(pid)
    if qty <= 0 or not product:
        items.pop(pid, None)
    else:
        items[pid] = min(qty, MAX_QTY, max(1, product["available"]))
    _save(items)
    return redirect(url_for(".view"), 303)


@cart.get("/cart")
def view(form=None, code=200):
    rows = _lines()
    total = round(sum(p["price"] * q for p, q in rows), 2)
    return render_template("cart.html", rows=rows, total=total, me=signed_in_customer(), brand=svc().brand.get(),
                           max_qty=MAX_QTY, form=form or {}), code


@cart.post("/cart/checkout")
def checkout():
    """The cart becomes one order, paid on its pay page."""
    me = signed_in_customer()
    if not me:
        flash("سجّل الدخول لإتمام طلبك.", "error")
        return redirect(url_for("account.login", next=url_for(".view")), 303)
    if not me["name"] or not me["phone"]:
        flash("أكمل اسمك ورقم جوالك ليُكتبا في طلبك، ثم ارجع إلى السلة.", "error")
        return redirect(url_for("account.details", next=url_for(".view")), 303)
    gift, error = _gift(me)
    rows = _lines()
    answers = {}
    for product, _ in rows:      # what each product done by hand needs from the customer
        if product["manual"] and not error:
            answers[product["id"]], error = read_fields(request.form, product)
    if error:
        flash(error, "error")
        return view(request.form, 400)
    if too_many("order", 10, per=600):
        abort(429)
    token, error = svc().shop.order([(p["id"], q) for p, q in rows], person(me), me["id"], lambda: _customer(me),
                                    answers=answers)
    if error:
        flash(error, "error")
        return redirect(url_for(".view"), 303)
    if gift:
        svc().store.set_gift(svc().store.by_token(token)["id"], gift["email"], me["name"], gift["note"])
    _save({})
    return redirect(url_for("pub.invoice", token=token), 303)


def _gift(me):
    """({email, note}, None) when the order is a gift, (None, None) when not, or (None, why)."""
    if request.form.get("for") != "gift":
        return None, None
    if not request.form.get("gift_email", "").strip():
        return None, "اكتب بريد صديقك لتصله الهدية."
    email, error = clean_email(request.form.get("gift_email", ""))
    if error:
        return None, "اكتب بريد صديقك صحيحاً لتصله الهدية."
    if email == me["email"]:
        return None, "هذا بريدك أنت. للهدية اكتب بريد صديقك، أو أكمل الطلب دون هدية."
    return {"email": email, "note": clean_text(request.form.get("gift_note", ""), 200)}, None


@cart.post("/order/<pid>")
def buy(pid):
    """Buy now: one of this product, straight to its pay page (signed in, like the cart)."""
    product = svc().shop.get(pid) if PID_RE.match(pid) else None
    if not product or not product["active"] or product["available"] <= 0:
        flash("هذا المنتج غير متاح الآن. اختر منتجاً آخر.", "error")
        return redirect(url_for("shop.store"), 303)
    if product["manual"]:      # it needs the customer's details, written in the cart
        items = contents()
        items[pid] = max(1, items.get(pid, 0))
        _save(items)
        flash(f"اكتب بيانات «{product['name']}» في السلة ثم أكمل طلبك.")
        return redirect(url_for(".view"), 303)
    me = signed_in_customer()
    if not me:
        flash("سجّل الدخول لإتمام الشراء.", "error")
        return redirect(url_for("account.login", next=url_for("shop.store")), 303)
    if not me["name"] or not me["phone"]:
        flash("أكمل اسمك ورقم جوالك ليُكتبا في طلبك.", "error")
        return redirect(url_for("account.details", next=url_for("shop.store")), 303)
    if too_many("order", 10, per=600):
        abort(429)
    token, error = svc().shop.order([(pid, 1)], person(me), me["id"], lambda: _customer(me))
    if error:
        flash(error, "error")
        return redirect(url_for("shop.store"), 303)
    return redirect(url_for("pub.invoice", token=token), 303)


def person(me):
    return {"name": me["name"], "phone": me["phone"], "email": me["email"]}


def _customer(me):
    """The order joins the owner's customer book under the account's mobile (a saved email is never replaced)."""
    return svc().book.remember(person(me), new_invoice=True, overwrite=False)
