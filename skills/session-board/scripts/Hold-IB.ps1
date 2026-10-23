# Стоп-кран для параллельных сессий разработки: пока он взведён, ни одна сессия
# не начинает оборот к ИБ (обновление, печать) и ждёт команды пользователя.
#
#   .\Hold-IB.ps1                                             # статус
#   .\Hold-IB.ps1 -On -Reason "ИБ в неизвестном состоянии" -Owner "задача 123456"
#   .\Hold-IB.ps1 -Off                                        # снять
#
# Снимает стопор человек. Сессия, увидевшая его, останавливает цикл и
# докладывает — обходить или снимать самой запрещено (см. SKILL.md).
param(
  [switch]$On,
  [switch]$Off,
  [string]$Reason = "",
  [string]$Owner  = ""     # чья сессия взвела: номер задачи, имя ПФ
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot "_env.ps1")
. (Join-Path $PSScriptRoot "_queue.ps1")

if ($On -and $Off) { throw "-On и -Off вместе не имеют смысла" }

$ib   = Get-IbServer
$stop = Get-IbStop $ib

if ($On) {
  # Причина обязательна: остальные сессии не увидят ничего, кроме этого текста.
  if (-not $Reason) { throw "Укажите -Reason: остальные сессии увидят только этот текст" }
  if ($stop) { "СТОП уже взведён: $stop"; return }
  "СТОП взведён: $(Set-IbStop $ib $Reason $Owner)"
  "Сессии остановятся на входе в очередь к ИБ. Идущий сейчас оборот доработает до конца."
}
elseif ($Off) {
  if (-not $stop) { "Стопор не взведён — снимать нечего"; return }
  Clear-IbStop $ib
  "СТОП снят (был: $stop)"
}
else {
  if ($stop) { "СТОП взведён: $stop" } else { "Стопор не взведён" }
}

