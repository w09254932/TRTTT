"""The store's terms, privacy and refund pages, and the agreement every payment needs: the customer ticks both boxes
on the pay page, the server checks them, and the version agreed to is kept on the invoice with the time."""
from flask import Blueprint, abort, render_template

from products import ORDER_MINUTES
from security import too_many

VERSION = "2026-10-17"                # change it whenever the wording of a policy changes
UPDATED = "17 أكتوبر 2026"
AGREE = ("agree_terms", "agree_refund")
NOT_AGREED = "للمتابعة إلى الدفع وافق على الشروط والأحكام وسياسة الخصوصية، وعلى سياسة الاسترداد والإلغاء."
PAGES = (("terms", "الشروط والأحكام"), ("privacy", "الخصوصية وملفات تعريف الارتباط"),
         ("refund", "سياسة الاسترداد والإلغاء"))

policies = Blueprint("policies", __name__)


@policies.before_request
def limit():
    if too_many("public", 120):
        abort(429)


def agreed(form):
    """Both boxes on the pay page were ticked."""
    return all(form.get(name) == "1" for name in AGREE)


def _page(name):
    return render_template(f"policy_{name}.html", page=name, title=dict(PAGES)[name], pages=PAGES,
                           updated=UPDATED, minutes=ORDER_MINUTES)


@policies.get("/terms")
def terms():
    return _page("terms")


@policies.get("/privacy")
def privacy():
    return _page("privacy")


@policies.get("/refund")
def refund():
    return _page("refund")
