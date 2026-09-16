from fractions import Fraction
from io import BytesIO
from pathlib import Path
import subprocess
import os
import time
from unittest.mock import Mock

from fastapi import HTTPException
import pytest

from src.apps.identity import UserContext
from src.apps.recreation import analysis
from src.apps.recreation.service import RecreationService


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return RecreationService(UserContext("user", "profile", "User", ""))


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "input.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=96x64:r=24:d=1",
        "-f", "lavfi", "-i", "color=blue:s=96x64:r=24:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,select=not(eq(mod(n\\,3)\\,1))[v]",
        "-map", "[v]", "-fps_mode", "vfr", "-c:v", "libx264", str(path),
    ], check=True, timeout=30)
    return path


def register(service, video):
    with video.open("rb") as stream:
        return service.register(stream, video.name)


def completed(service, video):
    project = register(service, video)
    queued = service.start(project["id"], project["revision"])
    service.process(project["id"], queued["analysis_id"])
    result = service.get(project["id"])
    assert result["status"] == "review", result["error"]
    return result


def test_real_vfr_analysis_and_confirmation(service, video):
    project = completed(service, video)
    data = project["analysis"]
    pts = data["frame_pts"]
    tb = Fraction(data["time_base"])
    assert len({b - a for a, b in zip(pts, pts[1:])}) > 1
    assert data["audio_streams"] == 0
    assert data["end_pts"] > pts[-1]
    assert len(data["candidates"]) == 1
    candidate = data["candidates"][0]
    assert candidate["pts"] * tb == 1
    assert candidate["before_pts"] == pts[pts.index(candidate["pts"]) - 1]
    from PIL import Image
    with Image.open(Path("output") / candidate["before_url"]) as image:
        assert image.getpixel((10, 10))[0] > 200
    with Image.open(Path("output") / candidate["after_url"]) as image:
        assert image.getpixel((10, 10))[2] > 200
    assert len({s["pts"] for s in data["samples"]}) == len(data["samples"])
    confirmed = service.confirm(project["id"], project["revision"], project["analysis_id"], [candidate["pts"]])
    assert confirmed["status"] == "confirmed"
    assert confirmed["timeline"]["shots"][-1]["end_pts"] == data["end_pts"]
    assert RecreationService(service.user).get(project["id"])["timeline"] == confirmed["timeline"]
    with pytest.raises(HTTPException) as exc:
        service.confirm(project["id"], project["revision"], project["analysis_id"], [])
    assert exc.value.status_code == 409


def test_cross_owner_cannot_read_start_or_confirm(service, video):
    record = register(service, video)
    stranger = RecreationService(UserContext("other", "other-profile", "Other", ""))
    assert stranger.list() == []
    for call in [lambda: stranger.get(record["id"]), lambda: stranger.start(record["id"], 0),
                 lambda: stranger.confirm(record["id"], 0, "x", [])]:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 404


def test_duplicate_start_and_expired_attempt_cannot_publish(service, video, monkeypatch):
    record = register(service, video)
    first = service.start(record["id"], 0)
    with pytest.raises(HTTPException):
        service.start(record["id"], first["revision"])
    with service.db() as db:
        state = service._get(db, record["id"])
        state["started_at"] = time.time() - 601
        service._save(db, state)
    failed = service.get(record["id"])
    assert failed["status"] == "failed"
    second = service.start(record["id"], failed["revision"])
    runner = Mock()
    monkeypatch.setattr("src.apps.recreation.service.analyze", runner)
    service.process(record["id"], first["analysis_id"])
    runner.assert_not_called()
    assert service.get(record["id"])["analysis_id"] == second["analysis_id"]


def test_corruption_and_explicit_retry(service):
    record = service.register(BytesIO(b"not video"), "bad.mp4")
    first = service.start(record["id"], 0)
    service.process(record["id"], first["analysis_id"])
    failed = service.get(record["id"])
    assert failed["status"] == "failed" and failed["error"]
    second = service.start(record["id"], failed["revision"])
    assert second["attempt"] == 2 and second["analysis_id"] != first["analysis_id"]


def test_source_mutation_rejected(service, video):
    record = completed(service, video)
    (Path("output") / record["source_url"]).write_bytes(b"changed")
    with pytest.raises(HTTPException, match="fingerprint"):
        service.confirm(record["id"], record["revision"], record["analysis_id"], [])
    retry = service.start(record["id"], record["revision"])
    service.process(record["id"], retry["analysis_id"])
    assert "fingerprint" in service.get(record["id"])["error"]


