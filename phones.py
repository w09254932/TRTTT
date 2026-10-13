"""Phone numbers: the country code box, reading what people type, and showing a number with its
country. Numbers are kept in international form (E.164), e.g. +201001234567."""
import json
import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType, ValidationResult

from validate import western

DEFAULT = "SA"
# Arabic names (CLDR) of every country the phone library knows, a few shortened.
NAMES = {
    "AC": "جزيرة أسينشيون", "AD": "أندورا", "AE": "الإمارات", "AF": "أفغانستان",
    "AG": "أنتيغوا وبربودا", "AI": "أنغويلا", "AL": "ألبانيا", "AM": "أرمينيا", "AO": "أنغولا",
    "AR": "الأرجنتين", "AS": "ساموا الأمريكية", "AT": "النمسا", "AU": "أستراليا", "AW": "أروبا",
    "AX": "جزر آلاند", "AZ": "أذربيجان", "BA": "البوسنة والهرسك", "BB": "بربادوس", "BD": "بنغلاديش",
    "BE": "بلجيكا", "BF": "بوركينا فاسو", "BG": "بلغاريا", "BH": "البحرين", "BI": "بوروندي",
    "BJ": "بنين", "BL": "سان بارتليمي", "BM": "برمودا", "BN": "بروناي", "BO": "بوليفيا",
    "BQ": "هولندا الكاريبية", "BR": "البرازيل", "BS": "جزر البهاما", "BT": "بوتان", "BW": "بوتسوانا",
    "BY": "بيلاروس", "BZ": "بليز", "CA": "كندا", "CC": "جزر كوكوس", "CD": "الكونغو الديمقراطية",
    "CF": "جمهورية أفريقيا الوسطى", "CG": "الكونغو", "CH": "سويسرا", "CI": "ساحل العاج",
    "CK": "جزر كوك", "CL": "تشيلي", "CM": "الكاميرون", "CN": "الصين", "CO": "كولومبيا",
    "CR": "كوستاريكا", "CU": "كوبا", "CV": "الرأس الأخضر", "CW": "كوراساو", "CX": "جزيرة كريسماس",
    "CY": "قبرص", "CZ": "التشيك", "DE": "ألمانيا", "DJ": "جيبوتي", "DK": "الدانمرك", "DM": "دومينيكا",
    "DO": "جمهورية الدومينيكان", "DZ": "الجزائر", "EC": "الإكوادور", "EE": "إستونيا", "EG": "مصر",
    "EH": "الصحراء الغربية", "ER": "إريتريا", "ES": "إسبانيا", "ET": "إثيوبيا", "FI": "فنلندا",
    "FJ": "فيجي", "FK": "جزر فوكلاند", "FM": "ميكرونيزيا", "FO": "جزر فارو", "FR": "فرنسا",
    "GA": "الغابون", "GB": "المملكة المتحدة", "GD": "غرينادا", "GE": "جورجيا", "GF": "غويانا الفرنسية",
    "GG": "غيرنزي", "GH": "غانا", "GI": "جبل طارق", "GL": "غرينلاند", "GM": "غامبيا", "GN": "غينيا",
    "GP": "غوادلوب", "GQ": "غينيا الاستوائية", "GR": "اليونان", "GT": "غواتيمالا", "GU": "غوام",
    "GW": "غينيا بيساو", "GY": "غيانا", "HK": "هونغ كونغ", "HN": "هندوراس", "HR": "كرواتيا",
    "HT": "هايتي", "HU": "هنغاريا", "ID": "إندونيسيا", "IE": "أيرلندا", "IL": "إسرائيل",
    "IM": "جزيرة مان", "IN": "الهند", "IO": "إقليم المحيط الهندي البريطاني", "IQ": "العراق",
    "IR": "إيران", "IS": "آيسلندا", "IT": "إيطاليا", "JE": "جيرسي", "JM": "جامايكا", "JO": "الأردن",
    "JP": "اليابان", "KE": "كينيا", "KG": "قيرغيزستان", "KH": "كمبوديا", "KI": "كيريباتي",
    "KM": "جزر القمر", "KN": "سانت كيتس ونيفيس", "KP": "كوريا الشمالية", "KR": "كوريا الجنوبية",
    "KW": "الكويت", "KY": "جزر كايمان", "KZ": "كازاخستان", "LA": "لاوس", "LB": "لبنان",
    "LC": "سانت لوسيا", "LI": "ليختنشتاين", "LK": "سريلانكا", "LR": "ليبيريا", "LS": "ليسوتو",
    "LT": "ليتوانيا", "LU": "لوكسمبورغ", "LV": "لاتفيا", "LY": "ليبيا", "MA": "المغرب", "MC": "موناكو",
    "MD": "مولدوفا", "ME": "الجبل الأسود", "MF": "سان مارتن", "MG": "مدغشقر", "MH": "جزر مارشال",
    "MK": "مقدونيا الشمالية", "ML": "مالي", "MM": "ميانمار", "MN": "منغوليا", "MO": "ماكاو",
    "MP": "جزر ماريانا الشمالية", "MQ": "جزر المارتينيك", "MR": "موريتانيا", "MS": "مونتسرات",
    "MT": "مالطا", "MU": "موريشيوس", "MV": "جزر المالديف", "MW": "ملاوي", "MX": "المكسيك",
    "MY": "ماليزيا", "MZ": "موزمبيق", "NA": "ناميبيا", "NC": "كاليدونيا الجديدة", "NE": "النيجر",
    "NF": "جزيرة نورفولك", "NG": "نيجيريا", "NI": "نيكاراغوا", "NL": "هولندا", "NO": "النرويج",
    "NP": "نيبال", "NR": "ناورو", "NU": "نيوي", "NZ": "نيوزيلندا", "OM": "عُمان", "PA": "بنما",
    "PE": "بيرو", "PF": "بولينيزيا الفرنسية", "PG": "بابوا غينيا الجديدة", "PH": "الفلبين",
    "PK": "باكستان", "PL": "بولندا", "PM": "سان بيير ومكويلون", "PR": "بورتوريكو", "PS": "فلسطين",
    "PT": "البرتغال", "PW": "بالاو", "PY": "باراغواي", "QA": "قطر", "RE": "روينيون", "RO": "رومانيا",
    "RS": "صربيا", "RU": "روسيا", "RW": "رواندا", "SA": "السعودية", "SB": "جزر سليمان", "SC": "سيشل",
    "SD": "السودان", "SE": "السويد", "SG": "سنغافورة", "SH": "سانت هيلينا", "SI": "سلوفينيا",
    "SJ": "سفالبارد وجان ماين", "SK": "سلوفاكيا", "SL": "سيراليون", "SM": "سان مارينو",
    "SN": "السنغال", "SO": "الصومال", "SR": "سورينام", "SS": "جنوب السودان",
    "ST": "ساو تومي وبرينسيبي", "SV": "السلفادور", "SX": "سانت مارتن", "SY": "سوريا", "SZ": "إسواتيني",
    "TA": "تريستان دا كونا", "TC": "جزر توركس وكايكوس", "TD": "تشاد", "TG": "توغو", "TH": "تايلاند",
    "TJ": "طاجيكستان", "TK": "توكيلاو", "TL": "تيمور الشرقية", "TM": "تركمانستان", "TN": "تونس",
    "TO": "تونغا", "TR": "تركيا", "TT": "ترينيداد وتوباغو", "TV": "توفالو", "TW": "تايوان",
    "TZ": "تنزانيا", "UA": "أوكرانيا", "UG": "أوغندا", "US": "الولايات المتحدة", "UY": "أورغواي",
    "UZ": "أوزبكستان", "VA": "الفاتيكان", "VC": "سانت فنسنت وجزر غرينادين", "VE": "فنزويلا",
    "VG": "جزر فيرجن البريطانية", "VI": "جزر فيرجن الأمريكية", "VN": "فيتنام", "VU": "فانواتو",
    "WF": "جزر والس وفوتونا", "WS": "ساموا", "XK": "كوسوفو", "YE": "اليمن", "YT": "مايوت",
    "ZA": "جنوب أفريقيا", "ZM": "زامبيا", "ZW": "زيمبابوي"
}

