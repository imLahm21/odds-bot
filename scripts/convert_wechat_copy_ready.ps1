param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$utf8 = [System.Text.UTF8Encoding]::new($false)
$source = [System.IO.File]::ReadAllText($InputPath, $utf8)

$titleMatch = [regex]::Match(
    $source,
    '<title>(.*?)</title>',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
    [System.Text.RegularExpressions.RegexOptions]::Singleline
)
$bodyMatch = [regex]::Match(
    $source,
    '<body\b[^>]*>(.*?)</body>',
    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
    [System.Text.RegularExpressions.RegexOptions]::Singleline
)

if (-not $bodyMatch.Success) {
    throw 'Input HTML does not contain a complete <body> element.'
}

$title = if ($titleMatch.Success) { $titleMatch.Groups[1].Value.Trim() } else { 'WeChat article' }
$article = $bodyMatch.Groups[1].Value.Trim()

# Remove executable/interactive markup from the user-supplied document. The
# generated page adds only its own local copy helper outside the article.
$singleLineOptions = [System.Text.RegularExpressions.RegexOptions]::IgnoreCase -bor
    [System.Text.RegularExpressions.RegexOptions]::Singleline
$article = [regex]::Replace($article, '<!--.*?-->', '', $singleLineOptions)
$article = [regex]::Replace($article, '<(script|style|iframe|object|embed|form)\b[^>]*>.*?</\1\s*>', '', $singleLineOptions)
$article = [regex]::Replace($article, '<(input|button)\b[^>]*?/?>', '', $singleLineOptions)
$article = [regex]::Replace($article, '\s+on[a-z]+\s*=\s*("[^"]*"|''[^'']*'')', '', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
$article = [regex]::Replace($article, '(href|src)\s*=\s*(["''])\s*javascript:.*?\2', '$1="#"', $singleLineOptions)

# WeChat's editor is more reliable with a two-cell table than display:flex.
$matchBarPattern = '<section style="display:flex;justify-content:space-between;align-items:center;background:#f6f7f9;border-left:4px solid #d0342c;padding:10px 14px;margin:0 0 20px"><span style="font-size:17px;font-weight:700;color:#1a1a1a">(.*?)</span><span style="font-size:13px;color:#999">(.*?)</span></section>'
$matchBarReplacement = @'
<table role="presentation" style="width:100%;border-collapse:collapse;background:#f6f7f9;border-left:4px solid #d0342c;margin:0 0 20px"><tr><td style="padding:10px 8px 10px 14px;font-size:17px;font-weight:700;color:#1a1a1a;line-height:1.5">$1</td><td style="padding:10px 14px 10px 8px;font-size:13px;color:#999;text-align:right;white-space:nowrap;line-height:1.5">$2</td></tr></table>
'@
$article = [regex]::Replace($article, $matchBarPattern, $matchBarReplacement.Trim(), $singleLineOptions)

function Get-NormalizedVisibleText([string]$html) {
    $withoutTags = [regex]::Replace($html, '<[^>]+>', ' ')
    $decoded = [System.Net.WebUtility]::HtmlDecode($withoutTags)
    return [regex]::Replace($decoded, '\s+', ' ').Trim()
}

$sourceBody = $bodyMatch.Groups[1].Value
if ((Get-NormalizedVisibleText $sourceBody) -ne (Get-NormalizedVisibleText $article)) {
    throw 'Visible article text changed during conversion; output was not written.'
}

$safeTitle = [System.Net.WebUtility]::HtmlEncode($title)
$document = @"
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>$safeTitle - WeChat copy-ready</title>
  <style>
    *{box-sizing:border-box}
    body{margin:0;background:#eef1f4;color:#1a1a1a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",Arial,sans-serif}
    .copy-toolbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:center;gap:12px;padding:12px 16px;background:rgba(255,255,255,.96);border-bottom:1px solid #dfe3e8;box-shadow:0 2px 10px rgba(0,0,0,.05)}
    .copy-toolbar button{appearance:none;border:0;border-radius:6px;padding:10px 22px;background:#07c160;color:#fff;font-size:15px;font-weight:700;cursor:pointer}
    .copy-toolbar button:hover{background:#06ad56}
    .copy-toolbar button:focus-visible{outline:3px solid rgba(7,193,96,.25);outline-offset:2px}
    .copy-status{min-width:190px;font-size:13px;color:#667085}
    .copy-hint{margin:14px auto 0;max-width:720px;padding:0 20px;color:#667085;font-size:13px;line-height:1.7;text-align:center}
    .paper{max-width:720px;margin:14px auto 40px;padding:24px 20px;background:#fff;box-shadow:0 8px 30px rgba(31,35,41,.08)}
    .paper:focus{outline:3px solid rgba(7,193,96,.18);outline-offset:3px}
    @media(max-width:760px){.copy-toolbar{justify-content:flex-start}.copy-status{min-width:0}.paper{margin:10px 0 24px;padding:20px 16px;box-shadow:none}.copy-hint{padding:0 16px}}
  </style>
</head>
<body>
  <div class="copy-toolbar" contenteditable="false">
    <button id="copy-button" type="button">&#22797;&#21046;&#23500;&#25991;&#26412;</button>
    <span id="copy-status" class="copy-status" role="status">&#22797;&#21046;&#21518;&#31896;&#36148;&#21040;&#20844;&#20247;&#21495;&#27491;&#25991;&#21306;</span>
  </div>
  <p class="copy-hint" contenteditable="false">&#30333;&#33394;&#21306;&#22495;&#21487;&#20808;&#30452;&#25509;&#20462;&#25913;&#12290;&#28857;&#20987;&#8220;&#22797;&#21046;&#23500;&#25991;&#26412;&#8221;&#65292;&#20877;&#21040;&#24494;&#20449;&#20844;&#20247;&#21495;&#32534;&#36753;&#22120;&#25353; Ctrl+V&#12290;</p>
  <main id="wechat-content" class="paper" contenteditable="true" spellcheck="false" aria-label="WeChat article body">
$article
  </main>
  <script>
    (() => {
      const article = document.getElementById('wechat-content');
      const button = document.getElementById('copy-button');
      const status = document.getElementById('copy-status');

      function selectArticle() {
        const range = document.createRange();
        range.selectNodeContents(article);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }

      button.addEventListener('click', () => {
        selectArticle();
        let copied = false;
        try {
          copied = document.execCommand('copy');
        } catch (_) {
          copied = false;
        }
        status.textContent = copied
          ? '\u5df2\u590d\u5236\uff0c\u53ef\u5230\u516c\u4f17\u53f7\u7f16\u8f91\u5668\u7c98\u8d34'
          : '\u6b63\u6587\u5df2\u9009\u4e2d\uff0c\u8bf7\u6309 Ctrl+C \u590d\u5236';
        if (copied) {
          window.getSelection().removeAllRanges();
          button.textContent = '\u590d\u5236\u6210\u529f';
          window.setTimeout(() => { button.textContent = '\u590d\u5236\u5bcc\u6587\u672c'; }, 1800);
        }
      });
    })();
  </script>
</body>
</html>
"@

$parent = Split-Path -Parent $OutputPath
if ($parent -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}
[System.IO.File]::WriteAllText($OutputPath, $document, $utf8)

$result = Get-Item -LiteralPath $OutputPath
[pscustomobject]@{
    OutputPath = $result.FullName
    Bytes = $result.Length
    VisibleTextPreserved = $true
}
