"""
Login, signup, team invites, and the guard every protected route depends on.

The single most important rule in this file: get_current_user() is the ONLY way
a request learns which company it belongs to. Routes must never accept a
company_id from the client - otherwise one customer could read another's calls.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import Request
from sqlmodel import Session, select

from disposable import is_disposable
from models import Agent, Company, CompanySettings, Invite, User, engine, utcnow
from security import (
    SESSION_COOKIE,
    hash_password,
    make_session,
    read_session,
    verify_password,
)


class AuthError(Exception):
    """Raised for signup/login problems that are safe to show the user."""


def _validate_credentials(email: str, password: str) -> None:
    if not email or "@" not in email:
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if len(password.encode()) > 72:
        raise AuthError("Password is too long (max 72 bytes).")


def _seed_default_agent(company_id: int, company_name: str, db: Session) -> None:
    """Every new workspace gets one working agent, so the dashboard is never an
    empty shell on first login. Carried over from v2's signup flow."""
    db.add(
        Agent(
            company_id=company_id,
            name="Aria — Default",
            greeting=f"Hi {{name}}, this is Aria from {company_name}. How can I help you today?",
            system_prompt=(
                f"You are Aria, a warm and efficient voice agent for {company_name}. "
                "Keep every reply to one short sentence."
            ),
        )
    )


def prepare_signup(
    email: str, password: str, company_name: str, account_type: str = "enterprise"
) -> tuple[str, str, str]:
    """Validate a signup and return the cleaned (email, company_name, account_type).

    Creates nothing. The web form calls this, emails a confirmation link, and
    only creates the account once that link is clicked - so a mistyped or fake
    address never produces an account.

    account_type "individual": no business required - the workspace is named
    after the person, so freelancers and solo users can sign up too.
    """
    email = email.strip().lower()
    company_name = company_name.strip()
    account_type = "individual" if account_type == "individual" else "enterprise"

    _validate_credentials(email, password)
    if is_disposable(email):
        raise AuthError("Please use a permanent email address - temporary inboxes aren't allowed.")
    if account_type == "enterprise" and not company_name:
        raise AuthError("Enter your company name.")
    if account_type == "individual" and not company_name:
        # Fall back to the mailbox name, e.g. "avani" from avani@gmail.com
        company_name = email.split("@")[0].title()

    with Session(engine) as db:
        if db.exec(select(User).where(User.email == email)).first():
            raise AuthError("An account with that email already exists.")
    return email, company_name, account_type


def signup(
    email: str, password: str, company_name: str, account_type: str = "enterprise"
) -> tuple[User, str]:
    """Create an account immediately, unverified. For tests and scripts only -
    the signup form goes through prepare_signup() and the emailed link."""
    email, company_name, account_type = prepare_signup(email, password, company_name, account_type)
    return create_account(email, hash_password(password), company_name, account_type, verified=False)


def create_account(
    email: str, password_hash: str, company_name: str, account_type: str, *, verified: bool
) -> tuple[User, str]:
    """Create a Company + its first User. Returns the user and a session cookie.

    Re-checks the email: between the confirmation email being sent and its
    link being clicked, the same address may already have been registered
    (or the link clicked twice).
    """
    with Session(engine) as db:
        if db.exec(select(User).where(User.email == email)).first():
            raise AuthError("An account with that email already exists.")

        company = Company(
            name=company_name,
            account_type=account_type,
            plan="free",
            period_start=utcnow(),
            period_end=utcnow() + timedelta(days=30),
        )
        db.add(company)
        db.commit()
        db.refresh(company)

        user = User(
            company_id=company.id,
            email=email,
            password_hash=password_hash,
            role="owner",
            is_verified=verified,
        )
        db.add(user)
        # Every company starts with a settings row so the UI never hits a null.
        db.add(
            CompanySettings(
                company_id=company.id,
                company_display_name=company_name,
            )
        )
        _seed_default_agent(company.id, company_name, db)
        db.commit()
        db.refresh(user)

        return user, make_session(user.id, company.id)


def login(email: str, password: str) -> tuple[User, str]:
    email = email.strip().lower()
    with Session(engine) as db:
        user = db.exec(select(User).where(User.email == email)).first()
        # Same message for unknown email and wrong password, so the form can't
        # be used to discover which addresses have accounts.
        if not user or not verify_password(password, user.password_hash):
            raise AuthError("Incorrect email or password.")
        return user, make_session(user.id, user.company_id)


def accept_invite(invite_id: int, name: str, password: str) -> tuple[User, str]:
    """Turn a pending Invite into a real member login. Ported from v2."""
    _validate_credentials("placeholder@x.com", password)

    with Session(engine) as db:
        invite = db.get(Invite, invite_id)
        if not invite or invite.accepted:
            raise AuthError("This invitation is invalid or has already been used.")
        if db.exec(select(User).where(User.email == invite.email)).first():
            raise AuthError("That email already has an account - please sign in instead.")

        user = User(
            company_id=invite.company_id,
            email=invite.email,
            password_hash=hash_password(password),
            role=invite.role if invite.role in ("owner", "member") else "member",
            # Receiving the invite at that address already proves it works.
            is_verified=True,
        )
        db.add(user)
        invite.accepted = True
        db.add(invite)
        db.commit()
        db.refresh(user)
        return user, make_session(user.id, user.company_id)


def get_current_user(request: Request) -> User | None:
    """Resolve the logged-in user from the signed session cookie, or None."""
    data = read_session(request.cookies.get(SESSION_COOKIE))
    if not data:
        return None
    with Session(engine) as db:
        user = db.get(User, data.get("uid"))
        # Re-check the company matches the cookie, so an old cookie issued
        # before a change can't grant access to a different company.
        if user and user.company_id == data.get("cid"):
            return user
    return None


def require_owner(user: User | None) -> None:
    """Guard for destructive or billing-related actions."""
    if not user or user.role != "owner":
        raise AuthError("Only the workspace owner can do that.")
