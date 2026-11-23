"""Minimal SMTP delivery adapter for password reset messages.

Delivery is disabled unless SMTP host/from settings are configured. No reset token
or password is logged. A production deployment should provide a managed mailer.
"""

import os
import smtplib
from email.message import EmailMessage


def send_password_reset_email(email: str, token: str) -> None:
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM")
    if not host or not sender:
        raise RuntimeError("password-reset email delivery is not configured")
    message = EmailMessage()
    message["Subject"] = "FactoryOS password reset"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        "A password reset was requested for your FactoryOS account. "
        "Use this one-time token in the password reset form: " + token + "\n"
        "If you did not request this, ignore this message."
    )
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        username = os.environ.get("SMTP_USERNAME")
        password = os.environ.get("SMTP_PASSWORD")
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


def send_membership_invitation(email: str, token: str, company_id: str) -> None:
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM")
    if not host or not sender:
        raise RuntimeError("membership email delivery is not configured")
    message = EmailMessage()
    message["Subject"] = "FactoryOS company invitation"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"You have been invited to join company {company_id} in FactoryOS. "
        "Enter this one-time invitation token in the account invitation form: " + token + "\n"
        "If you did not expect this invitation, ignore this message."
    )
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        username = os.environ.get("SMTP_USERNAME")
        password = os.environ.get("SMTP_PASSWORD")
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)
