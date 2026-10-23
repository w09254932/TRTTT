"""What the API-key EdfaPay system is told about an order, and what it tells back about a payment.

checkout_items(): the cart's lines for the checkout page (its Template 1 shows them beside the card form).
describe(): EdfaPay's record of one transaction as rows for the owner's invoice page."""
from products import lines_of


def checkout_items(inv, store=""):
    """[{sku, description, unitCost, quantity, ...}] for a shop order whose lines add up to its amount, else [].
    EdfaPay's page shows `sku` as the line's title and `description` under it, so the name goes in sku."""
    lines = [line for line in lines_of(inv) if line.get("qty")]
    if not lines or not all(line.get("name") and line.get("price") for line in lines):
        return []      # an order from before the cart only knows its product id
    items = []
    for line in lines:
        price, qty = round(float(line["price"]), 2), int(line["qty"])
        total = round(price * qty, 2)
        items.append({"sku": str(line["name"])[:60], "description": str(store or line["name"])[:120], "unitCost": price,
                      "quantity": qty, "netTotal": total, "discountRate": 0, "discountAmount": 0,
                      "taxRate": 0, "taxTotal": 0, "total": total})
    if abs(sum(item["total"] for item in items) - float(inv["amount"])) > 0.009:
        return []      # never show a list that disagrees with what is charged
    return items


WORDS = {
    "SUCCESS": "ناجحة", "FAILED": "فاشلة", "PENDING": "قيد التنفيذ", "TIME_OUT": "انتهت المهلة",
    "IN_SAF": "معلّقة لدى البنك", "COMMUNICATION_FAILURE": "تعذّر الاتصال بالبنك",
    "APPROVED": "مقبولة", "DECLINED": "مرفوضة", "REDIRECT": "بانتظار العميل في صفحة الدفع",
    "PHYSICAL_CARD": "بطاقة", "APPLE_PAY": "Apple Pay", "GOOGLE_PAY": "Google Pay", "STC_PAY": "STC Pay",
    "SAMSUNG_PAY": "Samsung Pay", "MADA_PAY": "مدى Pay",
    "NONE": "لا يوجد", "NOT_SETTLED": "لم تُحوّل لحسابك بعد", "SETTLED": "حُوّلت لحسابك",
    "UNSETTLED": "لم تُسوَّ بعد", "RECONCILED": "تمت التسوية",
    "PURCHASE": "شراء", "REFUND": "استرداد", "VOID": "إلغاء", "AUTHORIZATION": "حجز مبلغ", "CAPTURE": "تحصيل",
}
ROWS = (
    ("transactionStatus", "حالة العملية"), ("paymentStatus", "حالة الدفع"), ("declineReason", "سبب الرفض"),
    ("amount", "المبلغ"), ("transactionType", "النوع"), ("cardScheme", "شبكة البطاقة"),
    ("cardChannel", "طريقة الدفع"), ("pan", "رقم البطاقة"), ("cardHolderName", "اسم حامل البطاقة"),
    ("rrn", "المرجع البنكي"), ("refundStatus", "الاسترداد"), ("totalRefundAmount", "المبلغ المسترد"),
    ("settlementStatus", "التحويل لحسابك"), ("reconciliationStatus", "التسوية"),
    ("createdAt", "بدأت"), ("finishedAt", "انتهت"), ("orderId", "رقم الطلب عند البوابة"),
)


def describe(record):
    """[(label, value, raw)] for the fields EdfaPay sent; raw is the gateway's own word when it was translated."""
    rows = []
    for key, label in ROWS:
        value = record.get(key) if isinstance(record, dict) else None
        if value in (None, "", [], {}):
            continue
        text = str(value)[:120]
        if key in ("createdAt", "finishedAt"):
            text = text.replace("T", " ")[:19]
        word = WORDS.get(text.upper())
        rows.append((label, word or text, text if word and word != text else ""))
    return rows
