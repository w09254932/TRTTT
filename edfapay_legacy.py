"""EdfaPay checkout for accounts that work with a merchant ID and a merchant password
(api.edfapay.com/payment/...). A payment counts only when EdfaPay's status API says it is settled."""
import hashlib
import ipaddress
from urllib.parse import urlparse

import requests

from edfapay import GatewayError

# The checkout API requires a billing address; invoices do not collect one, so these fixed values are sent.
PAYER_PLACE = {"payer_country": "SA", "payer_city": "Riyadh", "payer_address": "Riyadh", "payer_zip": "12221"}


def _digest(text):
    """sha1(md5(UPPERCASE(text))) as EdfaPay defines its request signatures."""
    return hashlib.sha1(hashlib.md5(text.upper().encode()).hexdigest().encode()).hexdigest()


class EdfaPayLegacy:
    legacy = True

    def __init__(self, base_url, merchant_id, password, fallback_ip="127.0.0.1", timeout=25):
        self.base_url = base_url
        self.merchant_id = merchant_id
        self.password = password
        self.fallback_ip = fallback_ip
        self.timeout = timeout
        self.http = requests.Session()

    def _post(self, path, **kwargs):
        try:
            resp = self.http.post(self.base_url + path, timeout=self.timeout, **kwargs)
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise GatewayError(f"no usable answer from gateway: {type(exc).__name__}") from exc
        return resp.status_code, body if isinstance(body, dict) else {}

    def initiate(self, order_id, amount, customer, success_url, failure_url=None, payer_ip=None):
        """Opens a checkout session and returns the URL the customer must be sent to."""
        amount_text = f"{amount:.2f}"
        description = "Invoice " + order_id.partition("x")[0]
        first, _, last = customer["name"].partition(" ")
        try:
            ip = str(ipaddress.IPv4Address(payer_ip or ""))
        except ValueError:
            ip = self.fallback_ip
        fields = {
            "action": "SALE",
            "edfa_merchant_id": self.merchant_id,
            "order_id": order_id,
            "order_amount": amount_text,
            "order_currency": "SAR",
            "order_description": description,
            "req_token": "N",
            "payer_first_name": first,
            "payer_last_name": last or first,
            **PAYER_PLACE,
            "payer_email": customer["email"],
            "payer_phone": customer["phone"].lstrip("+"),
            "payer_ip": ip,
            "term_url_3ds": success_url.split("?")[0],
            "auth": "N",
            "recurring_init": "N",
            "hash": _digest(order_id + amount_text + "SAR" + description + self.password),
        }
        code, body = self._post("/payment/initiate", files={name: (None, value) for name, value in fields.items()})
        url = str(body.get("redirect_url") or "")
        parts = urlparse(url)
        host = parts.hostname or ""
        if parts.scheme != "https" or not (host == "edfapay.com" or host.endswith(".edfapay.com")):
            reason = body.get("error_message") or body.get("message") or body.get("errors") or sorted(body)
            raise GatewayError(f"HTTP {code}: {str(reason)[:300]}")
        return url

    def status(self, order_id, payment_id):
        """Asks EdfaPay about one payment. Returns its record (with a "status" key) or None."""
        answer = None
        for key in ("gway_Payment_id", "gway_Payment_Id"):
            _, body = self._post("/payment/status", json={
                "order_id": order_id,
                "merchant_id": self.merchant_id,
                key: payment_id,
                "hash": _digest(payment_id + self.password),
            })
            record = body.get("responseBody") if isinstance(body.get("responseBody"), dict) else body
            if record.get("status"):
                answer = record
                if str(record["status"]).lower() == "settled":
                    break
        return answer
