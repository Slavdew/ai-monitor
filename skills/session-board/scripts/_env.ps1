# Адрес ИБ для скриптов session-board. Доске больше ничего не нужно: файлы доски,
# очереди, журнала и стоп-крана именуются по адресу ИБ (см. _queue.ps1).
# В репозитории не должно быть путей конкретной машины — адрес берётся из
# .dev.env в корне репозитория (файл в .gitignore), первым найденным из ключей:
#   ONEC_IB_SERVER — сервер/база, напр. server:1541/base
#   INFOBASE_PATH  — общий ключ проекта, серверная ИБ: Srvr="сервер";Ref="база";
# Оба дают один и тот же файл доски: в имени файла \W заменяется на "_",
# поэтому server:1541/base и server:1541\base совпадают.

# Скилл может лежать вне репозитория (~/.claude/skills), поэтому корень репозитория
# не выводится из позиции скрипта. Ищем .dev.env: по $env:BOARD_REPO, затем вверх
# от скрипта и от текущего каталога.
function Resolve-RepoRoot {
  if ($env:BOARD_REPO -and (Test-Path (Join-Path $env:BOARD_REPO ".dev.env"))) { return $env:BOARD_REPO }
  $seen = New-Object System.Collections.Generic.List[string]
  foreach ($start in @($PSScriptRoot, (Get-Location).Path)) {
    $d = Get-Item -LiteralPath $start -ErrorAction SilentlyContinue
    while ($d) { $seen.Add($d.FullName); $d = $d.Parent }
  }
  foreach ($c in $seen) {
    if (Test-Path (Join-Path $c ".dev.env")) { return (Resolve-Path $c).Path }
  }
  # git worktree: .dev.env не версионируется и лежит только в основной копии, а у
  # worktree вместо каталога .git — файл «gitdir: <основная>/.git/worktrees/<имя>».
  foreach ($c in $seen) {
    $git = Join-Path $c ".git"
    if (-not (Test-Path $git -PathType Leaf)) { continue }
    if ((Get-Content $git -Raw) -match 'gitdir:\s*(.+?)[\\/]\.git[\\/]worktrees[\\/]') {
      if (Test-Path (Join-Path $Matches[1] ".dev.env")) { return $Matches[1] }
    }
  }
  throw "Не найден .dev.env: задайте переменную окружения BOARD_REPO с путём к репозиторию"
}

function Get-IbServer {
  $envFile = Join-Path (Resolve-RepoRoot) ".dev.env"
  $settings = @{}
  foreach ($line in Get-Content $envFile -Encoding UTF8) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$') { $settings[$Matches[1]] = $Matches[2] }
  }

  if ($settings['ONEC_IB_SERVER']) { return $settings['ONEC_IB_SERVER'] }
  if ($settings['INFOBASE_PATH'] -match 'Srvr\s*=\s*"([^"]+)"\s*;\s*Ref\s*=\s*"([^"]+)"') {
    return "$($Matches[1])/$($Matches[2])"
  }
  throw "В $envFile не задан адрес ИБ: нужен ONEC_IB_SERVER (сервер/база) или INFOBASE_PATH серверной ИБ"
}
