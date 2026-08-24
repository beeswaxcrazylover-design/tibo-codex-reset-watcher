import os
import smtplib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import watcher


def make_config(state_file: Path) -> watcher.Config:
    return watcher.Config(
        x_bearer_token="token",
        x_username="thsottiaux",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="from@example.com",
        smtp_password="secret",
        mail_from="from@example.com",
        mail_to=("to@example.com",),
        smtp_use_ssl=True,
        smtp_starttls=False,
        poll_interval_seconds=300,
        first_run_lookback_hours=24,
        state_file=state_file,
        product_keywords=watcher.DEFAULT_PRODUCT_KEYWORDS,
        reset_keywords=watcher.DEFAULT_RESET_KEYWORDS,
    )


class MatchingTests(unittest.TestCase):
    def test_matches_codex_reset(self):
        self.assertTrue(
            watcher.is_reset_related(
                "Codex usage limits have now been reset across all paid plans."
            )
        )

    def test_matches_chatgpt_work_reset(self):
        self.assertTrue(
            watcher.is_reset_related(
                "Another usage limit reset for all ChatGPT Work users."
            )
        )

    def test_rejects_unrelated_codex_post(self):
        self.assertFalse(watcher.is_reset_related("Codex shipped a new feature today."))

    def test_rejects_reset_without_product(self):
        self.assertFalse(watcher.is_reset_related("I reset my laptop this morning."))


class TimeTests(unittest.TestCase):
    def test_converts_utc_to_beijing(self):
        value = watcher.parse_x_datetime("2026-08-24T01:02:03.000Z")
        self.assertEqual(
            watcher.format_beijing_time(value),
            "2026-08-24 09:02:03（北京时间，UTC+08:00）",
        )


class XClientTests(unittest.TestCase):
    def test_uses_long_post_text(self):
        client = watcher.XClient("test")
        payload = {
            "data": [
                {
                    "id": "123",
                    "text": "truncated",
                    "note_tweet": {"text": "Full Codex usage limits reset text"},
                    "created_at": "2026-08-24T01:02:03.000Z",
                }
            ],
            "meta": {},
        }
        with patch.object(client, "_get", return_value=payload):
            posts = client.get_posts("42")
        self.assertEqual(posts[0].text, "Full Codex usage limits reset text")


class StateStoreTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            store = watcher.StateStore(path)
            store.save({"last_seen_id": "123"})
            self.assertEqual(store.load()["last_seen_id"], "123")


class EmailTests(unittest.TestCase):
    def test_message_contains_beijing_time_and_url(self):
        config = make_config(Path("state.json"))
        post = watcher.Post(
            id="123",
            text="Codex usage limits reset",
            created_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc),
        )
        message = watcher.EmailSender(config).build_post_message(post)
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("2026-08-24 09:02:03", plain)
        self.assertIn("https://x.com/thsottiaux/status/123", plain)


class ProcessTests(unittest.TestCase):
    def test_sends_match_and_advances_cursor(self):
        post = watcher.Post(
            id="123",
            text="Codex usage limits have been reset.",
            created_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory) / "state.json")
            with (
                patch.object(watcher.XClient, "get_user_id", return_value="42"),
                patch.object(watcher.XClient, "get_posts", return_value=[post]),
                patch.object(watcher.EmailSender, "send") as send,
            ):
                result = watcher.process_once(config)
            self.assertEqual(result, (1, 1))
            send.assert_called_once()
            self.assertEqual(
                watcher.StateStore(config.state_file).load()["last_seen_id"], "123"
            )

    def test_smtp_failure_does_not_advance_failed_post(self):
        post = watcher.Post(
            id="456",
            text="Codex usage limits have been reset.",
            created_at=datetime(2026, 8, 24, 1, 2, 3, tzinfo=timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory) / "state.json")
            with (
                patch.object(watcher.XClient, "get_user_id", return_value="42"),
                patch.object(watcher.XClient, "get_posts", return_value=[post]),
                patch.object(
                    watcher.EmailSender,
                    "send",
                    side_effect=smtplib.SMTPException("temporary failure"),
                ),
            ):
                with self.assertRaises(smtplib.SMTPException):
                    watcher.process_once(config)
            self.assertFalse(config.state_file.exists())


class DotenvTests(unittest.TestCase):
    def test_loads_values_without_overwriting_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("A=from-file\nB='quoted value'\n", encoding="utf-8")
            with patch.dict(os.environ, {"A": "already-set"}, clear=False):
                os.environ.pop("B", None)
                watcher.load_dotenv(path)
                self.assertEqual(os.environ["A"], "already-set")
                self.assertEqual(os.environ["B"], "quoted value")
                os.environ.pop("B", None)


if __name__ == "__main__":
    unittest.main()
