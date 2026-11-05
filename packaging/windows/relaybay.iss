; Inno Setup 6 script: a per-user installer of the PyInstaller onedir build, no admin rights.
;
;   uv run pyinstaller --noconfirm packaging/relaybay.spec
;   iscc /DVersion=0.6.0 packaging\windows\relaybay.iss
;
; Output: dist\relaybay-<version>-windows-x64-setup.exe. The data folder (%LOCALAPPDATA%\relaybay)
; and the settings (desktop.toml) are outside {app}, so an uninstall keeps them.

#ifndef Version
  #error Pass the version: iscc /DVersion=X.Y.Z packaging\windows\relaybay.iss
#endif
#ifndef BuildDir
  #define BuildDir "..\..\dist\relaybay"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

; Mirrors APP_MUTEX in src/relaybay/desktop/single_instance.py; the program holds it while it runs.
#define AppMutexName "relaybay-desktop"
; The AppId below without its escaping brace: the uninstall key is "<AppId>_is1".
#define AppIdGuid "{C9B56C49-D24A-4E1D-9496-A562F9234644}"
#define RunKey "Software\Microsoft\Windows\CurrentVersion\Run"

[Setup]
AppId={{C9B56C49-D24A-4E1D-9496-A562F9234644}
AppName=relaybay
AppVersion={#Version}
AppVerName=relaybay {#Version}
AppPublisher=relaybay contributors
AppPublisherURL=https://github.com/Codenator81/relaybay
AppSupportURL=https://github.com/Codenator81/relaybay/issues
AppUpdatesURL=https://github.com/Codenator81/relaybay/releases
VersionInfoVersion={#Version}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\relaybay
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=relaybay-{#Version}-windows-x64-setup
SetupIconFile=..\icons\relaybay.ico
UninstallDisplayIcon={app}\relaybay-desktop.exe
UninstallDisplayName=relaybay
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; No AppMutex: Inno checks it at start-up with an OK/Cancel box, and its order against our [Code]
; is not documented. PrepareToInstall and InitializeUninstall send `desktop --quit` and wait on the
; program's mutex instead; the Restart Manager (CloseApplications) handles one that does not answer.
CloseApplications=yes
RestartApplications=no
ChangesEnvironment=yes

[Tasks]
Name: "autostart"; Description: "Start relaybay at login"; Flags: unchecked
Name: "addtopath"; Description: "Add relaybay to PATH for this user"

[InstallDelete]
; A new version replaces the bundled libraries as a whole: no stale modules from the old one.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\relaybay"; Filename: "{app}\relaybay-desktop.exe"

[Run]
; The program's own code writes desktop.toml and the Run entry, as its Settings window does.
Filename: "{app}\relaybay.exe"; Parameters: "desktop config --autostart on"; Flags: runhidden; Tasks: autostart
Filename: "{app}\relaybay-desktop.exe"; Description: "Start relaybay"; Flags: nowait postinstall skipifsilent

[Code]
const
  EnvKey = 'Environment';

var
  WasRunning: Boolean;

{ Ask a running program to quit (`relaybay desktop --quit`) and wait up to 30 s for its mutex.
  True when none runs any more. }
function QuitRunningProgram(Dir: String): Boolean;
var
  ResultCode, Waited: Integer;
begin
  Result := True;
  if not CheckForMutexes('{#AppMutexName}') then
    exit;
  if FileExists(Dir + '\relaybay.exe') then
    Exec(Dir + '\relaybay.exe', 'desktop --quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Waited := 0;
  while CheckForMutexes('{#AppMutexName}') and (Waited < 60) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
  Result := not CheckForMutexes('{#AppMutexName}');
end;

{ After the wizard, before any file is written, so a cancelled wizard leaves the program running.
  A program that does not answer is left to the Restart Manager (CloseApplications). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir: String;
begin
  Result := '';
  WasRunning := CheckForMutexes('{#AppMutexName}');
  if not WasRunning then
    exit;
  if not RegQueryStringValue(HKCU,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdGuid}_is1', 'InstallLocation', Dir) then
    Dir := WizardDirValue;
  QuitRunningProgram(RemoveBackslashUnlessRoot(Dir));
end;

function InitializeUninstall(): Boolean;
begin
  Result := QuitRunningProgram(ExpandConstant('{app}'));
  if not Result then
    SuppressibleMsgBox('relaybay is still running. Quit it from its tray icon, then uninstall again.',
      mbError, MB_OK, IDOK);
end;

function PathHas(Paths, Dir: String): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') > 0;
end;

procedure AddToUserPath(Dir: String);
var
  Paths: String;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    Paths := '';
  if PathHas(Paths, Dir) then
    exit;
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Paths + Dir);
end;

procedure RemoveFromUserPath(Dir: String);
var
  Paths: String;
  P: Integer;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', Paths) then
    exit;
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);
  RegWriteExpandStringValue(HKCU, EnvKey, 'Path', Copy(Paths, 2, Length(Paths) - 2));
end;

{ Remove the login entry only when it starts this installation (not a development checkout). }
procedure RemoveAutostart(Dir: String);
var
  Command: String;
begin
  if RegQueryStringValue(HKCU, '{#RunKey}', 'relaybay', Command) and
    (Pos(Uppercase(Dir) + '\', Uppercase(Command)) > 0) then
    RegDeleteValue(HKCU, '{#RunKey}', 'relaybay');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  if WizardIsTaskSelected('addtopath') then
    AddToUserPath(ExpandConstant('{app}'));
  { An upgrade over a running program starts the new version again, hidden in the tray. }
  if WasRunning then
    Exec(ExpandConstant('{app}\relaybay-desktop.exe'), '--background', '', SW_SHOWNORMAL, ewNoWait,
      ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveAutostart(ExpandConstant('{app}'));
    RemoveFromUserPath(ExpandConstant('{app}'));
  end;
end;
