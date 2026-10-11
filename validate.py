"""Input cleaning. Each cleaner returns (value, error_message)."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫", "01234567890123456789.")
# An address email can actually be sent to: the syntax browsers accept for type=email, with a dot in the domain.
MAIL_ADDRESS = re.compile(r"^[A-Za-z0-9.#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                          r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
GOODS_LIMIT = 2000


def western(value):
    """Arabic and Persian digits become 0-9."""
    return (value or "").translate(_DIGITS)


def clean_text(value, limit):
    return " ".join((value or "").split())[:limit]


def clean_name(value):
    value = clean_text(value, 80)
    return (value, None) if len(value) >= 2 else (None, "اكتب الاسم كاملاً.")


def clean_email(value):
    value = (value or "").strip().lower()
    if len(value) <= 120 and MAIL_ADDRESS.match(value):
        return value, None
    return None, "البريد الإلكتروني غير صحيح."


def clean_goods(value):
    """What the customer receives by email after paying (a code, a link, instructions). Line breaks are kept."""
    lines = (value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    text = "\n".join(line.rstrip() for line in lines).strip("\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    if len(text) > GOODS_LIMIT:
        return None, f"ما يُرسل للعميل يجب ألا يزيد على {GOODS_LIMIT} حرف."
    return text, None


def clean_phone(value):
    value = re.sub(r"[\s\-()]", "", western(value))
    if value.startswith("00"):
        value = "+" + value[2:]
    if re.fullmatch(r"05\d{8}", value):
        value = "+966" + value[1:]
    elif re.fullmatch(r"5\d{8}", value):
        value = "+966" + value
    elif not value.startswith("+"):
        value = "+" + value
    if re.fullmatch(r"\+\d{8,15}", value):
        return value, None
    return None, "رقم الجوال غير صحيح. مثال: 0512345678"


def clean_amount(value):
    try:
        amount = Decimal(western(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None, "اكتب المبلغ بالأرقام."
    if amount.is_nan() or not (Decimal("1") <= amount <= Decimal("1000000")):
        return None, "المبلغ يجب أن يكون بين 1 و 1,000,000 ريال."
    return float(amount), None
