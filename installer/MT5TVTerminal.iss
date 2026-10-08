; MT5 TV Terminal - Windows wizard installer (Inno Setup 6)
; Build:  iscc installer\MT5TVTerminal.iss      (CI: .github/workflows/mt5-tv-terminal-installer.yml)
; Per-user install (no admin prompt) so the auto-updater can upgrade silently.

#define AppName "MT5 TV Terminal"
#define AppVersion Trim(FileRead(FileOpen(AddBackslash(SourcePath) + "..\VERSION")))
#define AppPublisher "jokingtim2468-max"
#define AppURL "https://github.com/jokingtim2468-max/trading-app"

[Setup]
AppId={{6C1E7A52-3F0B-4C9E-9A57-8E2D1F4B7C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={localappdata}\Programs\MT5TVTerminal
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=MT5TVTerminal-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\installer\app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start MT5 TV Terminal when I sign in to Windows"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\VERSION"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docker-compose.yml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.dockerignore"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "..\backend\*"; DestDir: "{app}\backend"; Excludes: "__pycache__,*.pyc,.env,state,tests,.pytest_cache"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\frontend\*"; DestDir: "{app}\frontend"; Excludes: "node_modules"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docker\*"; DestDir: "{app}\docker"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\launcher\*"; DestDir: "{app}\launcher"; Flags: ignoreversion
; Self-contained Python 3.11 with every backend package pre-installed (built by CI, see workflow).
Source: "..\runtime\python\*"; DestDir: "{app}\runtime\python"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "app.ico"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "..\secrets\README.md"; DestDir: "{app}\secrets"; Flags: ignoreversion

[Dirs]
Name: "{app}\secrets"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"""; IconFilename: "{app}\installer\app.ico"; Comment: "Charts, MT5 trading and AI agents"
Name: "{autoprograms}\Stop {#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"" -Stop"; IconFilename: "{app}\installer\app.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"""; IconFilename: "{app}\installer\app.ico"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"""; IconFilename: "{app}\installer\app.ico"; Tasks: autostart

[Run]
; Interactive install: offer to launch at the end. Silent auto-update: relaunch automatically.
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"" -NoUpdate"; Description: "Launch {#AppName} now"; Flags: postinstall nowait skipifsilent
Filename: "{sys}\wscript.exe"; Parameters: """{app}\launcher\launch.vbs"" -NoUpdate"; Flags: nowait; Check: IsRelaunch

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\launcher\launch.ps1"" -Stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopApp"

[Code]
var
  ModePage: TInputOptionWizardPage;

function IsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

procedure InitializeWizard;
begin
  ModePage := CreateInputOptionPage(wpSelectTasks,
    'How should MetaTrader 5 run?', 'Choose how the trading engine starts when you open the app.',
    'Native mode runs the bridge in the background with the MetaTrader 5 terminal installed on this PC - ' +
    'nothing else to install. Docker mode runs MetaTrader 5 headless inside containers (needs Docker Desktop).',
    True, False);
  ModePage.Add('Native - use the MetaTrader 5 terminal on this PC  [recommended]');
  ModePage.Add('Docker - headless MT5 in isolated containers (experimental, needs Docker Desktop)');
  ModePage.SelectedValueIndex := 0;
end;

function ModeName: String;
begin
  { /MODE=native|docker on the command line wins (silent installs, CI smoke test). }
  Result := Lowercase(ExpandConstant('{param:MODE|}'));
  if (Result <> 'native') and (Result <> 'docker') then
    if ModePage.SelectedValueIndex = 1 then Result := 'docker' else Result := 'native';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  S: String;
  Names: TArrayOfString;
  I: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    { Keep the mode chosen on first install; silent updates never change it. }
    if (not WizardSilent) or (ExpandConstant('{param:MODE|}') <> '') or
       (not FileExists(ExpandConstant('{app}\config.json'))) then
      SaveStringToFile(ExpandConstant('{app}\config.json'), '{"mode": "' + ModeName + '"}', False);
    { docker compose needs the secret files to exist; values are entered in the app instead. }
    SetArrayLength(Names, 5);
    Names[0] := 'mt5_login'; Names[1] := 'mt5_password'; Names[2] := 'mt5_server';
    Names[3] := 'tv_sessionid'; Names[4] := 'anthropic_api_key';
    for I := 0 to 4 do
    begin
      S := ExpandConstant('{app}\secrets\') + Names[I] + '.txt';
      if not FileExists(S) then SaveStringToFile(S, '', False);
    end;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { On upgrades keep the existing mode silently. }
  Result := (PageID = ModePage.ID) and FileExists(ExpandConstant('{app}\config.json')) and WizardSilent;
end;