LRM = "\u200e"  # keeps "+966" reading left to right inside Arabic text
BAD = "رقم الجوال غير صحيح."
_JUNK = re.compile(r"[\s\-().\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def flag(iso):
    return "".join(chr(0x1F1E6 + ord(letter) - 65) for letter in iso) if iso else ""


def _example(iso):
    number = phonenumbers.example_number_for_type(iso, PhoneNumberType.MOBILE)
    return phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL) if number else ""


COUNTRIES = {iso: (phonenumbers.country_code_for_region(iso), name) for iso, name in NAMES.items()
             if phonenumbers.country_code_for_region(iso)}
EXAMPLES = {iso: _example(iso) for iso in COUNTRIES}
# The code people type and the country it shows. Where countries share a code (+1, +7, +44...) the main one
# is shown; the number itself still decides, so a Canadian +1 number is kept as Canadian.
CODES = {str(dial): phonenumbers.region_code_for_country_code(dial) for dial, _ in COUNTRIES.values()}
# What the page needs to show the country while the code is typed: code -> [country, name, example],
# as JSON that is safe inside a <script type="application/json"> block.
CODES_JSON = json.dumps({code: [iso, NAMES[iso], EXAMPLES[iso]] for code, iso in sorted(CODES.items())},
                        ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def code_of(value):
    """The country code typed next to the number, as (digits, country): ("966", "SA"), ("20", "EG"), or
    ("999", None) for a code no country has. Empty means Saudi Arabia. Pages from before the code box sent
    the country's letters ("EG"); those still work."""
    text = western(str(value or "")).strip().lstrip("+").upper()
    if text in COUNTRIES:
        return str(COUNTRIES[text][0]), text
    digits = re.sub(r"\D", "", text).lstrip("0")[:4]
    if not digits:
        return str(COUNTRIES[DEFAULT][0]), DEFAULT
    return digits, CODES.get(digits)


def label(value):
    """What shows under the boxes for a typed code: the country's name."""
    _, iso = code_of(value)
    return NAMES[iso] if iso else "رمز دولة غير معروف"


def _read(text, region, good_enough):
    try:
        number = phonenumbers.parse(text, region)
    except NumberParseException:
        return None
    return number if good_enough(number) else None


def _any_length(number):
    """Has as many digits as some number of its country (a full number, not a local one)."""
    return phonenumbers.is_possible_number_with_reason(number) == ValidationResult.IS_POSSIBLE


def _mobile_length(number):
    """Has as many digits as a mobile number of its country. Lengths only: a new range still passes."""
    reason = phonenumbers.is_possible_number_for_type_with_reason(number, PhoneNumberType.MOBILE)
    return reason == ValidationResult.IS_POSSIBLE


def clean_phone(value, code=None):
    """Returns (number in E.164, None) or (None, error message). `code` is the country code typed next to
    the number. A number typed with its own code keeps that code: +20..., 0020..., or 20... when it cannot
    be a number of the typed code's country."""
    digits, region = code_of(code)
    text = _JUNK.sub("", western(value)).replace("\uff0b", "+")
    if text.startswith("00"):
        text = "+" + text[2:]
    if not re.fullmatch(r"\+?\d+", text):
        return None, BAD + " اكتبه بالأرقام فقط."
    if not 5 <= len(text.lstrip("+")) <= 17:
        return None, BAD + " تأكد من عدد الأرقام."
    if text.startswith("+"):
        number = _read(text, None, _any_length)
    else:
        number = ((region and _read(text, region, _mobile_length))
                  or _read("+" + text, None, phonenumbers.is_valid_number))
    phone = phonenumbers.format_number(number, PhoneNumberFormat.E164) if number else ""
    if region_of(phone):
        return phone, None
    if text.startswith("+"):
        return None, BAD + " تأكد من رمز الدولة وعدد الأرقام."
    if not region:
        return None, f"رمز الدولة {LRM}+{digits} غير صحيح. اكتب رمز دولتك، مثل 966 للسعودية."
    return None, f"رقم الجوال لا يناسب {NAMES[region]} ({LRM}+{digits}). تأكد من رمز الدولة ومن الرقم."


def chat_number(value):
    """WHATSAPP_NUMBER as wa.me wants it: digits with the country code, no + and no leading zeros. Takes
    966501234567, +966 50 123 4567, 00966501234567 or a Saudi 0501234567. Returns "" when it is not a number."""
    if not (value or "").strip():
        return ""
    phone, _ = clean_phone(value, "966")
    return phone.lstrip("+") if phone else ""


def _parsed(phone):
    try:
        return phonenumbers.parse(phone or "", None)
    except NumberParseException:
        return None


def region_of(phone):
    """Two-letter country of a stored number, e.g. "EG" for +201001234567, or None."""
    number = _parsed(phone)
    if not number:
        return None
    iso = (phonenumbers.region_code_for_number(number)
           or phonenumbers.region_code_for_country_code(number.country_code))
    return iso if iso in COUNTRIES else None


def parts(phone):
    """(country code, the rest of the number), to put a stored number back into the two boxes."""
    number = _parsed(phone)
    if not number or not region_of(phone):
        return str(COUNTRIES[DEFAULT][0]), phone or ""
    return str(number.country_code), phonenumbers.national_significant_number(number)


def pretty(phone):
    """+966 55 123 4567 rather than +966551234567."""
    number = _parsed(phone)
    return phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL) if number else (phone or "")


def country(phone):
    """The flag and the name of the number's country, or ""."""
    iso = region_of(phone)
    return f"{flag(iso)} {NAMES[iso]}" if iso else ""
