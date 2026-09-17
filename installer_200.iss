; Inno Setup 2.0.0
#define AppName "AudioDownloader"
#define AppVersion "2.0.0"
#define AppExe "UniversalAudioStudio.exe"

[Setup]
AppId={{25D1293C-B08A-43CA-8FF5-70C08A988633}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
OutputDir=C:\Users\antho\Downloads\scripts\path
OutputBaseFilename=mysetup200
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "C:\Users\antho\Downloads\scripts\path\dist\UniversalAudioStudio\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "C:\Users\antho\Downloads\scripts\path\dist\UniversalAudioStudio\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "C:\Users\antho\Downloads\scripts\path\dist\updater_cli.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"