def test_symlink_source_escape_fails(service, video):
    record = register(service, video)
    source = Path("output") / record["source_url"]
    source.unlink()
    source.symlink_to(video)
    queued = service.start(record["id"], 0)
    service.process(record["id"], queued["analysis_id"])
    assert "outside" in service.get(record["id"])["error"]


def test_media_search_is_owner_scoped_and_cursor_paginated(service, video):
    record = register(service, video)
    result = service.search_media(query="input", kind="source_video", limit=1)
    assert len(result["items"]) == 1
    assert result["items"][0]["media_id"] == record["source_media_id"]
    assert service.search_media(project_id="missing")["items"] == []


def test_timeout_is_explicit(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("ffmpeg", 90)))
    with pytest.raises(ValueError, match="timed out"):
        analysis.run(["ffmpeg"])


def test_non_frame_or_unsorted_cuts_rejected(service, video):
    record = completed(service, video)
    frames = record["analysis"]["frame_pts"]
    for cuts in [[frames[0]], [frames[1] + 1], [frames[2], frames[1]], [frames[1], frames[1]]]:
        with pytest.raises(HTTPException) as exc:
            service.confirm(record["id"], record["revision"], record["analysis_id"], cuts)
        assert exc.value.status_code == 422


def test_api_registration_analysis_evidence_and_confirmation(service, video, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.apps.recreation.api import router
    from src.apps.studio_access import require_studio_user, verify_studio_media
    from urllib.parse import urlsplit, parse_qs
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/recreation/projects").status_code == 401
    app.dependency_overrides[require_studio_user] = lambda: service.user
    monkeypatch.setenv("LUMENX_MEDIA_SIGNING_KEY", "test-only-signing-key")
    with video.open("rb") as source:
        response = client.post("/recreation/projects", files={"file": ("original.mp4", source, "video/mp4")})
    assert response.status_code == 201
    project = response.json()
    signed = urlsplit(project["source_url"])
    owner, path = signed.path.removeprefix("/studio/media/").split("/", 1)
    query = parse_qs(signed.query)
    assert Path(verify_studio_media(owner, path, int(query["expires"][0]), query["signature"][0])).is_file()
    url = f"/recreation/projects/{project['id']}"
    assert client.post(url + "/analyze", json={"revision": 0}).status_code == 202
    project = client.get(url).json()
    assert project["status"] == "review"
    cut = project["analysis"]["frame_pts"][2]
    pair = client.post(url + "/evidence", json={"analysis_id": project["analysis_id"], "pts": cut})
    assert pair.status_code == 200
    assert pair.json()["before_url"].startswith("/studio/media/")
    assert client.put(url + "/timeline", json={"revision": project["revision"],
        "analysis_id": project["analysis_id"], "cut_pts": [cut + 0.5]}).status_code == 422
    assert client.put(url + "/timeline", json={"revision": project["revision"],
        "analysis_id": project["analysis_id"], "cut_pts": [cut]}).status_code == 200


@pytest.mark.skipif(not os.getenv("LUMENX_RECREATION_SAMPLE"), reason="Optional authorized source-video acceptance")
def test_authorized_fifteen_second_source(service):
    record = completed(service, Path(os.environ["LUMENX_RECREATION_SAMPLE"]))
    data = record["analysis"]
    tb = Fraction(data["time_base"])
    assert abs(data["duration_seconds"] - 15) < 0.001
    assert len({b - a for a, b in zip(data["frame_pts"], data["frame_pts"][1:])}) > 1
    cuts = []
    for value in ["4.016667", "9.083333", "10.400000"]:
        point = min(data["frame_pts"], key=lambda p: abs((p - data["start_pts"]) * tb - Fraction(value)))
        assert abs((point - data["start_pts"]) * tb - Fraction(value)) < Fraction(1, 1000000)
        cuts.append(point)
    result = service.confirm(record["id"], record["revision"], record["analysis_id"], cuts)
    assert len(result["timeline"]["shots"]) == 4
    assert [c["pts"] for c in result["timeline"]["cuts"]] == cuts
    assert data["end_pts"] > data["frame_pts"][-1]
