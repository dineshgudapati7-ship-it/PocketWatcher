"""Plain-text + HTML email over SMTP."""

import smtplib
from email.message import EmailMessage

from jinja2 import Environment, PackageLoader, select_autoescape

from pocketwatcher.alerts.message import SEVERITY_ICON, Message
from pocketwatcher.settings import Settings

_env = Environment(loader=PackageLoader("pocketwatcher.alerts"), autoescape=select_autoescape(["html"]))


def render(msg: Message, sender: str, recipients: list[str]) -> EmailMessage:
    email = EmailMessage()
    email["Subject"] = f"{SEVERITY_ICON.get(msg.severity, '')} {msg.title}".strip()
    email["From"] = sender
    email["To"] = ", ".join(recipients)
    email.set_content(_env.get_template("email.txt").render(msg=msg))
    email.add_alternative(_env.get_template("email.html").render(msg=msg), subtype="html")
    return email


def send(target: str, msg: Message, settings: Settings) -> None:
    recipients = [a.strip() for a in target.split(",") if a.strip()]
    email = render(msg, settings.smtp_from, recipients)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(email)
