import unittest
from unittest.mock import MagicMock, patch

from modules.email_notifications import notify_email


class EmailNotificationTest(unittest.TestCase):
    @patch("modules.email_notifications.smtplib.SMTP_SSL")
    @patch("modules.email_notifications._load_email_settings")
    def test_sends_notification_with_configured_recipient(self, settings_mock, smtp_mock):
        settings_mock.return_value = (
            "sender@example.com", "app-password", "owner@example.com", "smtp.example.com", 465,
        )
        connection = MagicMock()
        smtp_mock.return_value.__enter__.return_value = connection

        self.assertTrue(notify_email("ERRORE: pubblicazione fallita"))

        smtp_mock.assert_called_once_with("smtp.example.com", 465, timeout=20)
        connection.login.assert_called_once_with("sender@example.com", "app-password")
        sent_message = connection.send_message.call_args.args[0]
        self.assertEqual(sent_message["To"], "owner@example.com")
        self.assertIn("ERRORE", sent_message["Subject"])

    @patch("modules.email_notifications._load_email_settings", return_value=None)
    def test_missing_settings_does_not_break_cron(self, _settings_mock):
        self.assertFalse(notify_email("ERRORE di prova"))


if __name__ == "__main__":
    unittest.main()
