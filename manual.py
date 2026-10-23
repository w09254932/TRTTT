"""Products the store carries out by hand (a top-up to a player ID, a subscription) beside the instant codes.

A manual product asks the customer for what is needed to carry it out (up to three fields the owner names). Once
the order is paid it waits in the owner's queue: received, then being done, then done with a note and an optional
reference that are emailed to the customer. The customer sees the same three steps on the result page and in
their orders. What the customer typed is kept encrypted with the order."""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from google.cloud import firestore

from db import KSA, _read, now
from validate import clean_text

MAX_FIELDS = 3
FIELD_LIMIT = 120
STEPS = (("queued", "استلمنا طلبك"), ("working", "قيد التنفيذ"), ("done", "تم التنفيذ"))
STATE_AR = dict(STEPS)


def svc():
    return current_app.extensions["svc"]


def is_manual(line):
    return bool(line.get("manual"))


def seal_answers(vault, inv_id, answers):
    return vault.seal({"a": answers}, "answers:" + inv_id)


def open_answers(vault, inv):
    """{pid: [[label, value], ...]} the customer typed for the order's manual products."""
    if not inv.get("answers"):
        return {}
    return vault.open(inv["answers"], "answers:" + inv["id"]).get("a", {}) or {}


def answers_for(inv):
    """[(product name, [(label, value)])] for templates."""
    answers = open_answers(svc().store.vault, inv)
    lines = inv.get("lines") or []
    return [(line["name"], [tuple(pair) for pair in answers.get(line["pid"], []) if pair[1]])
            for line in lines if is_manual(line)]


def steps_of(inv):
    """[(label, 'done' | 'now' | '')] for the progress bar of a paid order with manual products, else []."""
    work = inv.get("work") or {}
    if inv.get("status") not in ("paid", "refunded") or not work:
        return []
    marks = {"queued": ("done", "", ""), "working": ("done", "now", ""), "done": ("done", "done", "done")}
    return list(zip((label for _, label in STEPS), marks.get(work.get("state"), marks["queued"])))


def read_fields(form, product):
    """([[label, value]], error) for one manual product of the cart, from the checkout form."""
    pairs = []
    for number, field in enumerate(product.get("fields") or []):
        value = clean_text(form.get(f"m_{product['id']}_{number}", ""), FIELD_LIMIT)
        if field.get("required") and not value:
            return None, f"اكتب «{field['label']}» لـ «{product['name']}»."
        pairs.append([field["label"], value])
    return pairs, None


def manual_block(line, answers):
    """The part of the customer's order email for a product done by hand."""
    head = f"{line['name']} × {line['qty']}" if line.get("name") else ""
    eta = f" {line['eta']}" if line.get("eta") else ""
    rows = [f"{label}: {value}" for label, value in answers.get(line["pid"], []) if value]
    tail = [f"ينفّذه المتجر{eta}، ويصلك إشعار على بريدك حين يُنفّذ."]
    return "\n".join(([head] if head else []) + rows + tail + ([line["note"]] if line.get("note") else []))


# ---------------------------------------------------------------- the owner's queue
manual_admin = Blueprint("manual_admin", __name__, url_prefix="/admin")


@manual_admin.before_request
def protect():
    from views_admin import guard
    return guard()


def waiting(store):
    """Paid orders with manual products not done yet, oldest payment first."""
    rows = [store._load(snap) for snap in store.invoices.where("work_open", "==", True).stream()]
    rows = [inv for inv in rows if inv and inv.get("status") == "paid"]
    return sorted(rows, key=lambda inv: inv.get("paid_at") or inv["created_at"])


@manual_admin.get("/manual")
def queue():
    rows = waiting(svc().store)
    return render_template("manual_queue.html", rows=rows, answers_for=answers_for, state_ar=STATE_AR, moment=now())


def _move(inv_id, to, extra=None):
    """Moves an order's work forward in one transaction. Returns the invoice, or None when it cannot move."""
    store = svc().store
    ref = store.invoices.document(inv_id)

    def work(tx):
        snap = _read(tx, ref)
        inv = snap.to_dict() or {}
        current = (inv.get("work") or {}).get("state")
        if not snap.exists or inv.get("status") != "paid" or not current or current == "done":
            return None
        if to == "working" and current != "queued":
            return None
        moment = now()
        work_ = {**inv["work"], "state": to, f"{to}_at": moment, **(extra or {})}
        tx.update(ref, {"work": work_, "work_open": to != "done"})
        return True

    return store.get(inv_id) if store._run(work) else None


@manual_admin.post("/invoices/<inv_id>/work/start")
def start(inv_id):
    inv = _move(inv_id, "working")
    flash("بدأت تنفيذ الطلب. يرى العميل «قيد التنفيذ»." if inv else "لا يمكن بدء هذا الطلب.",
          "info" if inv else "error")
    return redirect(request.form.get("back") if request.form.get("back") == "/admin/manual"
                    else url_for("admin.invoice", inv_id=inv_id) + "#work", 303)


@manual_admin.post("/invoices/<inv_id>/work/done")
def finish(inv_id):
    s = svc()
    note = clean_text(request.form.get("note", ""), 300)
    proof = clean_text(request.form.get("ref", ""), 80)
    sealed = s.store.vault.seal({"note": note, "ref": proof}, "work:" + inv_id)
    inv = _move(inv_id, "done", {"proof": sealed})
    if not inv:
        flash("هذا الطلب نُفّذ من قبل أو لم يعد مدفوعاً.", "error")
        return redirect(url_for("admin.invoice", inv_id=inv_id), 303)
    sent = done_mail(s, inv, note, proof)
    flash("تم التنفيذ وأُبلغ العميل على بريده." if sent else "تم التنفيذ. تعذر إرسال الإيميل للعميل الآن.",
          "info" if sent else "error")
    return redirect(url_for(".queue"), 303)


def proof_of(inv):
    work = inv.get("work") or {}
    return svc().store.vault.open(work["proof"], "work:" + inv["id"]) if work.get("proof") else {}


def done_mail(s, inv, note, proof):
    """Tells the customer (or the friend a gift went to) that the products done by hand are done."""
    from notices import _send, message
    to = inv.get("deliver_email")
    if not to or not s.mailer.ready:
        return False
    names = "، ".join(f"{line['name']} × {line['qty']}" for line in inv.get("lines") or [] if is_manual(line))
    rows = [("الطلب", names), ("رقم الفاتورة", inv["id"]), ("رقم العملية", proof),
            ("وقت التنفيذ", now().astimezone(KSA).strftime("%Y-%m-%d %H:%M"))]
    mail = message(s, f"تم تنفيذ طلبك من {s.cfg.store_name}", "تم تنفيذ طلبك",
                   f"نفّذنا طلبك: {names}.", body=[note] if note else (), rows=rows, logo=True,
                   outro=f"إن واجهت مشكلة فتواصل مع {s.cfg.store_name} واذكر رقم الفاتورة.")
    error = _send(s, to, mail)
    if error:
        current_app.logger.warning("work done email failed for %s: %s", inv["id"], error)
    return not error
