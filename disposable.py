"""
Disposable ("temp mail") email domains, refused at signup.

The signup code proves someone can open an inbox - not that it's a real,
lasting one. A throwaway inbox would let anyone farm unlimited free-minute
accounts, with each account's calls billed to our Twilio and AI budget.

The list is the public-domain disposable-email-domains project
(github.com/disposable-email-domains), saved in data/ so the check needs no
network call and no external service. New temp-mail domains appear all the
time, so no list is complete: refresh the file now and then, and add any
domain seen slipping through to _EXTRA below.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_LIST = Path(__file__).parent / "data" / "disposable_email_domains.txt"

# Seen in our own signups before the upstream list had them.
_EXTRA = {"dreameg.com"}


@lru_cache(maxsize=1)
def _domains() -> frozenset[str]:
    try:
        lines = _LIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        # A missing list must never break signup - it just stops blocking.
        lines = []
    listed = {ln.strip().lower() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}
    return frozenset(listed | _EXTRA)


def is_disposable(email: str) -> bool:
    """True if the address is on a known temp-mail domain, subdomains included."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    parts = email.rsplit("@", 1)[1].split(".")
    domains = _domains()
    # "x.mailinator.com" is as disposable as "mailinator.com".
    return any(".".join(parts[i:]) in domains for i in range(len(parts) - 1))
