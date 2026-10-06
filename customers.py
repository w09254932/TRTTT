"""The customer book. Each customer is stored encrypted and is found by a keyed fingerprint
of the phone number, so Firestore never holds a readable name, phone or email."""
import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from google.cloud import firestore

from db import now
from validate import clean_email, clean_name, clean_phone, western
from views_admin import guard

book = Blueprint("book", __name__)
book.before_request(guard)
CID_RE = re.compile(r"^[0-9a-f]{32}$")


class CustomerBook:
    def __init__(self, client, vault):
        self.db, self.vault = client, vault
        self.col = client.collection("customers")

    def _load(self, snap):
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        person = self.vault.open(data.get("enc", ""), "customer:" + snap.id)
        return {"id": snap.id, "name": person.get("name", ""), "phone": person.get("phone", ""),
                "email": person.get("email", ""), "invoices": data.get("invoices", 0), "last_at": data.get("last_at")}

    def get(self, cid):
        return self._load(self.col.document(cid).get()) if CID_RE.match(cid or "") else None

    def recent(self, limit=50):
        query = self.col.order_by("last_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [self._load(snap) for snap in query.stream()]

    def remember(self, person, new_invoice=True):
        """Saves or refreshes the customer who owns this phone. Returns the customer id (None without a phone)."""
        phone = person.get("phone")
        if not phone:
            return None
        cid = self.vault.index(phone)
        ref = self.col.document(cid)

        def work(tx):
            snap = next(iter(tx.get(ref)))
            old = self.vault.open((snap.to_dict() or {}).get("enc", ""), "customer:" + cid) if snap.exists else {}
            kept = {"name": person.get("name") or old.get("name", ""), "phone": phone,
                    "email": person.get("email") or old.get("email", "")}
            changes = {"enc": self.vault.seal(kept, "customer:" + cid), "last_at": now()}
            if new_invoice:
                changes["invoices"] = firestore.Increment(1)
            tx.set(ref, changes, merge=True)

        firestore.transactional(work)(self.db.transaction())
        return cid

    def update(self, person, name, email):
        kept = {"name": name, "phone": person["phone"], "email": email}
        self.col.document(person["id"]).update({"enc": self.vault.seal(kept, "customer:" + person["id"])})

    def forget(self, cid, erase=False):
        """Removes the customer from the book and unlinks their invoices. With erase=True the name,
        phone and email are also wiped from those invoices; amounts and payment history stay."""
        invoices = self.db.collection("invoices")
        for snap in invoices.where("customer", "==", cid).stream():
            changes = {"customer": firestore.DELETE_FIELD}
            if erase:
                private = self.vault.open((snap.to_dict() or {}).get("enc", ""), snap.id)
                changes["enc"] = self.vault.seal({"title": private.get("title", "")}, snap.id)
            invoices.document(snap.id).update(changes)
        self.col.document(cid).delete()


def svc():
    return current_app.extensions["svc"]


def _person(cid):
    person = svc().book.get(cid)
    if not person:
        abort(404)
    return person


@book.get("/customers")
def people():
    rows, query = svc().book.recent(300), request.args.get("q", "").strip()[:60]
    if query:
        needle = western(query).lower()
        as_phone = clean_phone(query)[0] or needle
        rows = [p for p in rows if needle in p["name"].lower() or needle in p["email"]
                or needle in p["phone"] or as_phone in p["phone"]]
    return render_template("customers.html", rows=rows, query=query)


@book.get("/customers/<cid>")
def person(cid, error=None, code=200):
    who = _person(cid)
    rows = svc().store.for_customer(cid)
    paid = [inv for inv in rows if inv["status"] == "paid"]
    return render_template("customer.html", who=who, rows=rows, error=error, paid=len(paid),
                           total=sum(inv["amount"] for inv in paid),
                           waiting=sum(1 for inv in rows if inv["status"] == "pending")), code


@book.post("/customers/<cid>")
def edit(cid):
    who = _person(cid)
    name, error = clean_name(request.form.get("name", ""))
    email = request.form.get("email", "").strip()
    if not error and email:
        email, error = clean_email(email)
    if error:
        return person(cid, error, 400)
    svc().book.update(who, name, email)
    flash("تم حفظ بيانات العميل.")
    return redirect(url_for(".person", cid=cid), 303)


@book.post("/customers/<cid>/delete")
def delete(cid):
    _person(cid)
    erase = request.form.get("erase") == "1"
    svc().book.forget(cid, erase)
    flash("تم حذف العميل ومسح بياناته من فواتيره." if erase else "تم حذف العميل من الدفتر.")
    return redirect(url_for(".people"), 303)
