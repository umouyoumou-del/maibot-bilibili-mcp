param(
    [string]$NodeExe = "",
    [int]$HttpPort = 21082,
    [string]$ServerVersion = "11.0.1"
)

$ErrorActionPreference = "Stop"

if (-not $NodeExe) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw "未找到 node.exe，请先安装 Node.js"
    }
    $NodeExe = $nodeCommand.Source
}
$NodeExe = (Resolve-Path $NodeExe).Path
$NodeDir = Split-Path -Parent $NodeExe
$NpmCmd = Join-Path $NodeDir "npm.cmd"
if (-not (Test-Path $NpmCmd)) {
    throw "未找到 npm.cmd：$NpmCmd"
}

$ServerHome = Join-Path $env:LOCALAPPDATA "MCPBrowserKit"
New-Item -ItemType Directory -Force -Path $ServerHome | Out-Null

Write-Host "安装 @mcp-browser-kit/server@$ServerVersion 到 $ServerHome"
& $NpmCmd install --prefix $ServerHome "@mcp-browser-kit/server@$ServerVersion" --no-audit --no-fund

$MainJs = Join-Path $ServerHome "node_modules\@mcp-browser-kit\server\target\tsup\dist\main.js"
if (-not (Test-Path $MainJs)) {
    throw "Browser Kit 服务入口不存在：$MainJs"
}

$StartScript = Join-Path $ServerHome "start-http-server.ps1"
@"
`$ErrorActionPreference = 'Stop'
`$serverHome = '$ServerHome'
`$nodeExe = '$NodeExe'
`$mainJs = '$MainJs'
`$stdoutLog = Join-Path `$serverHome 'server.stdout.log'
`$stderrLog = Join-Path `$serverHome 'server.stderr.log'
`$listening = Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue
if (`$listening) { exit 0 }
Start-Process -FilePath `$nodeExe ``
    -ArgumentList @(`$mainJs, '--transport', 'http', '--http-host', '127.0.0.1', '--http-port', '$HttpPort') ``
    -WorkingDirectory `$serverHome ``
    -WindowStyle Hidden ``
    -RedirectStandardOutput `$stdoutLog ``
    -RedirectStandardError `$stderrLog
"@ | Set-Content -Path $StartScript -Encoding UTF8

$StartupVbs = Join-Path $ServerHome "start-http-server.vbs"
@"
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$StartScript""", 0, False
"@ | Set-Content -Path $StartupVbs -Encoding ASCII

$StartupDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
New-Item -ItemType Directory -Force -Path $StartupDir | Out-Null
Copy-Item -Force $StartupVbs (Join-Path $StartupDir "MCPBrowserKit-http.vbs")

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $StartScript
Start-Sleep -Seconds 2
Write-Host "Browser Kit HTTP MCP 已启动：http://127.0.0.1:$HttpPort/mcp"
Write-Host "开机启动项：$(Join-Path $StartupDir 'MCPBrowserKit-http.vbs')"
