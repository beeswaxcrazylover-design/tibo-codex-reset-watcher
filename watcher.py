#!/usr/bin/env python3
"""Track Tibo's X posts about Codex usage-limit resets and send email alerts."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable


LOGGER = logging.getLogger("tibo-reset-watcher")
X_API_BASE = "https://api.x.com/2"
BEIJING_TZ = timezone(timedelta(hours=8), name="UTC+08:00")

DEFAULT_PRODUCT_KEYWORDS = ("codex", "chatgpt work")
DEFAULT_RESET_KEYWORDS = (
    "reset",
    "usage limit",
    "usage limits",
    "rate limit",
    "rate limits",
    "weekly limit",
    "weekly limits",
    "hourly limit",
    "hourly limits",
    "quota",
    "banked reset",
    "reset bank",
    "100% weekly",
    "100% hourly",
)


class ConfigurationError(ValueError):
    """Raised when a required setting is missing or invalid."""


class XApiError(RuntimeError):
    """Raised when X API returns an error."""


def load_dotenv(path: Path) -> None:
    """Load a small, dependency-free subset of .env syntax."""
    if not path.exists():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"{path}:{line_number} 不是有效的 KEY=VALUE 配置")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} 必须是 true/false，当前值为 {raw!r}")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} 不能小于 {minimum}")
    return value


def env_keywords(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return defaults
    values = tuple(item.strip().casefold() for item in raw.split(",") if item.strip())
    if not values:
        raise ConfigurationError(f"{name} 至少需要一个关键词")
    return values


@dataclass(frozen=True)
class Config:
    x_bearer_token: str
    x_username: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    mail_from: str
    mail_to: tuple[str, ...]
    smtp_use_ssl: bool
    smtp_starttls: bool
    poll_interval_seconds: int
    first_run_lookback_hours: int
    state_file: Path
    product_keywords: tuple[str, ...]
    reset_keywords: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        recipients = tuple(
            item.strip() for item in os.getenv("MAIL_TO", "").split(",") if item.strip()
        )
        smtp_username = os.getenv("SMTP_USERNAME", "").strip()
        return cls(
            x_bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
            x_username=os.getenv("X_USERNAME", "thsottiaux").strip().lstrip("@"),
            smtp_host=os.getenv("SMTP_HOST", "").strip(),
            smtp_port=env_int("SMTP_PORT", 465),
            smtp_username=smtp_username,
            smtp_password=os.getenv("SMTP_PASSWORD", ""),
            mail_from=os.getenv("MAIL_FROM", smtp_username).strip(),
            mail_to=recipients,
            smtp_use_ssl=env_bool("SMTP_USE_SSL", True),
            smtp_starttls=env_bool("SMTP_STARTTLS", False),
            poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 300, minimum=30),
            first_run_lookback_hours=env_int("FIRST_RUN_LOOKBACK_HOURS", 24),
            state_file=Path(
                os.getenv("STATE_FILE", ".state/tibo-reset-watcher.json")
            ).expanduser(),
            product_keywords=env_keywords("PRODUCT_KEYWORDS", DEFAULT_PRODUCT_KEYWORDS),
            reset_keywords=env_keywords("RESET_KEYWORDS", DEFAULT_RESET_KEYWORDS),
        )

    def validate(self, *, require_x: bool = True, require_email: bool = True) -> None:
        missing: list[str] = []
        if require_x and not self.x_bearer_token:
            missing.append("X_BEARER_TOKEN")
        if require_x and not self.x_username:
            missing.append("X_USERNAME")
        if require_email:
            if not self.smtp_host:
                missing.append("SMTP_HOST")
            if not self.smtp_username:
                missing.append("SMTP_USERNAME")
            if not self.smtp_password:
                missing.append("SMTP_PASSWORD")
            if not self.mail_from:
                missing.append("MAIL_FROM")
            if not self.mail_to:
                missing.append("MAIL_TO")
        if self.smtp_use_ssl and self.smtp_starttls:
            raise ConfigurationError("SMTP_USE_SSL 和 SMTP_STARTTLS 不能同时为 true")
        if missing:
            raise ConfigurationError("缺少配置：" + ", ".join(missing))


@dataclass(frozen=True)
class Post:
    id: str
    text: str
    created_at: datetime

    @property
    def beijing_time(self) -> datetime:
        return self.created_at.astimezone(BEIJING_TZ)

    def url(self, username: str) -> str:
        return f"https://x.com/{username}/status/{self.id}"


def parse_x_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_beijing_time(value: datetime) -> str:
    return value.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S（北京时间，UTC+08:00）")


def is_reset_related(
    text: str,
    product_keywords: Iterable[str] = DEFAULT_PRODUCT_KEYWORDS,
    reset_keywords: Iterable[str] = DEFAULT_RESET_KEYWORDS,
) -> bool:
    normalized = " ".join(text.casefold().split())
    return any(word.casefold() in normalized for word in product_keywords) and any(
        word.casefold() in normalized for word in reset_keywords
    )


class XClient:
    def __init__(self, bearer_token: str, *, timeout: int = 30) -> None:
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = f"{X_API_BASE}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "tibo-codex-reset-watcher/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body)
                message = detail.get("detail") or detail.get("title") or body
            except json.JSONDecodeError:
                message = body
            raise XApiError(f"X API HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise XApiError(f"连接 X API 失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise XApiError("X API 返回了无效 JSON") from exc
        if payload.get("errors") and not payload.get("data"):
            raise XApiError(f"X API 错误：{payload['errors']}")
        return payload

    def get_user_id(self, username: str) -> str:
        payload = self._get(f"/users/by/username/{urllib.parse.quote(username)}")
        try:
            return str(payload["data"]["id"])
        except (KeyError, TypeError) as exc:
            raise XApiError(f"X API 未返回 @{username} 的用户 ID") from exc

    def get_posts(
        self,
        user_id: str,
        *,
        since_id: str | None = None,
        start_time: datetime | None = None,
        max_pages: int = 10,
    ) -> list[Post]:
        params = {
            "max_results": "100",
            "exclude": "retweets",
            "tweet.fields": "created_at,note_tweet",
        }
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            utc_start = start_time.astimezone(timezone.utc)
            params["start_time"] = utc_start.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )

        posts: list[Post] = []
        for _ in range(max_pages):
            payload = self._get(f"/users/{urllib.parse.quote(user_id)}/tweets", params)
            for item in payload.get("data") or []:
                note_tweet = item.get("note_tweet") or {}
                text = note_tweet.get("text") or item.get("text") or ""
                created_at = item.get("created_at")
                if not created_at:
                    LOGGER.warning("跳过缺少 created_at 的帖子 %s", item.get("id"))
                    continue
                posts.append(
                    Post(
                        id=str(item["id"]),
                        text=text,
                        created_at=parse_x_datetime(created_at),
                    )
                )
            next_token = (payload.get("meta") or {}).get("next_token")
            if not next_token:
                break
            params["pagination_token"] = next_token
        else:
            LOGGER.warning("已达到最多 %d 页，后续帖子将在下一轮继续读取", max_pages)
        return posts


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取状态文件 {self.path}：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"状态文件 {self.path} 的内容必须是 JSON 对象")
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)


class EmailSender:
    def __init__(self, config: Config) -> None:
        self.config = config

    def build_post_message(self, post: Post) -> EmailMessage:
        timestamp = format_beijing_time(post.created_at)
        url = post.url(self.config.x_username)
        subject_time = post.beijing_time.strftime("%m-%d %H:%M")
        message = EmailMessage()
        message["Subject"] = f"[Codex 额度重置动态] Tibo · {subject_time} 北京时间"
        message["From"] = self.config.mail_from
        message["To"] = ", ".join(self.config.mail_to)
        plain = (
            "检测到 Tibo 发布了与 Codex 额度重置有关的消息。\n\n"
            f"发布时间：{timestamp}\n"
            f"原文：\n{post.text}\n\n"
            f"查看原帖：{url}\n\n"
            "提示：本邮件由关键词自动识别生成，请以原帖和你的 Codex 账户页面为准。"
        )
        message.set_content(plain)
        message.add_alternative(
            """<!doctype html>
