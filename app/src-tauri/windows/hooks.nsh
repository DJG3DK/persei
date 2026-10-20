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
!macro NSIS_HOOK_PREINSTALL
  ReadRegStr $R8 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tektonix" "UninstallString"
  ${If} $R8 != ""
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
