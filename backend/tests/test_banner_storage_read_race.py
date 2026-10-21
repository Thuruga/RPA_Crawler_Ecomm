"""get_run must survive the brief ENOENT window of a concurrent os.replace."""

from pathlib import Path

from services.banner_storage_service import BannerStorageService


def test_get_run_retries_when_file_vanishes_mid_replace(tmp_path, monkeypatch):
    storage = BannerStorageService(tmp_path)
    run_id = "9cb24b19-eb77-4494-b413-13ace943ff66"
    (storage.runs_dir / f"{run_id}.json").write_text(
        '{"run_id": "%s", "selected_brands": ["aramis"]}' % run_id, encoding="utf-8"
    )
    original = Path.read_text
    calls = {"n": 0}

    def flaky_read_text(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    run = storage.get_run(run_id)
    assert run is not None and run.run_id == run_id
    assert calls["n"] == 2


def test_get_run_missing_file_returns_none(tmp_path):
    storage = BannerStorageService(tmp_path)
    assert storage.get_run("9cb24b19-eb77-4494-b413-13ace943ff66") is None
