; Inno Setup script (ARCHITECTURE.md §14): packages the PyInstaller one-folder
; build into deskai-<version>-windows-x64-setup.exe.
;
; Build with (from the repo root, after `pyinstaller packaging/deskai.spec`):
;   iscc packaging\windows\installer.iss /DMyAppVersion=0.1.0
;
; Signing is optional in v1 (ARCHITECTURE.md §14) — add a SignTool= line here
; once a code-signing certificate exists; until then the README explains
; SmartScreen's "More info -> Run anyway".

#define MyAppName "deskai"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "deskai"
#define MyAppExeName "deskai.exe"

[Setup]
AppId={{2E9F2E1A-7C4B-4E2C-9D2E-DESKAI000001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\..\dist\installer
OutputBaseFilename=deskai-{#MyAppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\deskai\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
