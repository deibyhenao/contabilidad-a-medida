#ifndef AppVersion
  #error AppVersion debe indicarse desde scripts/build_release.py
#endif
#ifndef ProjectRoot
  #define ProjectRoot ".."
#endif
[Setup]
AppId={{86CC85BC-5473-497D-8D93-70C73F05D41F}
AppName=Contabilidad a Medida
AppVersion={#AppVersion}
AppPublisher=Contabilidad a Medida
AppPublisherURL=https://github.com/deibyhenao/contabilidad-a-medida
DefaultDirName={localappdata}\Programs\ContabilidadAMedida
DefaultGroupName=Contabilidad a Medida
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
WizardStyle=modern
SetupIconFile={#ProjectRoot}\assets\icono.ico
UninstallDisplayIcon={app}\ContabilidadAMedida.exe
AppMutex=Local\ContabilidadAMedida
CloseApplications=no
RestartApplications=no
Compression=lzma2
SolidCompression=yes
OutputDir={#ProjectRoot}\release
OutputBaseFilename=ContabilidadAMedida-{#AppVersion}-windows-x64-Setup
SetupLogging=yes
VersionInfoVersion={#AppVersion}.0

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo en el escritorio"; Flags: unchecked

[Files]
Source: "{#ProjectRoot}\dist\ContabilidadAMedida\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[INI]
Filename: "{app}\instalacion.ini"; Section: "Instalacion"; Key: "Version"; String: "{#AppVersion}"

[Icons]
Name: "{group}\Contabilidad a Medida"; Filename: "{app}\ContabilidadAMedida.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Contabilidad a Medida"; Filename: "{app}\ContabilidadAMedida.exe"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\ContabilidadAMedida.exe"; Description: "Abrir Contabilidad a Medida"; Flags: nowait postinstall skipifsilent
Filename: "{app}\ContabilidadAMedida.exe"; Flags: nowait; Check: EsActualizacion

[Code]
var
  DatosPage: TInputDirWizardPage;
  ConfigPath: String;

function EsActualizacion: Boolean;
begin
  Result := ExpandConstant('{param:CAMUPDATE|0}') = '1';
end;

procedure InitializeWizard;
begin
  ConfigPath := ExpandConstant('{localappdata}\ContabilidadAMedida\ubicacion.ini');
  DatosPage := CreateInputDirPage(wpSelectDir, 'Dónde guardar tus datos',
    'Elige la carpeta de tus expedientes',
    'Aquí se guardarán la base de datos, los Excel, los PDF y las copias. Las actualizaciones y la desinstalación conservarán esta información.', False, '');
  DatosPage.Add('Carpeta de datos:');
  DatosPage.Values[0] := GetIniString('Datos', 'Carpeta', ExpandConstant('{userdocs}\Contabilidad a Medida'), ConfigPath);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = DatosPage.ID) and FileExists(ConfigPath);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Datos, Programa: String;
begin
  Result := True;
  if (CurPageID = DatosPage.ID) or (CurPageID = wpSelectDir) then begin
    Datos := AddBackslash(Lowercase(ExpandFileName(DatosPage.Values[0])));
    Programa := AddBackslash(Lowercase(ExpandFileName(WizardDirValue)));
    if (Pos(Programa, Datos) = 1) or (Pos(Datos, Programa) = 1) then begin
      MsgBox('Elige carpetas separadas para el programa y los datos.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Datos, Programa: String;
begin
  Result := '';
  Datos := AddBackslash(Lowercase(ExpandFileName(DatosPage.Values[0])));
  Programa := AddBackslash(Lowercase(ExpandFileName(WizardDirValue)));
  if (Pos(Programa, Datos) = 1) or (Pos(Datos, Programa) = 1) then begin
    Result := 'La carpeta del programa y la de datos deben estar separadas.';
    Exit;
  end;
  if FileExists(ConfigPath) and not DirExists(DatosPage.Values[0]) then
    Result := 'No se encuentra la carpeta de datos elegida. Conecta la unidad antes de actualizar.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not FileExists(ConfigPath) then begin
    if not ForceDirectories(DatosPage.Values[0]) then
      RaiseException('No se pudo crear la carpeta de datos.');
    ForceDirectories(ExtractFileDir(ConfigPath));
    // BOM UTF-16LE antes de WritePrivateProfileStringW: conserva rutas en cirílico.
    if not SaveStringToFile(ConfigPath, #255#254, False) then
      RaiseException('No se pudo crear la configuración de datos.');
    if not SetIniString('Datos', 'Carpeta', DatosPage.Values[0], ConfigPath) then
      RaiseException('No se pudo guardar la ubicación de los datos.');
  end;
end;

// No hay [InstallDelete], [UninstallDelete] ni limpieza de directorios de datos.
