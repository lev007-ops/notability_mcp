# notability-mcp

Один контейнер, один порт (за Caddy):

| Путь | Что | Аутентификация |
|---|---|---|
| `/mcp` | MCP-сервер (Streamable HTTP), только чтение | OAuth 2.1 + PKCE, вход по паролю на `/login` |
| `/dav/` | WebDAV для автобэкапа Notability | Basic auth (поверх TLS от Caddy) |
| `/healthz` | healthcheck | — |

Инструменты MCP: `list_notes`, `recent_notes`, `search_notes`, `read_note`,
`view_note_pages` (рендер страниц PDF в картинки, чтобы Claude читал рукописный текст),
`get_transcript`, `transcription_status`.

## Установка на сервере

```bash
git clone … notability-mcp && cd notability-mcp
cp .env.example .env
mkdir -p data                       # токены, кэш, расшифровки, модели whisper
id -u; id -g                        # → PUID / PGID в .env
nano .env                           # PUBLIC_URL, NOTABILITY_DIR, PUID/PGID
docker compose build
docker compose run --rm notability-mcp hash-password   # → LOGIN_PASSWORD_HASH в .env
docker compose run --rm notability-mcp hash-password   # → DAV_PASSWORD_HASH в .env
docker compose up -d
```

`NOTABILITY_DIR` — это папка `Notability` (например `~/obsidian-mcp/vault/Notability`).
Она монтируется в `/backups/Notability`, а корень WebDAV — `/backups`. Notability сам
создаёт папку `Notability` в корне WebDAV, поэтому файлы ложатся туда же, где лежат сейчас,
а остальной vault через WebDAV не виден.

### Caddy

Caddy на хосте:

```caddy
notes.example.com {
	reverse_proxy 127.0.0.1:8080
}
```

Если Caddy в Docker — уберите `ports:` из compose, подключите оба контейнера к общей сети
и пишите `reverse_proxy notability-mcp:8080`.

`PUBLIC_URL` должен точно совпадать с адресом в Caddy: по нему строятся OAuth-метаданные
и проверяется заголовок `Host` на `/mcp`.

### Notability

Настройки → Auto-backup → WebDAV:
- Server: `https://notes.example.com/dav`
- User / Password: `DAV_USER` и пароль, из которого сделан `DAV_PASSWORD_HASH`
- Формат: **PDF** (для чтения страниц) и **Note** (архив `.note`, внутри аудио и индекс
  распознанного рукописного текста). Поддерживаются и `.note`, и `.zip`.

### Подключение Claude

- claude.ai → Settings → Connectors → Add custom connector → `https://notes.example.com/mcp`.
  Откроется страница входа — введите пароль от `LOGIN_PASSWORD_HASH`.
- Claude Code: `claude mcp add --transport http notability https://notes.example.com/mcp`, затем `/mcp` → Authenticate.

## Транскрипция

Аудио внутри архива расшифровывается в фоне (сканер раз в `SCAN_INTERVAL` секунд + по
запросу `get_transcript`). Кэш хранится по CRC самой аудиодорожки: Notability перезаливает
архив при каждой правке заметки, но уже расшифрованная запись повторно не обрабатывается.

- `TRANSCRIBER=whisper` — локально, faster-whisper на CPU. `large-v3-turbo` int8 ≈ 2 ГБ RAM;
  на 2 vCPU работает примерно в реальном времени (урок 45 мин ≈ 45 мин). На слабом VPS — `small`.
  Модель скачивается при первом запуске в `data/models`.
- `TRANSCRIBER=assemblyai` — облако, быстрее и есть разделение по спикерам; нужен
  `ASSEMBLYAI_API_KEY`. После получения текста расшифровка удаляется на стороне AssemblyAI.

## Безопасность

- Пароли хранятся только как scrypt-хеши; токены в SQLite — только как SHA-256.
- Access token живёт 1 час, refresh — 60 дней с ротацией (повтор старого refresh отклоняется,
  а вся цепочка токенов отзывается). Код авторизации одноразовый, 5 минут.
- Динамическая регистрация клиентов разрешена только с redirect на `ALLOWED_REDIRECT_HOSTS`
  (защита от фишинговых клиентов, которые подсунут свой callback).
- Перебор паролей: 5 ошибок с IP за 15 минут → блок на 15 минут; плюс глобальный лимит.
- MCP-инструменты только читают; доступ ограничен `NOTES_ROOT`, скрытые файлы и `..` запрещены.
- Отозвать все сессии: `docker compose run --rm notability-mcp revoke-tokens`.
