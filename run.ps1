# ---------------------------------------------------------------------------
# Shipwright 本地启动器
#
# 为什么需要这个脚本，而不是直接 `python -m mewcode`：
#
#  1. **依赖位置**。这台机器的全局 site-packages（D:\python_load\Lib\site-packages）
#     不可写，`pip install -e .` 装不进去。缺的两个包（anthropic / textual）
#     用 `pip install --target .deps` 装在了仓库内，靠 PYTHONPATH 挂上。
#
#  2. **目录名**。本仓库是"源码根 = 包根"的扁平布局，`python -m mewcode`
#     靠的是"父目录在 sys.path 上"。所以：
#       · 必须从 **D:\mewcode** 启动（父目录 D:\ 在 sys.path 上）
#       · 仓库目录名必须恰好是 `mewcode`
#     （pyproject 里 package-dir = {"mewcode": "."} 就是为此而写。）
#
#  3. **文本编码**。PYTHONUTF8=1 强制 UTF-8，避免中文提示词在这一层被
#     按 GBK 解码 —— `.mewcode/history` 里已经能看到乱码痕迹。
#
# 用法：
#   .\run.ps1                          # 启动 TUI（交互式）
#   .\run.ps1 -p "你好"                # 非交互：跑一条提示词并打印结果
#   .\run.ps1 -p "..." --mode plan     # 覆盖权限模式
#
# ⚠️ 踩过的坑，两条都是 PowerShell 的参数绑定，不是本项目的 bug：
#
#   ① 别把参数容器叫 `$Args` —— 它是 PowerShell 的**自动变量**，与显式 param
#      同名时绑定会打架。所以这里叫 `$Rest`。
#
#   ② `-p` **必须显式声明**。不声明的话，"剩余参数"收集器根本收不到它：
#      PowerShell 会先拿它去匹配自己的公共参数（`-ProgressAction`），
#      匹配上了就吞掉，于是 `.\run.ps1 -p "x"` 变成"零个参数" ——
#      结果是静默进了 TUI 并一直等输入，看起来像卡死。
#      `--mode` 这类双横线的因为匹配不上公共参数，才恰好能透传。
# ---------------------------------------------------------------------------
param(
    [string]$p,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

# 依赖查找顺序：仓库内 .deps 优先，其次才是全局 site-packages
$env:PYTHONPATH = "$RepoRoot\.deps;$RepoRoot\.."
$env:PYTHONUTF8 = "1"

Set-Location $RepoRoot

$cliArgs = @()
if ($PSBoundParameters.ContainsKey("p")) { $cliArgs += @("-p", $p) }
if ($Rest) { $cliArgs += $Rest }

python -m mewcode @cliArgs
exit $LASTEXITCODE
