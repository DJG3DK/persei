"""app/src-tauri/windows/hooks.nsh, the Windows installer's hooks.

No Windows install runs in CI, so these pin the hooks' shape and compile
them with makensis inside a stub laid out like tauri-bundler's
installer.nsi (2.12): the hook file is included above the template's Vars,
and the macros are expanded inside `Section Install`."""
import os
import re
import shutil
import subprocess

import pytest

from agent import paths

HOOKS = paths.REPO_ROOT / "app/src-tauri/windows/hooks.nsh"
# What the template has where the hooks run. Anything else is a build that
# fails only in the tag's Windows job.
TEMPLATE_DEFINES = {"PRODUCTNAME", "MAINBINARYNAME"}
TEMPLATE_VARS = {"NoShortcutMode", "INSTDIR", "SMPROGRAMS", "DESKTOP", "R8", "R9"}
LOGICLIB = {"If", "OrIf", "OrIfNot", "AndIf", "AndIfNot", "Else", "EndIf", "FileExists"}


def _code():
    return "\n".join(line.split(";", 1)[0] for line in HOOKS.read_text().splitlines())


def _macro(code, name):
    m = re.search(rf"^!macro {name}\n(.*?)^!macroend", code, re.M | re.S)
    assert m, name
    return m.group(1)


def test_a_tektonix_to_persei_update_recreates_the_shortcuts():
    """D28, 2026-10-22: the updater runs the installer with /UPDATE, which
    makes no shortcuts, after PREINSTALL had uninstalled Tektonix and its
    shortcuts with it. POSTINSTALL makes them when that uninstall ran, and
    the Start-menu one whenever it is missing (the copies 0.9.4 stranded)."""
    code = _code()
    pre, post = _macro(code, "NSIS_HOOK_PREINSTALL"), _macro(code, "NSIS_HOOK_POSTINSTALL")
    declared = set(re.findall(r"^Var (\w+)$", code, re.M))
    assert declared == {"TektonixRemoved", "TektonixHadDesktop"}
    # Reset, then set before the uninstaller takes the .lnk files away.
    assert pre.index("StrCpy $TektonixRemoved 0") < pre.index("StrCpy $TektonixRemoved 1") < pre.index("ExecWait")
    assert pre.index('${FileExists} "$DESKTOP\\Tektonix.lnk"') < pre.index("ExecWait")
    start = '"$SMPROGRAMS\\${PRODUCTNAME}.lnk"'
    assert "${If} $TektonixRemoved = 1" in post
    assert f"${{OrIfNot}} ${{FileExists}} {start}" in post, "the repair half"
    assert f'CreateShortcut {start} "$INSTDIR\\${{MAINBINARYNAME}}.exe"' in post
    assert "${If} $TektonixHadDesktop = 1" in post
    assert 'CreateShortcut "$DESKTOP\\${PRODUCTNAME}.lnk"' in post
    assert post.lstrip().startswith("${If} $NoShortcutMode <> 1"), "/NS still means no shortcuts"


def test_the_hooks_use_only_what_the_template_defines_there():
    code = _code()
    declared = set(re.findall(r"^Var (\w+)$", code, re.M))
    assert set(re.findall(r"\$\{(\w+)\}", code)) <= TEMPLATE_DEFINES | LOGICLIB
    assert set(re.findall(r"\$(\w+)", code)) <= TEMPLATE_VARS | declared
    # A template macro may be gone in another Tauri version: always guarded.
    for name in re.findall(r"!insertmacro (\w+)", code):
        assert f"!ifmacrodef {name}" in code, name


def test_the_installer_hooks_compile(tmp_path):
    """CI installs nsis for this (the python job); elsewhere it skips."""
    makensis = shutil.which("makensis")
    if not makensis:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("makensis is missing on CI; the python job installs nsis")
        pytest.skip("makensis not installed")
    shutil.copy(HOOKS, tmp_path / "hooks.nsh")
    (tmp_path / "t.nsi").write_text(
        "Unicode true\n!include LogicLib.nsh\n!include FileFunc.nsh\n"
        '!define PRODUCTNAME "Persei"\n!define MAINBINARYNAME "persei"\n'
        '!include "hooks.nsh"\nVar NoShortcutMode\nOutFile "t.exe"\n'
        "RequestExecutionLevel user\nSection Install\n"
        "  StrCpy $NoShortcutMode 0\n"
        "  !insertmacro NSIS_HOOK_PREINSTALL\n  !insertmacro NSIS_HOOK_POSTINSTALL\n"
        "SectionEnd\n"
    )
    r = subprocess.run([makensis, "-WX", "-V2", "t.nsi"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
