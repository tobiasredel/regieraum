import pytest

from publish import storage as st
from publish.transfer import content_type, transfer_tree
from tests.publish_fixtures.helpers import fake_mp3


class Spy(st.LocalStorage):
    def __init__(self, *a):
        super().__init__(*a)
        self.meta = {}

    def put_file(self, key, path, content_type, cache_control=None):
        self.meta[key] = (content_type, cache_control)
        super().put_file(key, path, content_type, cache_control)


def _tree(root):
    (root / "media/g1").mkdir(parents=True)
    (root / "media/g1/a-1.mp3").write_bytes(fake_mp3(3000))
    (root / "media/g1/a-1.chapters.json").write_text("{}")
    (root / "media/g1/a-1.vtt").write_text("WEBVTT\n")
    (root / "media/g1/.upload-rest").write_bytes(b"x")
    (root / "cover-12345678.jpg").write_bytes(b"\xff\xd8jpg")
    (root / "feed.xml").write_text("<rss/>")
    (root / "notiz.txt").write_text("?")


def test_transfer_keeps_keys_types_and_is_repeatable(tmp_path):
    src = tmp_path / "public"
    _tree(src)
    target = Spy(tmp_path / "ziel", "https://pub.example.org")
    seen = []
    res = transfer_tree(src, target, on_progress=lambda i, n, key, what: seen.append((i, n, what)))
    assert sorted(res.uploaded) == ["cover-12345678.jpg", "media/g1/a-1.chapters.json", "media/g1/a-1.mp3", "media/g1/a-1.vtt"]
    assert res.unknown == ["notiz.txt"] and res.skipped == []
    assert res.bytes_uploaded == res.bytes_total == sum(target.size(k) for k in res.uploaded)
    assert target.meta["media/g1/a-1.mp3"] == ("audio/mpeg", st.IMMUTABLE)
    assert target.meta["media/g1/a-1.chapters.json"][0] == "application/json+chapters"
    assert target.meta["cover-12345678.jpg"][0] == "image/jpeg"
    assert not target.exists("feed.xml") and not target.exists("notiz.txt") and not target.exists("media/g1/.upload-rest")
    assert [s[:2] for s in seen] == [(i, 5) for i in range(1, 6)]

    # zweiter Lauf: nichts mehr zu tun; eine geänderte Datei wird nachgezogen
    again = transfer_tree(src, target)
    assert again.uploaded == [] and len(again.skipped) == 4 and again.bytes_uploaded == 0
    (src / "media/g1/a-1.vtt").write_text("WEBVTT\n\nneu\n")
    assert transfer_tree(src, target).uploaded == ["media/g1/a-1.vtt"]


def test_transfer_dry_run_and_size_check(tmp_path):
    src = tmp_path / "public"
    _tree(src)
    target = Spy(tmp_path / "ziel", "https://pub.example.org")
    res = transfer_tree(src, target, dry_run=True)
    assert len(res.uploaded) == 4 and not target.exists("media/g1/a-1.mp3")

    class Broken(Spy):
        def size(self, key):
            return 1 if self.exists(key) else None

    with pytest.raises(st.StorageError, match="statt"):
        transfer_tree(src, Broken(tmp_path / "kaputt", "https://x"))


def test_content_type():
    assert content_type("media/g/A.MP3") == "audio/mpeg"
    assert content_type("a.json") is None
