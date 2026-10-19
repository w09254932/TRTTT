"""Firestore storage: invoices, gateway events, counters and the login lock."""
import re
import secrets
import time
import warnings
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

warnings.filterwarnings("ignore", message="Detected filter using positional arguments")

KSA = timezone(timedelta(hours=3))
ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ID_RE = re.compile(r"^[A-Z2-9]{12}$")
STAT_KEYS = ("links", "pending", "paid", "cancelled", "expired", "refunded",
             "paid_amount", "refunded_amount", "views", "attempts", "declines")
MAX_ATTEMPTS = 25
GROUPS = {"pending": ("pending",), "paid": ("paid",), "ended": ("expired", "cancelled"), "refunded": ("refunded",)}
LOCK_SECONDS = 900
SENDING_GRACE = 300  # an order email marked "sending" for longer than this is treated as lost


def now():
    return datetime.now(timezone.utc)


def day_key(moment=None):
    return (moment or now()).astimezone(KSA).strftime("%Y-%m-%d")


def make_client(cfg):
    if cfg.emulator:
        return firestore.Client(project="demo-local")
    from google.oauth2 import service_account
    info = cfg.service_account
    creds = service_account.Credentials.from_service_account_info(info)
    return firestore.Client(project=info["project_id"], credentials=creds)


def _read(tx, ref):
    return next(iter(tx.get(ref)))


