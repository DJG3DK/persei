"""The publish job adds the Linux AppImage to latest.json
(scripts/add_linux_to_updater_manifest.mjs, release.yml)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "add_linux_to_updater_manifest.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(tmp_path, files: dict[str, str], tag="v0.9.2"):
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps({"version": "0.9.2", "platforms": {
        "windows-x86_64": {"signature": "win", "url": "https://x/win.exe"}}}))
    d = tmp_path / "linux-app"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    r = subprocess.run(["node", str(SCRIPT), str(manifest), str(d), "DJG3DK/persei", tag],
                       capture_output=True, text=True)
    return r, json.loads(manifest.read_text())


def test_the_appimage_joins_the_manifest_and_windows_is_untouched(tmp_path):
    r, m = _run(tmp_path, {"Persei_0.9.2_amd64.AppImage": "bin",
                           "Persei_0.9.2_amd64.AppImage.sig": "c2lnbmF0dXJl\n",
                           "Persei_0.9.2_amd64.deb": "deb"})
    assert r.returncode == 0, r.stderr
    want = {"signature": "c2lnbmF0dXJl",
            "url": "https://github.com/DJG3DK/persei/releases/download/v0.9.2/Persei_0.9.2_amd64.AppImage"}
    assert m["platforms"]["linux-x86_64"] == want
    assert m["platforms"]["linux-x86_64-appimage"] == want
    assert m["platforms"]["windows-x86_64"] == {"signature": "win", "url": "https://x/win.exe"}


def test_an_unsigned_appimage_fails_the_release(tmp_path):
    r, m = _run(tmp_path, {"Persei_0.9.2_amd64.AppImage": "bin"})
    assert r.returncode == 1 and "no signature" in r.stderr
    assert "linux-x86_64" not in m["platforms"]


def test_no_appimage_fails_the_release(tmp_path):
    r, _ = _run(tmp_path, {"Persei_0.9.2_amd64.deb": "deb"})
    assert r.returncode == 1 and "expected one AppImage" in r.stderr


def test_a_strange_tag_is_refused(tmp_path):
    r, _ = _run(tmp_path, {"Persei_0.9.2_amd64.AppImage": "bin",
                           "Persei_0.9.2_amd64.AppImage.sig": "s"}, tag="v1;rm")
    assert r.returncode == 2
