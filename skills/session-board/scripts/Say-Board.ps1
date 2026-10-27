# Доска объявлений для параллельных сессий разработки: сообщить остальным об
# изменении общих параметров работы (.dev.env, состояние ИБ, расширение, ветка).
#
#   .\Say-Board.ps1 -Message "переустановил расширение" -Owner "задача 123456"
#   .\Say-Board.ps1 -Message "…" -Hours 2       # короткое объявление
#   .\Say-Board.ps1 -Message "ИБ обновляется, не заходить" -Owner "задача 123456" -Key ib
#   .\Say-Board.ps1 -Owner "задача 123456" -Close   # задача закрыта: снять всё по ней
#   .\Say-Board.ps1                              # прочитать активные
#   .\Say-Board.ps1 -Clear                       # снять все, в т. ч. чужие: только по просьбе пользователя
#
# Объявление живёт -Hours часов и уходит само: отвечать на него некому, и
# «прочитанным» его никто не помечает — сессии читают доску на входе в очередь
# к ИБ и хуком на каждом вводе пользователя.
#
# -Owner по умолчанию — задача из ветки (feature/123456-… → «задача 123456»); объявления
# одной задачи узнаются по номеру, как бы ни была подписана остальная часть.
# -Key — тема факта внутри задачи (ib, branch, бот…): новое объявление с тем же
# -Owner и -Key заменяет прежнее. ib, stop, branch — блокирующие, по умолчанию
# живут 2 ч: брошенное «не заходить» не должно висеть сутки.
param(
  [string]$Message = "",
  [string]$Owner   = "",   # чья сессия: номер задачи, имя ПФ; пусто — из ветки
  [int]$Hours      = 12,
  [string]$Key     = "",
  [switch]$Close,
  [switch]$Clear
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "_env.ps1")
. (Join-Path $PSScriptRoot "_queue.ps1")

$ib = Get-IbServer
$formatWarning = Get-IbBoardFormatWarning $ib
if ($formatWarning) { "ВНИМАНИЕ: $formatWarning" }

if ($Clear) {
  Clear-IbNotes $ib
  "Доска очищена"
  return
}

$Owner = ConvertTo-IbOwner $Owner
if (($Close -or $Message) -and -not $Owner) { $Owner = Get-IbBranchOwner }

# Сколько действующих объявлений снимет новое: агенту видно, что сработало замещение.
function Get-Replaced([string]$WithKey) {
  $id = Get-IbTagId $Owner
  @(Get-IbNotes $ib | Where-Object {
    $_.TagId -eq $id -and ($WithKey -eq $BoardCloseKey -or ($WithKey -and $_.Key -eq $WithKey))
  }).Count
}

if ($Close) {
  if (-not $Owner) { throw "Укажите -Owner: в ветке нет номера задачи, закрытию не по чему искать объявления" }
  $text = if ($Message) { $Message } else { "задача закрыта, прежние объявления по ней неактуальны" }
  $replaced = Get-Replaced $BoardCloseKey
  Add-IbNote $ib $text $Owner 2 -Key $BoardCloseKey | Out-Null
  "Тег '$Owner': снято объявлений $replaced. Само закрытие видно 2 ч — чтобы прочитавшие узнали об отмене."
  return
}

if ($Message) {
  if ($Key -match '\s') { throw "-Key — одно слово без пробелов" }
  if ($Key -eq $BoardCloseKey) { throw "Ключ close занят закрытием, используйте -Close" }
  # Без подписи у каждого вызова свой «pid N»: -Key не заменил бы прежнее объявление.
  if ($Key -and -not $Owner) { throw "Укажите -Owner: в ветке нет номера задачи, без подписи -Key не заменит прежнее объявление" }
  if ($BoardBlockingKeys -contains $Key -and -not $PSBoundParameters.ContainsKey('Hours')) { $Hours = 2 }
  $replaced = Get-Replaced $Key
  Add-IbNote $ib $Message $Owner $Hours -Key $Key | Out-Null
  $who = if ($Owner) { $Owner } else { "pid $PID (в ветке нет номера задачи — лучше задать -Owner)" }
  "Объявление на доске: тег $who, ключ $(if ($Key) { $Key } else { 'нет' }), заменено $replaced, срок $Hours ч."
  if ($Message.Length -gt 300) {
    "Длинное объявление ($($Message.Length) симв.) уходит в каждую сессию: подробности лучше вынести в файл и дать ссылку."
  }
  return
}

$notes = @(Get-IbNotes $ib)
if (-not $notes) { "Объявлений нет"; return }
foreach ($note in $notes) { Format-IbNote $note $ib }

