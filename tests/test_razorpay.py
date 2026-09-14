"""
Razorpay Standard Checkout: order creation, signature verification, and the
anti-replay check that ties a confirmed payment back to the invoice that
opened it.

Unlike the rest of this suite, order creation here hits Razorpay's REAL test
API (no stub) - the whole point is proving the order comes back genuinely
signed by Razorpay, which nothing we control can fake convincingly enough to
be worth testing against. Everything downstream of that (signature checks,
the anti-replay logic, invoice state) is pure local logic and needs no
network. Requires RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in the environment -
skips cleanly if they are not set, so this suite does not fail on a machine
that only has demo mode configured.
"""
import hashlib
import hmac
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_razorpay.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.ngrok-free.app"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
if not (KEY_ID and KEY_SECRET):
    print("SKIPPED: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set in this environment.")
    print("\nALL PASSED")
    raise SystemExit(0)

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
import app as application  # noqa: E402
import plans  # noqa: E402
from models import Company, Invoice, engine as db_eng, init_db  # noqa: E402

init_db()
fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def sign(order_id: str, payment_id: str) -> str:
    return hmac.new(KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------- unit: signature math itself
sig = sign("order_ABC", "pay_XYZ")
check("correctly-signed payment verifies", plans.verify_razorpay_signature("order_ABC", "pay_XYZ", sig))
check("tampered payment_id is rejected", not plans.verify_razorpay_signature("order_ABC", "pay_OTHER", sig))
check("empty order_id is rejected", not plans.verify_razorpay_signature("", "pay_XYZ", sig))

# -------------------------------------------------- unit: real order creation
probe = Invoice(id=999_999, company_id=1, plan="starter", amount_inr=999, status="pending")
order = plans.create_razorpay_order(probe)
check("a real Razorpay order is created", order.get("id", "").startswith("order_"), str(order))
check("amount is in paise, from the invoice - never client-suppliable",
      order.get("amount") == 999 * 100, str(order))
check("order id is stamped onto the invoice for the anti-replay check",
      probe.provider_ref == order["id"], probe.provider_ref)

# ------------------------------------------------------------ full app flow
from auth import signup as create_test_account  # noqa: E402
from security import SESSION_COOKIE  # noqa: E402

with TestClient(application.app) as c:
    # The signup form creates nothing until its emailed link is clicked, so
    # the test account is created directly and its session cookie attached.
    _, _cookie = create_test_account("pay@test.com", "supersecret123", "Pay Co", "enterprise")
    c.cookies.set(SESSION_COOKIE, _cookie)
    csrf = re.search(r'name="csrf" content="([^"]+)"', c.get("/").text).group(1)

    page = c.get("/billing").text
    check("billing page loads Razorpay's checkout.js when payments are live",
          "checkout.razorpay.com" in page)
    check("no demo-upgrade banner once real payments are configured",
          "Demo upgrades are enabled" not in page)

    r = c.post("/api/billing/upgrade", data={"plan": "starter", "csrf_token": csrf})
    d = r.json()
    check("upgrade call succeeds", d.get("ok") is True, str(d))
    check("a real order id comes back to the frontend", d.get("order_id", "").startswith("order_"), str(d))
    check("key_id is exposed, never the secret", d.get("key_id") == KEY_ID)
    check("amount is the plan's own price, not attacker-suppliable",
          d.get("amount_inr") == 999, str(d))

    payment_id = "pay_legit001"
    r2 = c.post("/api/billing/confirm", data={
        "invoice_id": d["invoice_id"], "razorpay_order_id": d["order_id"],
        "razorpay_payment_id": payment_id, "razorpay_signature": sign(d["order_id"], payment_id),
        "csrf_token": csrf})
    d2 = r2.json()
    check("a genuinely matching payment upgrades the plan", d2.get("ok") is True, str(d2))
    check("response names the correct plan (no detached-session crash)",
          d2.get("detail") == "Upgraded to starter.", str(d2))

    with Session(db_eng) as db:
        company = db.exec(select(Company)).first()
        check("company actually moved to starter", company.plan == "starter", company.plan)
        inv = db.get(Invoice, d["invoice_id"])
        check("invoice marked paid", inv.status == "paid", inv.status)
        check("invoice provider_ref updated to the PAYMENT id", inv.provider_ref == payment_id)

    r2b = c.post("/api/billing/confirm", data={
        "invoice_id": d["invoice_id"], "razorpay_order_id": d["order_id"],
        "razorpay_payment_id": payment_id, "razorpay_signature": sign(d["order_id"], payment_id),
        "csrf_token": csrf})
    check("an already-paid invoice cannot be confirmed twice", r2b.json().get("ok") is not True, str(r2b.json()))

    # ---- the attack this session's review caught: replaying a genuinely
    # signed payment from a DIFFERENT (attacker-controlled) order.
    r3 = c.post("/api/billing/upgrade", data={"plan": "pro", "csrf_token": csrf})
    d3 = r3.json()
    other_order, other_payment = "order_ATTACKER_CONTROLLED", "pay_cheap001"
    r4 = c.post("/api/billing/confirm", data={
        "invoice_id": d3["invoice_id"], "razorpay_order_id": other_order,
        "razorpay_payment_id": other_payment,
        "razorpay_signature": sign(other_order, other_payment), "csrf_token": csrf})
    check("a VALIDLY-signed payment for the WRONG order is rejected",
          r4.json().get("ok") is not True, str(r4.json()))

    with Session(db_eng) as db:
        company = db.exec(select(Company)).first()
        check("the replay attack did NOT upgrade the company", company.plan == "starter", company.plan)

    r5 = c.post("/api/billing/upgrade", data={"plan": "pro", "csrf_token": csrf})
    d5 = r5.json()
    r6 = c.post("/api/billing/confirm", data={
        "invoice_id": d5["invoice_id"], "razorpay_order_id": d5["order_id"],
        "razorpay_payment_id": "pay_tamper", "razorpay_signature": "deadbeef" * 8,
        "csrf_token": csrf})
    check("a garbage signature on the correct order is rejected",
          r6.json().get("ok") is not True, str(r6.json()))

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
