"""Turns the payment gateway's decline reason into a sentence the customer can act on.
The codes and wordings follow EdfaPay's "Transaction Decline Codes" page."""
import re

GENERAL = "رفض البنك عملية الدفع. تواصل مع بنكك أو استخدم بطاقة أخرى."
GENERIC = {"failure", "failed", "fail", "declined", "decline", "txn_failure", "error", "rejected",
           "رفضت البوابة العملية"}
KNOWN = (
    ((r"insufficient", r"not sufficient", r"\b51\b"),
     "الرصيد في البطاقة لا يكفي. اشحن الرصيد أو استخدم بطاقة أخرى."),
    ((r"expired", r"\b54\b"),
     "البطاقة منتهية الصلاحية. استخدم بطاقة سارية."),
    ((r"cvv", r"cvc", r"security code"),
     "رمز الأمان (CVV) خلف البطاقة غير صحيح. تأكد منه وحاول مرة أخرى."),
    ((r"invalid card", r"card number", r"\b14\b"),
     "رقم البطاقة غير صحيح. تأكد من الرقم وحاول مرة أخرى."),
    ((r"authenticat", r"3-?d", r"\botp\b", r"verification"),
     "لم يكتمل التحقق برمز البنك. حاول مرة أخرى وأدخل الرمز الذي يصلك برسالة نصية."),
    ((r"\bpin\b", r"\b55\b", r"\b75\b"),
     "الرقم السري غير صحيح أو تجاوزت عدد المحاولات. تواصل مع بنكك."),
    ((r"exceed", r"limit", r"\b61\b", r"\b65\b"),
     "تجاوزت الحد المسموح للبطاقة. ارفع الحد من تطبيق البنك أو استخدم بطاقة أخرى."),
    ((r"not permitted", r"not allowed", r"restricted", r"\b57\b", r"\b58\b", r"\b62\b"),
     "البطاقة غير مفعّلة للشراء عبر الإنترنت. فعّلها من تطبيق البنك أو استخدم بطاقة أخرى."),
    ((r"lost", r"stolen", r"pick", r"\b04\b", r"\b41\b", r"\b43\b"),
     "البطاقة موقوفة. تواصل مع بنكك."),
    ((r"duplicate", r"\b94\b"),
     "سُجّلت هذه العملية كعملية مكررة. راجع كشف حسابك قبل إعادة المحاولة."),
    ((r"cancel",),
     "أُلغيت عملية الدفع قبل إتمامها."),
    ((r"unavailable", r"not available", r"inoperative", r"unable to route", r"time[d ]?out", r"system",
      r"\b80\b", r"\b91\b", r"\b92\b", r"\b96\b"),
     "تعذر الاتصال بالبنك الآن. حاول بعد دقائق."),
    ((r"do not honou?r", r"refer to", r"\b01\b", r"\b05\b"),
     "رفض البنك العملية دون ذكر السبب. تواصل مع بنكك أو استخدم بطاقة أخرى."),
)


def specific(reason):
    """False for an empty reason or a bare "declined" that says nothing about the cause."""
    return (reason or "").strip().lower() not in GENERIC | {""}


def explain(reason):
    """An Arabic sentence for the customer; unknown wordings are passed on as the bank sent them."""
    text = " ".join((reason or "").split())
    if not text:
        return ""
    if not specific(text):
        return GENERAL
    low = text.lower()
    for patterns, sentence in KNOWN:
        if any(re.search(pattern, low) for pattern in patterns):
            return sentence
    return "السبب كما ورد من البنك: " + text
