!macro customInstallMode
  ${if} ${isUpdated}
    ${if} $hasPerMachineInstallation == "1"
      StrCpy $isForceMachineInstall "1"
    ${else}
      StrCpy $isForceCurrentInstall "1"
    ${endif}
  ${endif}
!macroend

!macro closeExistingAppProcesses
  DetailPrint "Closing existing ${PRODUCT_NAME} processes before install or uninstall."
  nsExec::ExecToLog `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance -ClassName Win32_Process | ? {$$_.Name -eq '${APP_EXECUTABLE_FILENAME}' -or ($$_.Path -and $$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase'))} | % { $$p = Get-Process -Id $$_.ProcessId -ErrorAction SilentlyContinue; if ($$p) { $$null = $$p.CloseMainWindow() } }"`
  Sleep 3500
  nsExec::ExecToLog `"$SYSDIR\cmd.exe" /C taskkill /F /T /IM "${APP_EXECUTABLE_FILENAME}"`
  nsExec::ExecToLog `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance -ClassName Win32_Process | ? {$$_.Path -and $$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase')} | % { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
  Sleep 1500
!macroend

!macro abortIfExistingAppProcessesRemain
  nsExec::ExecToStack `"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "if ((Get-CimInstance -ClassName Win32_Process | ? {$$_.Name -eq '${APP_EXECUTABLE_FILENAME}' -or ($$_.Path -and $$_.Path.StartsWith('$INSTDIR', 'CurrentCultureIgnoreCase'))}).Count -gt 0) { exit 0 } else { exit 1 }"`
  Pop $0
  Pop $1
  ${if} $0 == 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "${PRODUCT_NAME} is still running or protected by Windows permissions. Please close it or run this installer as administrator, then try again."
    SetErrorLevel 2
    Quit
  ${endif}
!macroend

!macro customInit
  !insertmacro closeExistingAppProcesses
  !insertmacro abortIfExistingAppProcessesRemain
!macroend

!macro customCheckAppRunning
  !insertmacro closeExistingAppProcesses
  !insertmacro abortIfExistingAppProcessesRemain
!macroend

!macro customInstall
  ${if} ${isUpdated}
  ${andIf} ${isForceRun}
    HideWindow
    ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "--updated"
    !insertmacro quitSuccess
  ${endif}
!macroend

!macro handleOldUninstallResultForUpdate
  IfErrors 0 +3
    DetailPrint "Old uninstaller could not be launched; continuing with installer cleanup."
    Return

  ${if} $R0 == 0
    Return
  ${endif}

  ${if} $R0 == 2
    DetailPrint "Old uninstaller returned 2; verifying that app processes are closed before continuing."
    !insertmacro closeExistingAppProcesses
    !insertmacro abortIfExistingAppProcessesRemain
    ClearErrors
    Return
  ${endif}

  MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"
  DetailPrint "Uninstall was not successful. Uninstaller error code: $R0."
  SetErrorLevel 2
  Quit
!macroend

!macro customUnInstallCheck
  !insertmacro handleOldUninstallResultForUpdate
!macroend

!macro customUnInstallCheckCurrentUser
  !insertmacro handleOldUninstallResultForUpdate
!macroend

!macro preserveInstallDirectory RELATIVE_DIR
  ${if} ${FileExists} "$INSTDIR\${RELATIVE_DIR}\*.*"
  ${orIf} ${FileExists} "$INSTDIR\${RELATIVE_DIR}"
    CreateDirectory "$PLUGINSDIR\aic-preserved"
    ClearErrors
    Rename "$INSTDIR\${RELATIVE_DIR}" "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}"
    ${if} ${errors}
      DetailPrint "Unable to move $INSTDIR\${RELATIVE_DIR}; leaving it in place and using targeted cleanup."
      StrCpy $R9 "0"
      ClearErrors
    ${endif}
  ${endif}
!macroend

!macro restoreInstallDirectory RELATIVE_DIR
  ${if} ${FileExists} "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}\*.*"
  ${orIf} ${FileExists} "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}"
    CreateDirectory "$INSTDIR"
    ClearErrors
    Rename "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}" "$INSTDIR\${RELATIVE_DIR}"
    ${if} ${errors}
      DetailPrint "Unable to restore $INSTDIR\${RELATIVE_DIR}; leaving preserved copy in installer temp."
      CreateDirectory "$INSTDIR\${RELATIVE_DIR}"
      CopyFiles /SILENT "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}\*.*" "$INSTDIR\${RELATIVE_DIR}"
      RMDir /r "$PLUGINSDIR\aic-preserved\${RELATIVE_DIR}"
      ClearErrors
    ${endif}
  ${endif}
!macroend

!macro removeApplicationFilesOnly
  Delete "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
  Delete "$INSTDIR\${UNINSTALL_FILENAME}"
  Delete "$INSTDIR\*.dll"
  Delete "$INSTDIR\*.pak"
  Delete "$INSTDIR\*.bin"
  Delete "$INSTDIR\*.dat"
  Delete "$INSTDIR\*.json"
  Delete "$INSTDIR\*.txt"
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
  RMDir /r "$INSTDIR\swiftshader"
!macroend

!macro customRemoveFiles
  StrCpy $R9 "1"

  !insertmacro preserveInstallDirectory "Data"
  !insertmacro preserveInstallDirectory "Canvas Project"
  !insertmacro preserveInstallDirectory "output"
  !insertmacro preserveInstallDirectory "data"
  !insertmacro preserveInstallDirectory "AI CanvasPro Files"

  SetOutPath $TEMP

  ${if} $R9 == "1"
    RMDir /r "$INSTDIR"
  ${else}
    !insertmacro removeApplicationFilesOnly
  ${endif}

  !insertmacro restoreInstallDirectory "Data"
  !insertmacro restoreInstallDirectory "Canvas Project"
  !insertmacro restoreInstallDirectory "output"
  !insertmacro restoreInstallDirectory "data"
  !insertmacro restoreInstallDirectory "AI CanvasPro Files"
!macroend
