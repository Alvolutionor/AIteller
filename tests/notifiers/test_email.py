import pytest
from unittest.mock import patch, MagicMock
from src.notifiers.email_notifier import EmailNotifier

@pytest.fixture
def email_config():
    return {
        "notification": {
            "channels": {
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.test.com",
                    "smtp_port": 465,
                    "sender": "test@test.com",
                    "password": "testpass",
                    "recipients": ["user@test.com"],
                }
            }
        }
    }

@pytest.mark.asyncio
async def test_email_sends_digest(email_config):
    notifier = EmailNotifier(email_config)
    with patch("src.notifiers.email_notifier.smtplib.SMTP_SSL") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
        result = await notifier.send("# Test Digest\n**Bold text**\n[Link](https://example.com)")
    assert result is True
    mock_server.login.assert_called_once_with("test@test.com", "testpass")
    mock_server.sendmail.assert_called_once()

@pytest.mark.asyncio
async def test_email_missing_config():
    notifier = EmailNotifier({"notification": {"channels": {"email": {}}}})
    result = await notifier.send("Test")
    assert result is False

@pytest.mark.asyncio
async def test_email_markdown_to_html(email_config):
    notifier = EmailNotifier(email_config)
    html = notifier._markdown_to_html("# Title\n**bold** and [link](https://x.com)")
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://x.com">link</a>' in html
