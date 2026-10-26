# 通过 nw.js 远程调试端口（CDP）读取 RPG Maker MZ 游戏的实时状态
# 用法：先带 --remote-debugging-port=9222 启动游戏，再运行本脚本
param([int]$Port = 9222, [string]$Expr = 'typeof $gameActors')

$ErrorActionPreference = 'Stop'

function Get-PageTarget([int]$port) {
    $list = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json/list" -TimeoutSec 5
    $pages = @($list | Where-Object { $_.type -eq 'page' })
    if (-not $pages) { throw "没有 page 目标" }
    return $pages[0]
}

function Invoke-CdpEval([string]$wsUrl, [string]$expression) {
    $ws = New-Object System.Net.WebSockets.ClientWebSocket
    $ct = [System.Threading.CancellationToken]::None
    $ws.ConnectAsync([Uri]$wsUrl, $ct).GetAwaiter().GetResult()
    $payload = @{
        id     = 1
        method = 'Runtime.evaluate'
        params = @{ expression = $expression; returnByValue = $true; awaitPromise = $false }
    } | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $seg = New-Object System.ArraySegment[byte] -ArgumentList @(,$bytes)
    $ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).GetAwaiter().GetResult()

    $buf = New-Object byte[] 65536
    $sb = New-Object System.Text.StringBuilder
    do {
        $rseg = New-Object System.ArraySegment[byte] -ArgumentList @(,$buf)
        $res = $ws.ReceiveAsync($rseg, $ct).GetAwaiter().GetResult()
        [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buf, 0, $res.Count))
    } while (-not $res.EndOfMessage)
    $ws.Dispose()
    return $sb.ToString()
}

$page = Get-PageTarget $Port
Write-Output ("目标页面: {0}  ({1})" -f $page.title, $page.id)
$raw = Invoke-CdpEval $page.webSocketDebuggerUrl $Expr
try {
    $obj = $raw | ConvertFrom-Json
    if ($obj.result.exceptionDetails) {
        Write-Output "JS 异常: $($obj.result.exceptionDetails.text) $($obj.result.exceptionDetails.exception.description)"
    } else {
        Write-Output "表达式: $Expr"
        Write-Output "结果: $($obj.result.result.value | ConvertTo-Json -Depth 4 -Compress)"
    }
} catch {
    Write-Output "原始返回: $raw"
}
