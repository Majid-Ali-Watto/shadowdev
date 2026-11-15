"""Email to developers, about their own work (the questions a run asks them, tickets their status
rules hold), through the server's SMTP account (SMTP_* in .env). Encrypted with TLS, except to a
relay on this machine."""
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

TIMEOUT_SECONDS = 30
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    sender: str  # the From address
    username: str = ""
    password: str = field(default="", repr=False)

    @property
    def local(self) -> bool:
        return self.host in LOCAL_HOSTS


def send_mail(smtp: Smtp, to: str, subject: str, text: str, html: str = "") -> None:
    """Send an email: plain text, and `html` beside it for the mail apps that show it. Raises
    OSError or smtplib.SMTPException."""
    message = EmailMessage()
    message["From"], message["To"] = smtp.sender, to
    message["Subject"] = " ".join(subject.split())[:200]  # one line: no header can be added through it
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    if smtp.port == 465:
        server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, timeout=TIMEOUT_SECONDS)
    with server:
        if smtp.port != 465 and not smtp.local:
            server.starttls(context=context)  # refuses to go on unencrypted when the server can't
        if smtp.username:
            server.login(smtp.username, smtp.password)
        server.send_message(message)
