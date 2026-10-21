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
    ],
)
def test_redacts_secrets(text: str) -> None:
    out = redact(text)

    assert "SECRET" not in out
    assert "REDACTED" in out


def test_keeps_harmless_text() -> None:
    text = "HTTP 404 for https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3 (task-abcdefgh12)"

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


def test_keeps_url_without_password() -> None:
    text = "ssh://git@github.com/x and mailto:a@b.test"
    assert redact(text) == text
