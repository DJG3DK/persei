; Persei installer hooks (tauri.conf.json bundle.windows.nsis.installerHooks).
;
; Until 0.9.4 the app was called Tektonix, and Tauri's installer keys an
; install by the product name: a Persei installer, run by the updater or by
; hand, would install beside the old copy and leave it in the Start menu.
; So before installing, run the Tektonix uninstaller silently. Silent and
; without /UPDATE it removes the old shortcuts and the Add/Remove entry; it
; never removes app data on its own (that takes the checkbox on its
; interactive page), and the data lives under the bundle identifier,
; io.tektonix.desktop, which Persei kept. The stack (compose project
; "tektonix") is untouched.

; Set by PREINSTALL for POSTINSTALL: 1 when the Tektonix uninstaller ran,
; and 1 when it had a desktop shortcut too. The hook file is included above
; the template's own Vars, so these are declared here, at the top level.
Var TektonixRemoved
Var TektonixHadDesktop

!macro NSIS_HOOK_PREINSTALL
  StrCpy $TektonixRemoved 0
  StrCpy $TektonixHadDesktop 0
  ReadRegStr $R8 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tektonix" "UninstallString"
  ${If} $R8 != ""
    StrCpy $TektonixRemoved 1
    ${If} ${FileExists} "$DESKTOP\Tektonix.lnk"
      StrCpy $TektonixHadDesktop 1
    ${EndIf}
    ReadRegStr $R9 HKCU "Software\Tektonix\Tektonix" ""
    DetailPrint "Removing Tektonix, the app's name before 0.9.4 (your data stays)"
    ${If} $R9 != ""
      ExecWait '$R8 /S _?=$R9'
      Delete "$R9\uninstall.exe"
      RMDir "$R9"
    ${Else}
      ExecWait '$R8 /S'
    ${EndIf}
  ${EndIf}
!macroend

; The other half (2026-10-22). The updater runs this installer with /UPDATE,
; and in update mode Tauri's installer creates no shortcuts: it expects the
; ones the last install made. Those were Tektonix's, and PREINSTALL just
; removed them, so every copy that auto-updated to 0.9.4 was left with no
; way to start the app. Create them here when the uninstall ran, and the
; Start-menu one whenever it is missing, which repairs the copies 0.9.4
; already stranded on their next update. /NS (no shortcuts) is honoured.
!macro NSIS_HOOK_POSTINSTALL
  ${If} $NoShortcutMode <> 1
    ${If} $TektonixRemoved = 1
    ${OrIfNot} ${FileExists} "$SMPROGRAMS\${PRODUCTNAME}.lnk"
      DetailPrint "Creating the Start menu shortcut"
      CreateShortcut "$SMPROGRAMS\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      !ifmacrodef SetLnkAppUserModelId
        !insertmacro SetLnkAppUserModelId "$SMPROGRAMS\${PRODUCTNAME}.lnk"
      !endif
    ${EndIf}
    ${If} $TektonixHadDesktop = 1
    ${AndIfNot} ${FileExists} "$DESKTOP\${PRODUCTNAME}.lnk"
      CreateShortcut "$DESKTOP\${PRODUCTNAME}.lnk" "$INSTDIR\${MAINBINARYNAME}.exe"
      !ifmacrodef SetLnkAppUserModelId
        !insertmacro SetLnkAppUserModelId "$DESKTOP\${PRODUCTNAME}.lnk"
      !endif
    ${EndIf}
  ${EndIf}
!macroend
