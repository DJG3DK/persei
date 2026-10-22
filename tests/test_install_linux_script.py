"""The one-line Linux installer (site/public/install-linux.sh), run for real
against a release served from file:// and a stub AppImage."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "site" / "public" / "install-linux.sh"
pytestmark = pytest.mark.skipif(
    shutil.which("curl") is None or shutil.which("sha256sum") is None or os.uname().machine not in ("x86_64", "amd64"),
    reason="needs curl, sha256sum and an x86_64 machine")

STUB = '#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/stub-ran-with"\n'


def _release(tmp: Path, *, name="Persei_0.9.2_amd64.AppImage", body=STUB, digest=None, url=None,
             compact=False, notes=""):
    api = tmp / "api"
    (api / "tags").mkdir(parents=True)
    asset = tmp / name
    asset.write_text(body)
    sha = digest if digest is not None else hashlib.sha256(body.encode()).hexdigest()
    release = {"tag_name": "v0.9.2", "name": "Persei v0.9.2", "body": notes, "assets": [
        {"name": "latest.json", "digest": "sha256:" + "0" * 64,
         "browser_download_url": "https://github.com/DJG3DK/persei/releases/download/v0.9.2/latest.json"},
        {"name": name, "uploader": {"login": "github-actions[bot]", "url": "https://api.github.com/x"},
         "digest": f"sha256:{sha}", "browser_download_url": url or f"file://{asset}"},
    ]}
    text = json.dumps(release, separators=(",", ":")) if compact else json.dumps(release, indent=2)
    (api / "latest").write_text(text)
    (api / "tags" / "v0.9.2-rc2").write_text(text)
    return api


def _run(tmp: Path, api: Path, *args):
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "PERSEI_API": f"file://{api}"}
    r = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return r, home


def test_it_installs_where_the_app_keeps_itself_and_asks_it_for_the_menu_entry(tmp_path):
    r, home = _run(tmp_path, _release(tmp_path))
    assert r.returncode == 0, r.stderr
    target = home / ".local/share/io.tektonix.desktop/Tektonix.AppImage"
    assert target.read_text() == STUB and os.access(target, os.X_OK)
    assert (home / "stub-ran-with").read_text().strip() == "--integrate"
    assert "Open it from your app menu" in r.stdout, "no display here, so it is not started"


def test_a_named_version_is_fetched_by_its_tag(tmp_path):
    r, home = _run(tmp_path, _release(tmp_path), "--version", "v0.9.2-rc2")
    assert r.returncode == 0, r.stderr
    assert (home / ".local/share/io.tektonix.desktop/Tektonix.AppImage").exists()


def test_a_download_that_does_not_match_githubs_checksum_is_not_installed(tmp_path):
    r, home = _run(tmp_path, _release(tmp_path, digest="a" * 64))
    assert r.returncode == 1 and "does not match" in r.stderr
    assert not (home / ".local/share/io.tektonix.desktop/Tektonix.AppImage").exists()
    assert not (home / ".local/share/io.tektonix.desktop/Tektonix.AppImage.part").exists()


def test_a_release_without_the_linux_app_says_so(tmp_path):
    r, _ = _run(tmp_path, _release(tmp_path, name="Persei_0.9.2_x64-setup.exe"))
    assert r.returncode == 1 and "has no Linux app" in r.stderr


def test_only_this_repositorys_release_downloads_are_fetched(tmp_path):
    r, home = _run(tmp_path, _release(tmp_path, url="https://evil.example/Persei_0.9.2_amd64.AppImage"))
    assert r.returncode == 1 and "unexpected download address" in r.stderr
    assert not (home / "stub-ran-with").exists()


def test_a_strange_version_is_refused_before_anything_is_fetched(tmp_path):
    r, _ = _run(tmp_path, _release(tmp_path), "--version", "latest;rm -rf ~")
    assert r.returncode == 1 and "a version looks like" in r.stderr


def test_one_line_json_installs_too(tmp_path):
    """GitHub sometimes answers on one line; the plain one-liner said "that
    release has no Linux app" for v0.9.3 because of it (2026-10-07)."""
    r, home = _run(tmp_path, _release(tmp_path, compact=True))
    assert r.returncode == 0, r.stderr
    assert (home / ".local/share/io.tektonix.desktop/Tektonix.AppImage").read_text() == STUB


def test_release_notes_cannot_pose_as_an_asset(tmp_path):
    notes = 'Get "name": "Persei_9.9.9_amd64.AppImage", "browser_download_url": "https://evil.example/x"'
    r, home = _run(tmp_path, _release(tmp_path, compact=True, notes=notes))
    assert r.returncode == 0, r.stderr
    assert (home / ".local/share/io.tektonix.desktop/Tektonix.AppImage").read_text() == STUB


def test_help_works_under_a_pipe():
    """`curl ... | sh -s -- --help`: $0 is `sh` there, and the help used to
    be read from it."""
    r = subprocess.run(["sh", "-s", "--", "--help"], input=SCRIPT.read_text(), capture_output=True, text=True)
    assert r.returncode == 0 and "Install the Persei desktop app on Linux." in r.stdout


def test_a_truncated_download_runs_nothing(tmp_path):
    """I27, 2026-10-22: everything is in main, called on the last line, so a
    script cut short anywhere before it only defines functions."""
    text = SCRIPT.read_text()
    assert text.rstrip().splitlines()[-1] == 'main "$@"'
    home = tmp_path / "home"
    home.mkdir()
    cut = text[: text.rindex('main "$@"')]
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    for n in range(len(cut) // 4, len(cut), max(1, len(cut) // 9)):
        r = subprocess.run(["sh", "-s"], input=cut[:n], env=env, capture_output=True, text=True, timeout=30)
        assert "Looking up the release" not in r.stdout, n
    assert not (home / ".local").exists()


def test_every_fetch_is_https_only():
    text = SCRIPT.read_text()
    assert 'PROTO="--proto =https,file --proto-redir =https"' in text
    fetches = [line for line in text.splitlines() if "curl -f" in line and "perseiagent.ai/install" not in line]
    assert len(fetches) == 2 and all("$PROTO" in line for line in fetches)
