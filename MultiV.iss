#define AppName "MultiV"
#define AppPublisher "BR3XALITY"

#ifndef AppVersion
  #error AppVersion must be supplied by the build workflow.
#endif
[Setup]
AppId={{7C8D40C3-9EAA-49E0-8CB1-7819402B4761}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\MultiV
DefaultGroupName=MultiV
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
DisableDirPage=no
UsePreviousTasks=yes
OutputDir=release-out
OutputBaseFilename=MultiV-windows-x86_64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\logo.ico
UninstallDisplayIcon={app}\MultiV.exe
UninstallDisplayName=MultiV
CloseApplications=force
CloseApplicationsFilter=*.*
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional options:"; Flags: checkedonce

[Files]
Source: "dist\MultiV\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\data"; Permissions: users-modify

[Icons]
Name: "{group}\MultiV"; Filename: "{app}\MultiV.exe"; IconFilename: "{app}\logo.ico"; IconIndex: 0; WorkingDir: "{app}"
Name: "{userdesktop}\MultiV"; Filename: "{app}\MultiV.exe"; IconFilename: "{app}\logo.ico"; IconIndex: 0; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\MultiV.exe"; Description: "Launch MultiV"; Flags: nowait postinstall skipifsilent
