from pathlib import Path

from providers.gcp.worker import upload


class FakeBlob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.uploaded_file: str | None = None

    def upload_from_filename(self, file_name: str) -> None:
        self.uploaded_file = file_name


class FakeBucket:
    def __init__(self) -> None:
        self.blobs: list[FakeBlob] = []

    def blob(self, name: str) -> FakeBlob:
        blob = FakeBlob(name)
        self.blobs.append(blob)
        return blob


def test_upload_uses_explicit_object_name(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "node.rr.warts"
    artifact.write_bytes(b"warts")
    bucket = FakeBucket()

    class FakeClient:
        @classmethod
        def from_service_account_json(cls, _credentials: str) -> "FakeClient":
            return cls()

        def get_bucket(self, name: str) -> FakeBucket:
            assert name == "results-bucket"
            return bucket

    monkeypatch.setattr(upload.storage, "Client", FakeClient)
    monkeypatch.setattr(upload, "credential_path", lambda: "credentials.json")

    upload.send_to_cloud_storage(
        str(artifact),
        "results-bucket",
        "runs/run-1/nodes/us-central1/node-a/node.rr.warts",
    )

    assert bucket.blobs[0].name == (
        "runs/run-1/nodes/us-central1/node-a/node.rr.warts"
    )
    assert bucket.blobs[0].uploaded_file == str(artifact)


def test_upload_defaults_to_source_basename(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "manifest.json"
    artifact.write_text("{}\n", encoding="utf-8")
    bucket = FakeBucket()

    class FakeClient:
        @classmethod
        def from_service_account_json(cls, _credentials: str) -> "FakeClient":
            return cls()

        def get_bucket(self, _name: str) -> FakeBucket:
            return bucket

    monkeypatch.setattr(upload.storage, "Client", FakeClient)
    monkeypatch.setattr(upload, "credential_path", lambda: "credentials.json")

    upload.send_to_cloud_storage(str(artifact), "results-bucket")

    assert bucket.blobs[0].name == "manifest.json"
