; Inno Setup script for the Universal Audio Studio Windows installer.
;
; The version and the output filename are injected with /D flags, so the
; installer can never drift from version.py. tools/build_windows.ps1 reads
; version.py and supplies both:
;
;   ISCC.exe /DAppVersion=2.1.0 /DOutputBase=mysetup210 installer_200.iss
;
; The defaults below keep a bare `ISCC installer_200.iss` working for the
; current 2.0.0 layout. Paths are relative because Inno resolves them against
; this file: absolute paths worked on the author's machine only and would break
; the cloud build, whose checkout lives somewhere else entirely.
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif
#ifndef OutputBase
  #define OutputBase "mysetup200"
#endif

#define AppName "AudioDownloader"
#define AppExe "UniversalAudioStudio.exe"

[Setup]
AppId={{25D1293C-B08A-43CA-8FF5-70C08A988633}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
OutputDir=.
OutputBaseFilename={#OutputBase}
Compression=lzma2
SolidCompression=yes
; The installer's own icon, same asset the .exe and the app window use.
SetupIconFile=assets\tune_lab.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\UniversalAudioStudio\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\UniversalAudioStudio\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\updater_cli.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "assets\tune_lab.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "assets\tune_lab.ico"