class Store:
    def __init__(self, client, vault):
        self.db, self.vault = client, vault
        self.invoices = client.collection("invoices")
        self.events = client.collection("events")
        self.stats_ref = client.collection("meta").document("stats")

    def _run(self, work):
        return firestore.transactional(work)(self.db.transaction())

    def _bump(self, tx, **changes):
        tx.set(self.stats_ref, {k: firestore.Increment(v) for k, v in changes.items()}, merge=True)

    def _bump_day(self, tx, **changes):
        ref = self.db.collection("daily").document(day_key())
        tx.set(ref, {k: firestore.Increment(v) for k, v in changes.items()}, merge=True)

    def _payer(self, inv, inv_id):
        """What the customer typed on the payment page: for the attempt that was paid, else the latest one."""
        payers = inv.get("payers") or {}
        if inv.get("paid_order"):
            key = "a" + inv["paid_order"].partition("x")[2]
        else:
            key = max(payers, key=lambda k: int(k[1:]) if k[1:].isdigit() else 0, default="")
        return self.vault.open(payers[key], f"payer:{inv_id}:{key}") if key in payers else {}

    def _load(self, snap):
        if not snap.exists:
            return None
        inv = snap.to_dict() or {}
        inv["id"] = snap.id
        # "fixed" is what the owner entered (never changed by the public page); "private" adds what the
        # paying customer typed for the fields the owner left empty.
        inv["fixed"] = self.vault.open(inv.get("enc", ""), snap.id)
        payer = self._payer(inv, snap.id)
        inv["private"] = {**payer, **{k: v for k, v in inv["fixed"].items() if v}}
        inv["overdue"] = inv.get("status") == "pending" and inv["expires_at"] <= now()
        inv["has_goods"] = bool(inv.get("goods"))
        override = self.vault.open(inv["deliver_to"], "deliver_to:" + snap.id) if inv.get("deliver_to") else {}
        inv["deliver_email"] = override.get("email") or inv["private"].get("email", "")
        sending = inv.get("delivery") or {}
        if sending.get("state") == "sending" and (now() - sending["at"]).total_seconds() >= SENDING_GRACE:
            inv["delivery"] = {**sending, "state": "failed", "error": "failed"}  # the worker sending it is gone
        return inv

    # ---- invoices
    def create_invoice(self, amount, private, days, customer=None, goods="", extra=None, minutes=None):
        inv_id = "".join(secrets.choice(ID_CHARS) for _ in range(12))
        created = now()
        doc = {
            "token": secrets.token_urlsafe(18), "amount": amount, "currency": "SAR", "status": "pending",
            "enc": self.vault.seal(private, inv_id), "created_at": created,
            "expires_at": created + (timedelta(minutes=minutes) if minutes else timedelta(days=days)),
            "attempts": 0, "views": 0, "declines": 0, "refunded_amount": 0.0,
        }
        if customer:
            doc["customer"] = customer
        if goods:
            doc["goods"] = self._seal_goods(goods, inv_id)
        doc.update(extra or {})

        def work(tx):
            tx.set(self.invoices.document(inv_id), doc)
            self._bump(tx, links=1, pending=1)
            self._bump_day(tx, created=1)

        self._run(work)
        return inv_id

    def get(self, inv_id):
        return self._load(self.invoices.document(inv_id).get()) if ID_RE.match(inv_id or "") else None

    def by_token(self, token):
        for snap in self.invoices.where("token", "==", token).limit(1).stream():
            return self._load(snap)
        return None

    def page(self, group=None, limit=10, before=None, scan=500, since=None):
        """One page of invoices, newest first, and the date to continue from for older ones (None at the end).
        With a group (see GROUPS) invoices are read in date order and kept when their state belongs to it, an open
        invoice past its end counting as expired. At most `scan` are read for one page, so a filter needs no extra
        database index and its cost stays bounded; `since` stops the reading at that date."""
        wanted, moment, rows, read, cursor = GROUPS.get(group), now(), [], 0, before
        while True:
            query = self.invoices
            if cursor:
                query = query.where("created_at", "<", cursor)
            size = limit + 1 if wanted is None else min(50, scan - read)
            snaps = list(query.order_by("created_at", direction=firestore.Query.DESCENDING).limit(size).stream())
            read += len(snaps)
            for snap in snaps:
                data = snap.to_dict() or {}
                if since and data["created_at"] < since:
                    return rows, None
                state = data.get("status")
                if state == "pending" and data["expires_at"] <= moment:
                    state = "expired"
                if wanted is None or state in wanted:
                    rows.append(self._load(snap))
                    if len(rows) > limit:
                        return rows[:limit], rows[limit - 1]["created_at"]
                cursor = data["created_at"]
            if len(snaps) < size:
                return rows, None
            if read >= scan:
                return rows, cursor

    def close(self, inv_id, new_status):
        """pending -> cancelled / expired. Returns False when the invoice is not open any more."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            snap = _read(tx, ref)
            if not snap.exists or (snap.to_dict() or {}).get("status") != "pending":
                return False
            tx.update(ref, {"status": new_status, "closed_at": now()})
            self._bump(tx, pending=-1, **{new_status: 1})
            return True

        return self._run(work)

    def add_view(self, inv):
        if inv.get("views", 0) >= 500:
            return

        def work(tx):
            tx.update(self.invoices.document(inv["id"]), {"views": firestore.Increment(1)})
            self._bump(tx, views=1)

        self._run(work)

    def note_error(self, inv_id, reason):
        """Keeps why the last payment attempt failed, unless a reason is already recorded for it."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            inv = _read(tx, ref).to_dict() or {}
            if inv.get("status") == "pending" and not inv.get("last_error"):
                tx.update(ref, {"last_error": reason})

        self._run(work)

    # ---- the order emailed to the customer after payment (a code, a link...), encrypted on its own
    def _seal_goods(self, text, inv_id):
        return self.vault.seal({"text": text}, "goods:" + inv_id)

    def goods(self, inv):
        return self.vault.open(inv["goods"], "goods:" + inv["id"]).get("text", "") if inv.get("goods") else ""

    def set_goods(self, inv_id, text):
        """Replaces the order of an open or paid invoice; clearing it is always possible.
        Returns False when the invoice is closed and the order cannot be changed."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            if text and (_read(tx, ref).to_dict() or {}).get("status") not in ("pending", "paid"):
                return False
            tx.update(ref, {"goods": self._seal_goods(text, inv_id) if text else firestore.DELETE_FIELD})
            return True

        return self._run(work)

    def claim_delivery(self, inv_id, again=False):
        """Marks the order of a paid invoice as being sent, so it is never sent twice by accident.
        Sent on its own only when the invoice was still open when it was paid (not after a cancel).
        Returns the invoice, or None when there is nothing to send now."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            inv, moment = self._load(_read(tx, ref)), now()
            if not inv or inv.get("status") != "paid" or not inv.get("goods"):
                return None
            # A shop order takes its code from stock at the moment it is paid, so it goes out even when paid late.
            if not again and inv.get("paid_from", "pending") != "pending" and not inv.get("product"):
                return None
            last = inv.get("delivery") or {}
            if last.get("state") == "sending" and (moment - last["at"]).total_seconds() < SENDING_GRACE:
                return None
            if last.get("state") == "sent" and not again:
                return None
            inv["delivery"] = {"state": "sending", "at": moment, "tries": last.get("tries", 0) + 1}
            tx.update(ref, {"delivery": inv["delivery"]})
            return inv

        return self._run(work)

    def finish_delivery(self, inv_id, tries, state, error=""):
        self.invoices.document(inv_id).update(
            {"delivery": {"state": state, "at": now(), "tries": tries, "error": error}})

    def claim_notice(self, inv_id):
        """True once per paid invoice: the owner hears about each payment exactly once."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            inv = _read(tx, ref).to_dict() or {}
            if inv.get("status") != "paid" or inv.get("notice"):
                return False
            tx.update(ref, {"notice": {"state": "sending", "at": now()}})
            return True

        return self._run(work)

    def finish_notice(self, inv_id, error=""):
        self.invoices.document(inv_id).update(
            {"notice": {"state": "failed" if error else "sent", "at": now(), "error": error}})

    def set_delivery_email(self, inv_id, email):
        """The owner corrects where the order goes (kept encrypted, like everything about the customer)."""
        self.invoices.document(inv_id).update({"deliver_to": self.vault.seal({"email": email}, "deliver_to:" + inv_id)})

    def set_gift(self, inv_id, email, sender, note):
        """The order goes to a friend: the codes are sent to their email only. Kept encrypted."""
        self.invoices.document(inv_id).update({
            "deliver_to": self.vault.seal({"email": email}, "deliver_to:" + inv_id),
            "gift": self.vault.seal({"from": sender, "note": note}, "gift:" + inv_id)})

    def gift(self, inv):
        """{from, note} of a gift order, or {}."""
        return self.vault.open(inv["gift"], "gift:" + inv["id"]) if inv.get("gift") else {}

    def set_customer(self, inv_id, customer):
        self.invoices.document(inv_id).update({"customer": customer})

    def set_buyer(self, inv_id, buyer):
        """The account fingerprint of the email this invoice was paid with (accounts.py finds its orders by it)."""
        self.invoices.document(inv_id).update({"buyer": buyer})

    def for_buyer(self, buyer, limit=300):
        rows = [self._load(snap) for snap in self.invoices.where("buyer", "==", buyer).limit(limit).stream()]
        return sorted(rows, key=lambda inv: inv["created_at"], reverse=True)

    def for_customer(self, customer, limit=300):
        rows = [self._load(snap) for snap in self.invoices.where("customer", "==", customer).limit(limit).stream()]
        return sorted(rows, key=lambda inv: inv["created_at"], reverse=True)

    def note_checkout(self, inv_id, order_id, payment_id):
        """Remembers the latest gateway payment id of an invoice so its status can be asked for later."""
        self.invoices.document(inv_id).update({"checkout": {"order": order_id, "id": payment_id}})

    def start_attempt(self, inv_id, payer=None, agreed=None):
        """Reserves a payment attempt. Returns its unique gateway order id, or None when not allowed.
        What the customer typed is kept for this attempt only, so a later attempt can correct it. The version of
        the policies the customer agreed to is kept with the time, for any later dispute."""
        ref = self.invoices.document(inv_id)

        def work(tx):
            snap = _read(tx, ref)
            inv, moment = snap.to_dict() or {}, now()
            last = inv.get("last_attempt_at")
            if (not snap.exists or inv.get("status") != "pending" or inv["expires_at"] <= moment
                    or inv.get("attempts", 0) >= MAX_ATTEMPTS
                    or (last and (moment - last).total_seconds() < 3)):
                return None
            number = inv.get("attempts", 0) + 1
            changes = {"attempts": number, "last_attempt_at": moment, "last_error": ""}
            if agreed:
                changes["agreed"] = {"version": agreed, "at": moment}
            if payer:
                key = f"a{number}"
                changes["payers"] = {**(inv.get("payers") or {}), key: self.vault.seal(payer, f"payer:{inv_id}:{key}")}
            tx.update(ref, changes)
            self._bump(tx, attempts=1)
            return f"{inv_id}x{number}"

        return self._run(work)

    # ---- gateway notifications
    def apply_webhook(self, ev):
        """Applies one verified EdfaPay notification exactly once. Returns what was done."""
        inv_id = ev["order_id"].partition("x")[0]
        if not ID_RE.match(inv_id):
            return "unknown_order"
        ref = self.invoices.document(inv_id)
        key = re.sub(r"[^A-Za-z0-9]", "", f"{ev['txn']}{ev['type']}{ev['status']}")[:150] or "none"
        ev_ref = self.events.document(f"{inv_id}_{key}")

        def work(tx):
            snap = _read(tx, ref)
            seen = _read(tx, ev_ref)
            if not snap.exists:
                return "unknown_order"
            if seen.exists:
                return "duplicate"
            inv, moment = snap.to_dict() or {}, now()
            status, kind, amount = inv.get("status"), ev["type"], ev["amount"]
            changes, counters, daily, note = {}, {}, {}, ""
            if ev["status"] == "Approved" and kind in ("Purchase", "Capture"):
                if abs(amount - inv["amount"]) > 0.009:
                    note = "amount_mismatch"
                elif status in ("paid", "refunded"):
                    note = "already_paid"
                else:
                    # A payment that lands after the link expired or was cancelled is recorded, but its order is
                    # not emailed on its own: the owner may have reused that code by then.
                    before = "expired" if status == "pending" and inv["expires_at"] <= moment else status
                    changes = {"status": "paid", "paid_at": moment, "txn": ev["txn"], "rrn": ev["rrn"],
                               "scheme": ev["scheme"], "last_error": "", "paid_order": ev["order_id"],
                               "paid_from": before}
                    counters = {status: -1, "paid": 1, "paid_amount": amount}
                    daily = {"paid": 1, "amount": amount}
            elif ev["status"] == "Approved" and kind == "Refund":
                if status in ("paid", "refunded"):
                    total = round(inv.get("refunded_amount", 0) + amount, 2)
                    changes, counters = {"refunded_amount": total}, {"refunded_amount": amount}
                    if status == "paid" and total >= inv["amount"] - 0.009:
                        changes["status"] = "refunded"
                        counters.update(paid=-1, refunded=1)
                else:
                    note = "refund_without_payment"
            elif ev["status"] == "Declined" and kind in ("Purchase", "Authorization") and status == "pending":
                changes = {"last_error": ev["reason"] or "DECLINED", "txn": ev["txn"],
                           "declines": firestore.Increment(1)}
                counters = {"declines": 1}
            tx.set(ev_ref, {"invoice": inv_id, "at": moment, "note": note, **ev})
            if changes:
                tx.update(ref, changes)
            if counters:
                self._bump(tx, **counters)
            if daily:
                self._bump_day(tx, **daily)
            return note or ("applied" if changes else "recorded")

        return self._run(work)

    def events_for(self, inv_id):
        rows = [snap.to_dict() or {} for snap in self.events.where("invoice", "==", inv_id).stream()]
        return sorted(rows, key=lambda row: row["at"], reverse=True)

    # ---- statistics
    def stats(self):
        data = self.stats_ref.get().to_dict() or {}
        return {key: data.get(key, 0) for key in STAT_KEYS}

    def daily(self, days=14):
        keys = [day_key(now() - timedelta(days=back)) for back in range(days - 1, -1, -1)]
        col = self.db.collection("daily")
        found = {snap.id: snap.to_dict() or {} for snap in self.db.get_all([col.document(k) for k in keys])}
        return [{"day": k, **{f: found.get(k, {}).get(f, 0) for f in ("created", "paid", "amount")}} for k in keys]

    # ---- login lock
    def lock_left(self, *keys):
        left = 0
        for key in keys:
            data = self.db.collection("logins").document(key).get().to_dict() or {}
            left = max(left, data.get("until", 0) - time.time())
        return int(left) + 1 if left > 0 else 0

    def login_failed(self, key, limit):
        ref = self.db.collection("logins").document(key)

        def work(tx):
            moment, data = time.time(), _read(tx, ref).to_dict() or {}
            if moment - data.get("first", 0) > LOCK_SECONDS:
                data = {"first": moment, "fails": 0}
            data["fails"] = data.get("fails", 0) + 1
            if data["fails"] >= limit:
                data = {"first": moment, "fails": 0, "until": moment + LOCK_SECONDS}
            tx.set(ref, data)

        self._run(work)

    def login_ok(self, key):
        """Clears this address' failures and returns the time of the previous successful login."""
        self.db.collection("logins").document(key).delete()
        ref = self.db.collection("meta").document("access")
        previous = (ref.get().to_dict() or {}).get("at")
        ref.set({"at": now()})
        return previous
