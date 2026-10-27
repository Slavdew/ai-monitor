# Сторонние компоненты страницы панели

Лежат локально, из сети не грузятся. Обновляя — сменить версию, лицензию проверить, хеш пересчитать
(`Get-FileHash -Algorithm SHA256 web\<файл>`).

| Файл | Компонент | Версия | Лицензия | Источник |
|---|---|---|---|---|
| `marked.min.js` | marked — Markdown → HTML | 12.0.2 | MIT | https://github.com/markedjs/marked |
| `purify.min.js` | DOMPurify — очистка HTML | 3.1.6 | Apache-2.0 или MPL-2.0 (на выбор) | https://github.com/cure53/DOMPurify |

SHA-256 (27.10.2026):
- `marked.min.js` — `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894`
- `purify.min.js` — `c0845096a7c4a6741f362ac506c94c1c7d27dc603bcc1bf64a587f76f2dbe3a1`