<html lang="zh-CN"><body style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;line-height:1.6;color:#1f2937">
  <h2 style="margin-bottom:8px">Codex 额度重置动态</h2>
  <p>检测到 Tibo 发布了与 Codex 额度重置有关的消息。</p>
  <p><strong>发布时间：</strong>{timestamp}</p>
  <blockquote style="margin:16px 0;padding:12px 16px;border-left:4px solid #111827;background:#f3f4f6;white-space:pre-wrap">{text}</blockquote>
  <p><a href="{url}">在 X 上查看原帖</a></p>
  <p style="font-size:12px;color:#6b7280">本邮件由关键词自动识别生成，请以原帖和你的 Codex 账户页面为准。</p>
</body></html>""".format(
                timestamp=html.escape(timestamp),
                text=html.escape(post.text),
                url=html.escape(url, quote=True),
            ),
            subtype="html",
        )
        return message

    def build_test_message(self) -> EmailMessage:
        now = format_beijing_time(datetime.now(timezone.utc))
        message = EmailMessage()
        message["Subject"] = "[Codex 额度重置动态] 邮件配置测试"
        message["From"] = self.config.mail_from
        message["To"] = ", ".join(self.config.mail_to)
        message.set_content(f"邮件配置成功。\n发送时间：{now}\n")
        return message

    def send(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.config.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=30,
                context=context,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(message)
            return

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if self.config.smtp_starttls:
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(message)


def process_once(config: Config, *, dry_run: bool = False) -> tuple[int, int]:
    """Process new posts. Returns (posts_seen, matching_posts)."""
    state_store = StateStore(config.state_file)
    state = state_store.load()
    if state.get("username") != config.x_username:
        state = {"version": 1, "username": config.x_username}

    x_client = XClient(config.x_bearer_token)
    user_id = state.get("user_id") or x_client.get_user_id(config.x_username)
    last_seen_id = state.get("last_seen_id")
    start_time = None
    if not last_seen_id:
        start_time = datetime.now(timezone.utc) - timedelta(
            hours=config.first_run_lookback_hours
        )
        LOGGER.info(
            "首次运行：读取最近 %d 小时的帖子", config.first_run_lookback_hours
        )

    posts = x_client.get_posts(
        str(user_id), since_id=str(last_seen_id) if last_seen_id else None, start_time=start_time
    )
    posts.sort(key=lambda item: int(item.id))
    sender = EmailSender(config)
    matching = 0

    if not posts:
        LOGGER.info("没有新帖子")
        if not dry_run and state.get("user_id") != user_id:
            state.update({"user_id": user_id, "updated_at": datetime.now(timezone.utc).isoformat()})
            state_store.save(state)
        return 0, 0

    for post in posts:
        matched = is_reset_related(
            post.text, config.product_keywords, config.reset_keywords
        )
        if matched:
            matching += 1
            if dry_run:
                print(
                    f"[DRY-RUN 匹配] {format_beijing_time(post.created_at)}\n"
                    f"{post.text}\n{post.url(config.x_username)}\n"
                )
            else:
                sender.send(sender.build_post_message(post))
                LOGGER.info("已发送邮件：%s", post.url(config.x_username))
        else:
            LOGGER.info("忽略不相关帖子：%s", post.url(config.x_username))

        if not dry_run:
            state.update(
                {
                    "version": 1,
                    "username": config.x_username,
                    "user_id": user_id,
                    "last_seen_id": post.id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            state_store.save(state)
    return len(posts), matching


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="追踪 Tibo 在 X 上发布的 Codex 额度重置消息并发送邮件"
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(".env"), help="环境变量文件（默认 .env）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="运行一次后退出（默认）")
    mode.add_argument("--daemon", action="store_true", help="常驻运行并定时轮询")
    mode.add_argument("--test-email", action="store_true", help="仅发送 SMTP 测试邮件")
    parser.add_argument(
        "--dry-run", action="store_true", help="读取和筛选帖子，但不发邮件、不更新状态"
    )
    parser.add_argument("--verbose", action="store_true", help="显示详细日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        load_dotenv(args.env_file)
        config = Config.from_env()
        if args.test_email:
            config.validate(require_x=False, require_email=True)
            sender = EmailSender(config)
            sender.send(sender.build_test_message())
            LOGGER.info("测试邮件已发送到 %s", ", ".join(config.mail_to))
            return 0

        config.validate(require_x=True, require_email=not args.dry_run)
        if not args.daemon:
            seen, matching = process_once(config, dry_run=args.dry_run)
            LOGGER.info("本轮完成：读取 %d 条，匹配 %d 条", seen, matching)
            return 0

        LOGGER.info("开始常驻轮询，每 %d 秒检查一次", config.poll_interval_seconds)
        while True:
            try:
                seen, matching = process_once(config, dry_run=args.dry_run)
                LOGGER.info("本轮完成：读取 %d 条，匹配 %d 条", seen, matching)
            except (XApiError, OSError, smtplib.SMTPException, RuntimeError) as exc:
                LOGGER.exception("本轮执行失败，将在下一轮重试：%s", exc)
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        LOGGER.info("已停止")
        return 130
    except (ConfigurationError, XApiError, OSError, smtplib.SMTPException, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
