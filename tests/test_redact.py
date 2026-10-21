import time

import pytest

from clipper.redact import redact


@pytest.mark.parametrize(
    "text",
    [
        "https://www.googleapis.com/youtube/v3/videos?id=abc&key=SECRET",
        "GET /x?api_key=SECRET&part=1",
        "https://id.twitch.tv/oauth2/token?client_id=c&client_secret=SECRET",
        "https://x.test/cb?access_token=SECRET#frag",
        "https://x.test/?TOKEN=SECRET",
        "https://cdn.test/v.mp4?expire=1&sig=SECRET",
        "https://cdn.test/v.mp4?signature=SECRET",
        "https://discord.com/api/webhooks/123456/SECRET-tok_en",
        "https://discordapp.com/api/v10/webhooks/123456/SECRET",
        "https://hooks.slack.com/services/T000/B000/SECRET",
        "headers={'Authorization': 'Bearer SECRET'}",
        "Authorization: Basic SECRET",
        "sent Bearer SECRET.part-two",
        "anthropic said: invalid x-api-key sk-ant-api03-SECRETSECRET",
        "groq: bad key gsk_SECRETSECRET123",
        "rclone: s3 access_key_id=SECRET rejected",
        "Aws_Secret_Access_Key: SECRET",
        "secret_access_key = 'SECRET'",
        "config pass=SECRET user=me",
        '{"password": "SECRET"}',
        "password: SECRET",
        "refresh token=SECRET expired",
        "rclone said token:SECRET",
        "s3 secret-access-key=SECRET",
        "aws_secret_access_key=SECRET",
        "access-key-id: SECRET",
        "client_secret=SECRET (form body)",
        "api_key: SECRET",
        "x-api-key=SECRET",
        "DRIVE_TOKEN=SECRET",
        "passwd=SECRET",
        "my_secret: 'SECRET'",
        "gpg passphrase=SECRET",
        "https://bucket.s3.amazonaws.com/k X-Amz-Signature=SECRET",
        "secret: 'SECRET with spaces'",
        '{"password": "p SECRET w"}',
    ],
)
def test_redacts_secrets(text: str) -> None:
    out = redact(text)

    assert "SECRET" not in out
    assert "REDACTED" in out


def test_keeps_harmless_text() -> None:
    text = "HTTP 404 for https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3 (task-abcdefgh12)"

    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "bypass=1 compass: north",
        "passive token-bucket full",
        "max_tokens=100 tokens: 5",
        "secretary: Ann",
    ],
)
def test_keeps_lookalike_words(text: str) -> None:
    assert redact(text) == text


def test_keeps_url_shape_and_is_idempotent() -> None:
    once = redact("https://x.test/a?id=1&key=SECRET&page=2")

    assert once == "https://x.test/a?id=1&key=REDACTED&page=2"
    assert redact(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("proxy http://bob:SECRET@10.0.0.1:8080 refused", "proxy http://***@10.0.0.1:8080 refused"),
        ("socks5h://u:p%40SECRET@proxy.test", "socks5h://***@proxy.test"),
        ("HTTPS://Me:SECRET@host/x", "HTTPS://***@host/x"),
    ],
)
def test_redacts_url_userinfo(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("secret: 'a b c'", "secret: 'REDACTED'"),
        ('{"password": "p w", "user": "me"}', '{"password": "REDACTED", "user": "me"}'),
        ("token=abc def", "token=REDACTED def"),
    ],
)
def test_quoted_values_masked_to_closing_quote(text: str, expected: str) -> None:
    assert redact(text) == expected
    assert redact(expected) == expected


def test_keeps_url_without_password() -> None:
    text = "ssh://git@github.com/x and mailto:a@b.test"
    assert redact(text) == text


@pytest.mark.parametrize("text", ["a-" * 32768, "a." * 32768, "a+" * 30000, "a" * 65536 + "://"])
def test_url_userinfo_pattern_is_linear(text: str) -> None:
    # 64 KB inputs run every pattern, ~35 ms locally; CI runners are ~3x slower.
    # Backtracking blow-up would take seconds, so 0.5 s still catches it.
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.5


def test_url_userinfo_with_percent_encoded_password() -> None:
    assert redact("via http://u:p%40ss@h:3128/ ok") == "via http://***@h:3128/ ok"


@pytest.mark.parametrize("text", ["-" * 20000, "_" * 20000, "a-" * 10000, "x" * 50000])
def test_pathological_input_is_fast(text: str) -> None:
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.1


def test_url_userinfo_and_key_value_both_masked() -> None:
    out = redact("proxy http://u:SECRET1@h:3128 failed, token=SECRET2")

    assert out == "proxy http://***@h:3128 failed, token=REDACTED"
