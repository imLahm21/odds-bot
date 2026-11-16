<#
.SYNOPSIS
  把 OpenRouter 的 4 条 key 写进 .env 的 LLM_ROUTE_ENDPOINTS（op_gpt / op_gemini / op_claude / op_grok）。

.DESCRIPTION
  - 逐个隐藏输入 key（不回显、不进命令历史、不出现在命令行参数里）；直接回车 = 跳过该组、保留原值。
  - 同一组已有 op_* 条目时整条替换；其他组（ik_* / cpa_*）和文件其余内容一字不改。
  - 改之前各自备份为 .env.bak-<时间>-openrouter；写入用「临时文件 + 原子替换」，
    服务器上的 .env 监视器只会在完整文件就位后重启一次 odds-bot。
  - 默认同时更新本地 .env 和服务器 .env（两边保持一致）；-SkipLocal / -SkipServer 可只改一边。

.EXAMPLE
  # 仓库在 Google Drive（G:）上，RemoteSigned 会把它当远程脚本拒绝；只对这一次运行放行：
  powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\set_openrouter_keys.ps1" `
      -SshTarget ubuntu@<服务器IP> -SshPort <端口>
#>
param(
    [string]$SshTarget = $env:ODDS_SSH_TARGET,
    [int]$SshPort = 0,
    [string]$RemoteEnv = '/home/ubuntu/odds_bot/.env',
    [string]$LocalEnv = '',
    [switch]$SkipLocal,
    [switch]$SkipServer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# 管道传给 python / ssh 的程序文本按 UTF-8（无 BOM）编码；内容本身只含 ASCII。
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$BaseUrl = 'https://openrouter.ai/api/v1'
$Groups = [ordered]@{
    'op_gpt'    = 'OP-GPT'
    'op_gemini' = 'OP-Gemini'
    'op_claude' = 'OP-Claude'
    'op_grok'   = 'OP-Grok'
}

if (-not $LocalEnv) {
    $LocalEnv = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
}
if ($SshPort -le 0) {
    if ($env:ODDS_SSH_PORT) { $SshPort = [int]$env:ODDS_SSH_PORT } else { $SshPort = 22 }
}
if ($SkipLocal -and $SkipServer) {
    throw '-SkipLocal 和 -SkipServer 不能同时使用。'
}
if (-not $SkipLocal -and -not (Test-Path -LiteralPath $LocalEnv)) {
    throw "找不到本地 .env：$LocalEnv（可用 -LocalEnv 指定，或加 -SkipLocal）"
}
if (-not $SkipServer -and -not $SshTarget) {
    $SshTarget = Read-Host '服务器 SSH 目标（如 ubuntu@1.2.3.4）'
    if (-not $SshTarget) { throw '未提供服务器 SSH 目标（或加 -SkipServer 只改本地）。' }
}

function ConvertTo-PlainText([Security.SecureString]$Secure) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host '在 OpenRouter 后台为下面 4 个组各建一条 key（每组一条、互不相同），逐个粘贴。'
Write-Host '输入不会显示；直接回车 = 跳过该组（保留 .env 里原来的值）。'
$entries = [ordered]@{}
$seen = @{}
foreach ($group in $Groups.Keys) {
    $key = (ConvertTo-PlainText (Read-Host "$group 的 key" -AsSecureString)).Trim()
    if (-not $key) { Write-Host "  · $group 跳过"; continue }
    if ($key -notmatch '^[A-Za-z0-9._\-]+$') {
        throw "$group 的 key 含空格、| 或逗号等非法字符，请检查是否复制完整。"
    }
    if ($seen.ContainsKey($key)) {
        throw "$group 与 $($seen[$key]) 用了同一条 key；同一 key 不能跨组，请为每组单独建 key。"
    }
    if (-not $key.StartsWith('sk-or-')) {
        Write-Warning "$group 的 key 不是 sk-or- 开头，确认是 OpenRouter 的 key 吗？"
    }
    $seen[$key] = $group
    $entries[$group] = "$group|$key|$BaseUrl|$($Groups[$group])"
    Write-Host "  · $group 已录入（长度 $($key.Length)）"
}
if ($entries.Count -eq 0) { Write-Host '没有录入任何 key，未做改动。'; return }

# 更新逻辑用 Python 实现（本地与服务器共用同一段代码）；key 只经标准输入传递。
$helper = @'
import json, os, shutil, sys, tempfile, time
ORDER = ["op_gpt", "op_gemini", "op_claude", "op_grok"]
PREFIX = "LLM_ROUTE_ENDPOINTS="

def main(payload):
    entries = json.loads(payload)
    path = sys.argv[1]
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = (raw[3:] if bom else raw).decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    hits = [i for i, line in enumerate(lines) if line.startswith(PREFIX)]
    if len(hits) != 1:
        sys.exit("ERROR: expected exactly one LLM_ROUTE_ENDPOINTS line in " + path)
    i = hits[0]
    value = lines[i][len(PREFIX):]
    if value[:1] in ("'", '"'):
        sys.exit("ERROR: quoted LLM_ROUTE_ENDPOINTS is not supported, edit it by hand")
    items = [p.strip() for p in value.split(",") if p.strip()]
    kept = [p for p in items if p.split("|", 1)[0].strip() not in entries]
    used = {p.split("|")[1].strip() for p in kept if p.count("|") >= 3}
    for group in entries:
        if entries[group].split("|")[1] in used:
            sys.exit("ERROR: the key for %s is already used by another group" % group)
    added = [entries[g] for g in ORDER if g in entries]
    lines[i] = PREFIX + ",".join(kept + added)
    data = (b"\xef\xbb\xbf" if bom else b"") + newline.join(lines).encode("utf-8")
    backup = "%s.bak-%s-openrouter" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".env.tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    print("OK %s: wrote %s; replaced %d old op_* entries; %d entries total; backup %s" % (
        path, ",".join(g for g in ORDER if g in entries), len(items) - len(kept),
        len(kept) + len(added), os.path.basename(backup)))
'@
$payload = ConvertTo-Json -InputObject $entries -Compress
$program = $helper + "`nmain(r'''" + $payload + "''')`n"

try {
    if (-not $SkipLocal) {
        Write-Host "`n[本地] $LocalEnv"
        $python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($python) {
            $program | & $python.Path - $LocalEnv
        } else {
            $program | & py -3 - $LocalEnv
        }
        if ($LASTEXITCODE -ne 0) { throw "本地 .env 更新失败（退出码 $LASTEXITCODE），服务器未改动。" }
    }
    if (-not $SkipServer) {
        Write-Host "`n[服务器] ${SshTarget}:$RemoteEnv"
        $program | ssh -p $SshPort -o ConnectTimeout=15 $SshTarget python3 - $RemoteEnv
        if ($LASTEXITCODE -ne 0) { throw "服务器 .env 更新失败（退出码 $LASTEXITCODE）。" }
        Write-Host '等待 odds-bot 按 .env 变化自动重启…'
        ssh -p $SshPort -o ConnectTimeout=15 $SshTarget 'sleep 8; systemctl is-active odds-bot'
    }
} finally {
    $program = $null; $payload = $null; $entries = $null; $seen = $null
}
Write-Host "`n完成。部署含 op_* 组的新代码后，在 /llm 面板点「🧪 op_gpt」等按钮逐组测试。"
