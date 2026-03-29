# src/notifiers/email_notifier.py
import logging
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from .base import BaseNotifier

logger = logging.getLogger(__name__)


class EmailNotifier(BaseNotifier):
    def __init__(self, config: dict):
        super().__init__(config)
        channels = config.get("notification", {}).get("channels", {})
        email_cfg = channels.get("email", {})
        self.smtp_host = email_cfg.get("smtp_host", "")
        self.smtp_port = email_cfg.get("smtp_port", 465)
        self.sender = email_cfg.get("sender", "")
        self.password = email_cfg.get("password", "")
        raw_recipients = email_cfg.get("recipients", [])
        # Support both list and comma-separated string from env var
        if isinstance(raw_recipients, str):
            self.recipients = [r.strip() for r in raw_recipients.split(",") if r.strip()]
        else:
            self.recipients = raw_recipients

    async def send(self, digest: str, compact_digest: str = None,
                   attachment: str | Path = None, subject: str = None) -> bool:
        if not self.sender or not self.password or not self.recipients:
            logger.warning("Email not configured (missing sender/password/recipients)")
            return False

        try:
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject or "AIteller AI实践日报"
            msg["From"] = self.sender
            msg["To"] = ", ".join(self.recipients)

            # Text body
            body = MIMEMultipart("alternative")
            body.attach(MIMEText(digest, "plain", "utf-8"))
            html_body = self._markdown_to_html(digest)
            body.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(body)

            # PDF attachment
            if attachment:
                pdf_path = Path(attachment)
                if pdf_path.exists():
                    with open(pdf_path, "rb") as f:
                        part = MIMEApplication(f.read(), Name=pdf_path.name)
                    part["Content-Disposition"] = f'attachment; filename="{pdf_path.name}"'
                    msg.attach(part)

            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, self.recipients, msg.as_string())

            logger.info("Email sent to %d recipients", len(self.recipients))
            return True
        except Exception as e:
            logger.error("Email notification failed: %s", e)
            return False

    def _markdown_to_html(self, markdown: str) -> str:
        """Basic markdown to HTML conversion."""
        html = markdown
        # Headers
        import re
        html = re.sub(r'^### (.+)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
        html = re.sub(r'^## (.+)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
        html = re.sub(r'^# (.+)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
        # Bold
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        # Links
        html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html)
        # Line breaks
        html = html.replace("\n", "<br>\n")

        return f"""<html><body style="font-family: sans-serif; max-width: 800px; margin: 0 auto; padding: 20px;">
{html}
</body></html>"""
