; Qoder2API-Hub Windows 安装包脚本（Inno Setup 6.3+）
;
; 构建前先跑 PyInstaller 得到 dist/Qoder2API-Hub.exe，然后：
;   ISCC.exe desktop/installer/qoder2api-hub.iss /DAppVersion=<版本> /DArchTag=x64
;   ISCC.exe desktop/installer/qoder2api-hub.iss /DAppVersion=<版本> /DArchTag=arm64
;
; 产物：installer-out/Qoder2API-Hub-setup-windows-<ArchTag>.exe
;   - 按用户安装（PrivilegesRequired=lowest，无需管理员/UAC）
;   - 装到 %LOCALAPPDATA%\Programs\Qoder2API-Hub（目录可写 → 桌面壳的
;     便携数据目录直接落在安装目录内，accounts/ 等随安装目录走）
;   - 卸载器只删除安装时写入的文件；运行期生成的 accounts/usage/日志
;     默认保留在原目录，不会静默清掉用户凭证

#define MyAppName "Qoder2API-Hub"
#define MyAppExe "Qoder2API-Hub.exe"
#ifndef AppVersion
#define AppVersion "0.0.0"
#endif
#ifndef ArchTag
#define ArchTag "x64"
#endif
#if ArchTag == "arm64"
; Inno 6.7 起 ArchitecturesAllowed 的 arm64 标识符是裸 "arm64"
; （"arm64compatible" 已无效，实测）；x64 线沿用 "x64compatible"。
#define InnoArch "arm64"
#else
#define InnoArch "x64compatible"
#endif
#ifndef RepoRoot
#define RepoRoot "..\.."
#endif

[Setup]
AppId={{B7F3D9A2-5C41-4E8B-9A16-3F2D8C4E7A55}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppPublisher=Qoder2API-Hub
DefaultDirName={localappdata}\Programs\{#MyAppName}
PrivilegesRequired=lowest
OutputDir={#RepoRoot}\installer-out
OutputBaseFilename=Qoder2API-Hub-setup-windows-{#ArchTag}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed={#InnoArch}
ArchitecturesInstallIn64BitMode={#InnoArch}
UninstallDisplayIcon={app}\{#MyAppExe}
CloseApplications=yes

[Languages]
; 简体中文语言包 vendor 在本目录（Inno 默认不带），随脚本相对引用
Name: "chinese"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#RepoRoot}\dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\desktop\assets\qoder2api.png"; DestDir: "{app}"; \
    Flags: ignoreversion

[Icons]
; 只建桌面快捷方式（任务里默认不勾）；不在开始菜单创建任何项/文件夹
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent
