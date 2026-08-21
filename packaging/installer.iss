; SPDX-License-Identifier: MIT
; Copyright (c) 2026 Hung Om and Päkpätät contributors
; Inno Setup script -- turns the PyInstaller folder into ONE downloadable .exe.
;
;   iscc packaging\installer.iss /DMyAppVersion=1.0.0
;
; Produces: dist\Pakpatat-Setup-1.0.0.exe
;
; Design decisions worth keeping:
;
;   PrivilegesRequired=lowest
;     Installs per-user into %LOCALAPPDATA%\Programs, so it needs NO administrator
;     password. The people who need this app are often using a shared or managed
;     laptop they are not admin on; an installer that demands admin is an
;     installer they cannot run.
;
;   AppId is a fixed GUID
;     This is what makes the next version REPLACE this one instead of adding a
;     second entry in Add/Remove Programs. Never change it between releases.
;
;   The archive is not here
;     Unless the build was made with PAKPATAT_BUNDLE_ARCHIVE=1, the installed app
;     has no content to search and says so on its own splash. See the README's notices.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Pakpatat"
#define MyAppExe  "Pakpatat.exe"

[Setup]
AppId={{7C1D4E2A-9B3F-4A6E-8D51-2F0A6C4B9E77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
VersionInfoVersion={#MyAppVersion}
AppPublisher=Independent community tool - not affiliated with UNHCR
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=..\ui\brand\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
; LZMA2/max matters here: the payload is ~500MB of Python, ONNX and a model.
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; \
  GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Pakpatat\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Open {#MyAppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Program files only. The user's data -- their archive, index and settings in
; %LOCALAPPDATA%\Pakpatat -- is deliberately LEFT BEHIND on uninstall, so
; reinstalling or upgrading does not destroy an archive that may have taken a
; long time to assemble and may not exist anywhere else on the machine.
Type: filesandordirs; Name: "{app}"

[Code]
{ Edge WebView2 is the engine the desktop window uses. It is present on all
  current Windows 10/11 installs, but not on older or stripped images, and
  without it the app falls back to opening a browser tab -- which works, but is
  not what the shortcut promises. Warn rather than silently install something. }
function InitializeSetup(): Boolean;
var
  Value: String;
  Found: Boolean;
begin
  Found :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value);
  if not Found then
    MsgBox('Microsoft Edge WebView2 was not found on this computer.' #13#10 #13#10
           'Pakpatat will still work, but it will open in your web browser '
           'instead of its own window.' #13#10 #13#10
           'To get the app window, install "Edge WebView2 Runtime" from '
           'Microsoft, then run Pakpatat again.',
           mbInformation, MB_OK);
  Result := True;
end;
