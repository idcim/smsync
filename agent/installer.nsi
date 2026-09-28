; SMSync Agent Windows 安装包脚本（用 makensis.exe 编译，见 build_installer.sh）
!define APP_NAME "SMSync Agent"
!define APP_VERSION "1.2.0"
!define APP_EXE "smsync-agent.exe"
!define APP_ID "SMSyncAgent"
!define PUBLISHER "SMSync"

!include "MUI2.nsh"

; 生成 Unicode 安装包，否则中文组件名/描述会显示成问号
Unicode true

Name "${APP_NAME}"
OutFile "dist\SMSyncAgent-Setup-${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\SMSyncAgent"
RequestExecutionLevel admin
ShowInstDetails show

!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Section "主程序（必选）" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "dist\${APP_EXE}"
  ; 已有 config.ini 则保留用户配置，否则装模板（用户再填设备码/端口）
  IfFileExists "$INSTDIR\config.ini" config_done
  File "/oname=config.ini" "config.example.ini"
config_done:

  CreateDirectory "$SMPROGRAMS\SMSync"
  CreateShortcut "$SMPROGRAMS\SMSync\SMSync Agent.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortcut "$SMPROGRAMS\SMSync\卸载 SMSync Agent.lnk" "$INSTDIR\uninstall.exe"

  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "开机自动启动" SecAutoRun
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_ID}" "$INSTDIR\${APP_EXE}"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "SMS 采集代理：监听 EC20 新短信并上传到服务器"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutoRun} "登录 Windows 后自动运行 SMSync Agent"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  DeleteRegValue HKLM "Software\Microsoft\Windows\CurrentVersion\Run" "${APP_ID}"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  ; 保留 config.ini 与 %APPDATA%\SMSyncAgent 中的队列/日志
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\SMSync\*.lnk"
  RMDir "$SMPROGRAMS\SMSync"
SectionEnd
