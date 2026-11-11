import pytest

from publish import storage as st
from tests.publish_fixtures.helpers import fake_mp3


def test_local_roundtrip(tmp_path):
    s = st.LocalStorage(tmp_path / "pub", "https://cdn.example/public/")
    src = tmp_path / "a.mp3"
    src.write_bytes(fake_mp3(2000))
    s.put_file("media/x/a.mp3", src, "audio/mpeg", st.IMMUTABLE)
    assert s.exists("media/x/a.mp3")
    assert s.size("media/x/a.mp3") == 2000
    assert (tmp_path / "pub/media/x/a.mp3").read_bytes() == src.read_bytes()
    s.put_bytes("feed.xml", b"<rss/>", "application/rss+xml")
    assert s.size("feed.xml") == 6
    s.delete("media/x/a.mp3")
    assert not s.exists("media/x/a.mp3")
    assert not (tmp_path / "pub/media").exists()  # leere Ordner räumt delete auf
    s.delete("media/x/a.mp3")  # idempotent
    assert s.size("nope") is None


def test_local_overwrite_is_atomic_and_url_quotes(tmp_path):
    s = st.LocalStorage(tmp_path, "https://cdn.example/public")
    s.put_bytes("k/ä b.txt", b"1", "text/plain")
    s.put_bytes("k/ä b.txt", b"22", "text/plain")
    assert s.size("k/ä b.txt") == 2
    assert s.url("k/ä b.txt") == "https://cdn.example/public/k/%C3%A4%20b.txt"
    assert [p.name for p in (tmp_path / "k").iterdir()] == ["ä b.txt"]  # keine Temp-Reste


@pytest.mark.parametrize("key", ["../x", "/abs", "a/../b", "", "a\\b"])
def test_local_rejects_bad_keys(tmp_path, key):
    s = st.LocalStorage(tmp_path, "https://x")
    with pytest.raises(ValueError):
        s.put_bytes(key, b"x", "text/plain")


def _moto_storage(**kw):
    s = st.S3Storage("", "podcast", "k", "s", "https://pub.example.org", **kw)
    import boto3
    boto3.client("s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s").create_bucket(Bucket="podcast")
    return s


def test_s3_roundtrip_headers(tmp_path, monkeypatch):
    from moto import mock_aws
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        s = _moto_storage()
        src = tmp_path / "a.mp3"
        src.write_bytes(fake_mp3(5000))
        s.put_file("media/g/a-1.mp3", src, "audio/mpeg", st.IMMUTABLE)
        s.put_bytes("feed.xml", b"<rss/>", "application/rss+xml; charset=utf-8", st.FEED_CACHE)
        h = s.client.head_object(Bucket="podcast", Key="media/g/a-1.mp3")
        assert h["ContentType"] == "audio/mpeg"
        assert h["CacheControl"] == "public, max-age=31536000, immutable"
        h = s.client.head_object(Bucket="podcast", Key="feed.xml")
        assert h["CacheControl"] == "public, max-age=300"
        assert h["ContentType"].startswith("application/rss+xml")
        assert s.exists("media/g/a-1.mp3") and s.size("media/g/a-1.mp3") == 5000
        assert not s.exists("fehlt") and s.size("fehlt") is None
        s.delete("media/g/a-1.mp3")
        s.delete("media/g/a-1.mp3")
        assert not s.exists("media/g/a-1.mp3")
        assert s.url("media/g/a-1.mp3") == "https://pub.example.org/media/g/a-1.mp3"


def test_s3_large_file_is_multipart(tmp_path):
    from boto3.s3.transfer import TransferConfig
    from moto import mock_aws
    with mock_aws():
        s = _moto_storage()
        s._transfer = TransferConfig(multipart_threshold=6 * 1024 * 1024, multipart_chunksize=5 * 1024 * 1024, max_concurrency=2)
        big = tmp_path / "big.mp3"
        big.write_bytes(fake_mp3(12 * 1024 * 1024))
        s.put_file("media/g/big.mp3", big, "audio/mpeg", st.IMMUTABLE)
        h = s.client.head_object(Bucket="podcast", Key="media/g/big.mp3")
        assert h["ContentLength"] == 12 * 1024 * 1024
        assert "-" in h["ETag"]  # mehrteilig: ETag endet auf -<Teile>
        assert h["ContentType"] == "audio/mpeg" and "immutable" in h["CacheControl"]


def test_s3_errors_are_german(tmp_path):
    from moto import mock_aws
    with mock_aws():
        s = st.S3Storage("", "gibt-es-nicht", "k", "s", "https://pub.example.org")
        with pytest.raises(st.StorageError, match="Hochladen"):
            s.put_bytes("a", b"x", "text/plain")


def test_s3_client_uses_r2_settings():
    s = st.S3Storage("https://acc.r2.cloudflarestorage.com", "b", "k", "s", "https://pub")
    assert s.client.meta.region_name == "auto"
    assert s.client.meta.config.signature_version == "s3v4"
    assert s.client.meta.endpoint_url == "https://acc.r2.cloudflarestorage.com"


def test_from_env_local(tmp_path):
    s = st.from_env({"PS_DATA": str(tmp_path), "PS_BASE_URL": "http://127.0.0.1:8197"})
    assert isinstance(s, st.LocalStorage)
    assert s.root == tmp_path / "public"
    assert s.url("feed.xml") == "http://127.0.0.1:8197/public/feed.xml"
    s = st.from_env({"PS_DATA": str(tmp_path), "PS_PUBLIC_BASE_URL": "https://x.example/p/"})
    assert s.url("feed.xml") == "https://x.example/p/feed.xml"


def test_from_env_s3(tmp_path):
    env = {"PS_STORAGE": "s3", "PS_S3_ENDPOINT": "https://a.r2.cloudflarestorage.com", "PS_S3_BUCKET": "b",
           "PS_S3_ACCESS_KEY": "k", "PS_S3_SECRET_KEY": "s", "PS_PUBLIC_BASE_URL": "https://pod.example.org"}
    assert isinstance(st.from_env(env), st.S3Storage)
    del env["PS_S3_SECRET_KEY"]
    with pytest.raises(st.StorageError, match="PS_S3_SECRET_KEY"):
        st.from_env(env)
    with pytest.raises(st.StorageError, match="Unbekannter"):
        st.from_env({"PS_STORAGE": "ftp"})
