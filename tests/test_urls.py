import pytest

from clipper.urls import InvalidSourceURL, SourceRef, canonical_url, parse_source_url

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://WWW.YouTube.com/watch?v={VID}&t=42s&list=PL123",
        f"https://www.youtube.com/watch?feature=share&v={VID}#t=10",
        f"https://www.youtube.com/live/{VID}",
        f"https://www.youtube.com/live/{VID}/?si=abc",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=xyz&t=5",
    ],
)
def test_parses_youtube(url: str) -> None:
    assert parse_source_url(url) == SourceRef("youtube", VID)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/videos/2283744218",
        "https://twitch.tv/videos/2283744218",
        "https://m.twitch.tv/videos/2283744218?t=1h2m3s",
    ],
)
def test_parses_twitch_vod(url: str) -> None:
    assert parse_source_url(url) == SourceRef("twitch", "2283744218")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (f"http://www.youtube.com/watch?v={VID}", "https://"),
        (f"www.youtube.com/watch?v={VID}", "https://"),
        (f"javascript://youtube.com/watch?v={VID}", "https://"),
        (f"https://user@www.youtube.com/watch?v={VID}", "credentials"),
        (f"https://user:pw@youtu.be/{VID}", "credentials"),
        (f"https://www.youtube.com:8443/watch?v={VID}", "port"),
        (f"https://evil.com/watch?v={VID}", "unsupported host"),
        (f"https://youtube.com.evil.com/watch?v={VID}", "unsupported host"),
        (f"https://music.youtube.com/watch?v={VID}", "unsupported host"),
        (f"https://www.youtube.com/shorts/{VID}", "unsupported YouTube URL"),
        ("https://www.youtube.com/@MrBeast", "unsupported YouTube URL"),
        ("https://www.youtube.com/watch", "unsupported YouTube URL"),
        (f"https://www.youtube.com/watch?v={VID}&v={VID}", "unsupported YouTube URL"),
        ("https://www.youtube.com/watch?v=short", "valid youtube video id"),
        ("https://www.youtube.com/watch?v=-4mo8fGTST", "valid youtube video id"),  # 10 chars
        ("https://www.youtube.com/watch?v=-4mo8fGTST0x", "valid youtube video id"),  # 12
        ("https://www.youtube.com/watch?v=-4mo8fGTST.", "valid youtube video id"),
        ("https://www.youtube.com/watch?v=--../../etc", "valid youtube video id"),
        ("https://youtu.be/-4mo8fGTS%2F", "valid youtube video id"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXc%2F", "valid youtube video id"),
        (f"https://youtu.be/{VID}/extra", "youtu.be"),
        ("https://www.twitch.tv/kaicenat", "Twitch"),
        ("https://www.twitch.tv/videos/abc", "valid twitch video id"),
        ("https://www.twitch.tv/videos/0123", "valid twitch video id"),
        ("https://www.twitch.tv/kaicenat/clip/Foo", "Twitch"),
        (f"https://youtu.be/{VID}\n", "whitespace"),
        (f" https://youtu.be/{VID}", "whitespace"),
        ("https://youtu.be/" + "a" * 3000, "too long"),
        ("", "https://"),
        (f"https://[::1/watch?v={VID}", "malformed"),
        (f"https://\uff59outube.com/watch?v={VID}", "ASCII"),
        (f"https://youtube.com\\@evil.com/watch?v={VID}", "backslash"),
        (f"https://youtube.com.\\evil.com/watch?v={VID}", "backslash"),
        (f"https://youtube.com./watch?v={VID}", "unsupported host"),
        ("https://www.twitch.tv/videos/\uff11\uff12\uff13", "ASCII"),
        ("https://www.youtube.com/watch?v=", "valid youtube video id"),
        (f"https://www.youtube.com/watch?v=&v={VID}", "unsupported YouTube URL"),
        ("https://www.youtube.com/watch?v=%64Qw4w9WgXcQ", "valid youtube video id"),
        ("https://youtu.be/%64Qw4w9WgXcQ", "valid youtube video id"),
        ("https://www.twitch.tv/videos/%31%32", "valid twitch video id"),
    ],
)
def test_rejects(url: str, message: str) -> None:
    with pytest.raises(InvalidSourceURL, match=message):
        parse_source_url(url)


def test_canonical_url_round_trips() -> None:
    for ref in (SourceRef("youtube", VID), SourceRef("twitch", "123")):
        assert parse_source_url(canonical_url(ref)) == ref

    assert canonical_url(SourceRef("youtube", VID)) == f"https://www.youtube.com/watch?v={VID}"
    assert canonical_url(SourceRef("twitch", "123")) == "https://www.twitch.tv/videos/123"


def test_source_ref_validates_id_and_key() -> None:
    with pytest.raises(InvalidSourceURL):
        SourceRef("youtube", "../../etc/pa")
    with pytest.raises(InvalidSourceURL):
        SourceRef("vimeo", "123")  # type: ignore[arg-type]

    assert SourceRef("twitch", "42").key == "twitch_42"


# --- CLP-84: real YouTube ids can start with "-" ---------------------------------------

DASH_IDS = ["-4mo8fGTST0", "-HgaXKz0rtg", "-----------", "_-_-_-_-_-_"]


@pytest.mark.parametrize("vid", DASH_IDS)
@pytest.mark.parametrize(
    "template",
    [
        "https://www.youtube.com/watch?v={}",
        "https://youtube.com/watch?v={}&t=42s",
        "https://www.youtube.com/live/{}",
        "https://youtu.be/{}?si=xyz",
    ],
)
def test_parses_leading_dash_youtube_ids(template: str, vid: str) -> None:
    ref = parse_source_url(template.format(vid))

    assert ref == SourceRef("youtube", vid)
    assert ref.key == f"youtube_{vid}"  # the media dir name never starts with "-"


@pytest.mark.parametrize("vid", DASH_IDS)
def test_canonical_url_of_a_leading_dash_id_round_trips(vid: str) -> None:
    ref = SourceRef("youtube", vid)

    assert canonical_url(ref) == f"https://www.youtube.com/watch?v={vid}"
    assert parse_source_url(canonical_url(ref)) == ref


@pytest.mark.parametrize(
    "vid",
    [
        "-4mo8fGTST",  # 10 characters
        "-4mo8fGTST00",  # 12 characters
        "-4mo8fGTS/0",
        "-4mo8fGTS.0",
        "-4mo8fGT ST",
        "../../etc/p",
        "..-------..",
        "-4mo8fGTST\n",
        "-4mo8fGTST\u00e9",
        "",
    ],
)
def test_source_ref_still_rejects_bad_youtube_ids(vid: str) -> None:
    with pytest.raises(InvalidSourceURL):
        SourceRef("youtube", vid)


@pytest.mark.parametrize("vid", ["-1", "-123", "0", "1-2", "12a"])
def test_twitch_ids_stay_numeric(vid: str) -> None:
    with pytest.raises(InvalidSourceURL):
        SourceRef("twitch", vid)
