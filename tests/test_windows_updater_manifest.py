"""The Windows job writes latest.json from the installer and the .sig a
step of its own made (scripts/write_windows_updater_manifest.mjs,
release.yml), in the shape tauri-action wrote until 2026-10-22."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "write_windows_updater_manifest.mjs"
LINUX = ROOT / "scripts" / "add_linux_to_updater_manifest.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _run(tmp_path, files: dict[str, str], tag="v0.9.5"):
    manifest = tmp_path / "latest.json"
    d = tmp_path / "nsis"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body)
    r = subprocess.run(["node", str(SCRIPT), str(manifest), str(d), "DJG3DK/persei", tag],
                       capture_output=True, text=True)
    return r, manifest


def test_the_installer_and_its_signature_make_the_manifest(tmp_path):
    r, manifest = _run(tmp_path, {"Persei_0.9.5_x64-setup.exe": "exe",
                                  "Persei_0.9.5_x64-setup.exe.sig": "c2lnbmF0dXJl\n"})
    assert r.returncode == 0, r.stderr
    m = json.loads(manifest.read_text())
    want = {"signature": "c2lnbmF0dXJl",
            "url": "https://github.com/DJG3DK/persei/releases/download/v0.9.5/Persei_0.9.5_x64-setup.exe"}
    assert m["version"] == "0.9.5" and m["notes"] == "See CHANGELOG.md." and m["pub_date"]
    assert m["platforms"] == {"windows-x86_64": want, "windows-x86_64-nsis": want}


def test_the_linux_entries_join_it_afterwards(tmp_path):
    r, manifest = _run(tmp_path, {"Persei_0.9.5_x64-setup.exe": "exe",
                                  "Persei_0.9.5_x64-setup.exe.sig": "win"})
    assert r.returncode == 0, r.stderr
    d = tmp_path / "linux-app"
    d.mkdir()
    (d / "Persei_0.9.5_amd64.AppImage").write_text("bin")
    (d / "Persei_0.9.5_amd64.AppImage.sig").write_text("lin")
    r = subprocess.run(["node", str(LINUX), str(manifest), str(d), "DJG3DK/persei", "v0.9.5"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    platforms = json.loads(manifest.read_text())["platforms"]
    assert set(platforms) == {"windows-x86_64", "windows-x86_64-nsis", "linux-x86_64", "linux-x86_64-appimage"}


def test_an_unsigned_installer_fails_the_release(tmp_path):
    r, manifest = _run(tmp_path, {"Persei_0.9.5_x64-setup.exe": "exe"})
    assert r.returncode == 1 and "no signature" in r.stderr
    assert not manifest.exists()


def test_no_installer_or_two_fail_the_release(tmp_path):
    r, _ = _run(tmp_path, {})
    assert r.returncode == 1 and "expected one installer" in r.stderr


def test_a_strange_tag_is_refused(tmp_path):
    r, _ = _run(tmp_path, {"Persei_0.9.5_x64-setup.exe": "exe",
                           "Persei_0.9.5_x64-setup.exe.sig": "s"}, tag="v1;rm")
    assert r.returncode == 2
