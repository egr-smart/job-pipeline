"""Where a digest goes: a report file and/or an email."""

import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

from .config import Config, EmailNotifier, FileNotifier
from .digest import Digest


class NotifyError(Exception):
    pass


def write_report(cfg: Config, n: FileNotifier, digest: Digest, *, suffix: str = "") -> Path:
    out_dir = cfg.state_path(n.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(UTC).strftime("%Y-%m-%d") + suffix
    (out_dir / f"{stem}.md").write_text(digest.markdown)
    html_path = out_dir / f"{stem}.html"
    html_path.write_text(digest.html)
    return html_path


def send_email(n: EmailNotifier, digest: Digest) -> None:
    user, password = os.environ.get(n.username_env), os.environ.get(n.password_env)
    if not user or not password:
        raise NotifyError(f"set ${n.username_env} and ${n.password_env} to send email")
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = n.from_ or user
    msg["To"] = n.to
    msg.set_content(digest.markdown)
    msg.add_alternative(digest.html, subtype="html")
    try:
        with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"{type(e).__name__}: {e}") from e
