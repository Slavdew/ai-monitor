# Чтение доски объявлений и журнала оборотов к ИБ: что происходило, кто держал
# очередь, где падало. Только чтение — ничего не меняет.
#
#   .\Show-Board.ps1                          # активные объявления + журнал за сутки
#   .\Show-Board.ps1 -Board                   # только объявления
#   .\Show-Board.ps1 -Task "123456"           # всё по задаче (в журнале — за -Days суток)
#   .\Show-Board.ps1 -Days 3 -Tail 100        # больше истории
#   .\Show-Board.ps1 -Errors                  # только ошибки и стопоры
#
# Журнал ведёт Write-IbQueueLog построчно: дата, pid, событие. Ротация — одно
# поколение (<ИБ>.log.1), поэтому -Days глубже недели обычно упирается в неё.
param(
  [string]$Task = "",          # номер задачи (сравнивается по номеру) или подстрока: имя ПФ, текст события
  [int]$Days    = 1,           # за сколько последних суток смотреть журнал
  [int]$Tail    = 40,          # сколько последних строк журнала показать
  [switch]$Board,              # только доска, журнал не читать
  [switch]$Errors,             # только ошибки, стопоры и таймауты
  [switch]$All                 # и замещённые, и снятые закрытием объявления
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "_env.ps1")
. (Join-Path $PSScriptRoot "_queue.ps1")

$ib = Get-IbServer
$formatWarning = Get-IbBoardFormatWarning $ib
if ($formatWarning) { "ВНИМАНИЕ: $formatWarning" }

# --- доска ---------------------------------------------------------------------

$notes = @(Get-IbNotes $ib -All:$All)
# Номер задачи — по номеру, как теги доски: «задача 123456» находит и «123456 переверстка».
$taskNumber = Get-IbTaskNumber $Task
if ($taskNumber) { $notes = @($notes | Where-Object { $_.TagId -eq $taskNumber -or $_.Text -like "*$taskNumber*" }) }
elseif ($Task) { $notes = @($notes | Where-Object { $_.Owner -like "*$Task*" -or $_.Text -like "*$Task*" }) }

"ДОСКА ($ib): активных объявлений $($notes.Count)"
foreach ($note in $notes) {
  "  " + (Format-IbNote $note $ib) + (" [до {0:dd.MM HH:mm}]" -f $note.Until) + $(if ($note.Mine) { " (своё)" } else { "" })
}

$stop = Get-IbStop $ib
if ($stop) { "СТОП взведён: $stop" }

if ($Board) { return }

# --- журнал --------------------------------------------------------------------

$since = (Get-Date).AddDays(-$Days)
$rows  = @()
$logTask = if ($taskNumber) { $taskNumber } else { $Task }   # в журнале — подстрокой

foreach ($suffix in @(".log.1", ".log")) {            # старое поколение первым
  $path = Get-IbQueuePath $ib $suffix
  if (-not (Test-Path $path)) { continue }
  foreach ($line in (Get-Content $path -Encoding UTF8)) {
    $parts = $line -split "`t", 3
    if ($parts.Count -lt 3) { continue }
    $at = $parts[0] -as [datetime]
    if (-not $at -or $at -lt $since) { continue }
    if ($Task -and $parts[2] -notlike "*$logTask*") { continue }
    if ($Errors -and $parts[2] -notmatch 'ОШИБКА|СТОП|не освободилась|код \d') { continue }
    $rows += [pscustomobject]@{ Время = $at; Pid = $parts[1]; Событие = $parts[2] }
  }
}

$filter = @()
if ($Task)   { $filter += "задача '$Task'" }
if ($Errors) { $filter += "только ошибки" }
$title = "ЖУРНАЛ за $Days сут" + $(if ($filter) { " (" + ($filter -join ", ") + ")" } else { "" })
"`n${title}: событий $($rows.Count)" + $(if ($rows.Count -gt $Tail) { ", показаны последние $Tail" } else { "" })

$rows | Select-Object -Last $Tail |
  Format-Table @{ N = "Время"; E = { "{0:dd.MM HH:mm:ss}" -f $_.Время } }, Pid, Событие -AutoSize -Wrap
