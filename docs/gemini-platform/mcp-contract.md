# Контракт интеграции с партнёрским MCP

Спецификация партнёрского сервера не предоставлена, поэтому здесь зафиксирован
**contract assumption** — набор допущений, на который написана референсная
реализация (`pinocut/agentplatform/mcp.py`, `events.py`). При получении реальных
endpoints меняется адаптер, а не агентный слой.

Все пункты ниже нужно подтвердить с партнёром до пилота. Пока они не
подтверждены, любой прогон должен уметь работать без партнёра — планировщик
деградирует на локальную эвристику.

---

## 1. Профиль транспорта

| Компонент | Решение | Почему |
|---|---|---|
| Transport | Streamable HTTP на `/mcp` | проще для Cloud Run, Gateway и observability |
| Protocol | JSON-RPC 2.0 | базовый формат MCP |
| Auth (production) | OAuth 2.x / OIDC discovery | delegated auth, ротация без релиза |
| Auth (прототип) | bearer token через Agent Identity auth manager | быстро, но без делегирования |
| Eventing | подписанный webhook + Eventarc + Pub/Sub | не зависит от незастывшей push-части MCP |
| Idempotency | `_meta.idempotencyKey` + заголовок `X-Idempotency-Key` | обязателен для ретраев и двойной доставки |
| Error model | `code`, `category`, `retryable`, `humanMessage` | агенту нужна машинно-читаемая политика ретраев |
| Schema governance | версионирование tool schema + контрактные тесты | иначе агент деградирует при drift'е |

Вызовы идут не из бизнес-кода напрямую, а через Agent Gateway с регистрацией
сервера в Agent Registry. Если партнёрский сервер не дотягивает до governance-
требований, между Gateway и партнёром ставится тонкий адаптер на Cloud Run.

## 2. Таксономия ошибок и ретраи

Реализация: `McpErrorCategory`, `RETRYABLE_CATEGORIES`, `RetryPolicy`.

| Категория | Источник | Retry |
|---|---|---|
| `transport` | сетевой сбой, таймаут, битый JSON | да |
| `rate_limit` | HTTP 429 | да |
| `server` | HTTP 5xx, JSON-RPC `-32603` | да |
| `auth` | HTTP 401/403 | нет — нужен новый токен, а не повтор |
| `validation` | HTTP 400/422, JSON-RPC `-32600/-32601/-32602/-32700` | нет — повтор даст ту же ошибку |
| `protocol` | ответ не по схеме | нет |
| `unknown` | всё остальное | нет |

Backoff — экспоненциальный: 2 c, 4 c, 8 c, 16 c, максимум 4 попытки.
**Idempotency-ключ между попытками не меняется** — иначе ретрай создаст второе
расписание на стороне партнёра. Ключ выводится детерминированно из
`projectId + scriptVersion + sha256(goal)` и стабилен между процессами и
воркерами (`hash()` солится на процесс и для этого непригоден).

## 3. Примеры вызовов

### Инициализация сессии

```http
POST /mcp HTTP/1.1
Authorization: Bearer <access-token>
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": "init-1",
  "method": "initialize",
  "params": {
    "clientInfo": { "name": "pinocut-media-agent-platform", "version": "0.1.0" },
    "protocolVersion": "2025-11-25",
    "capabilities": { "tools": {}, "resources": {}, "prompts": {} }
  }
}
```

### Список инструментов

```http
POST /mcp HTTP/1.1
Authorization: Bearer <access-token>
Content-Type: application/json

{ "jsonrpc": "2.0", "id": "tools-1", "method": "tools/list", "params": {} }
```

### Оптимизация расписания

```http
POST /mcp HTTP/1.1
Authorization: Bearer <access-token>
Content-Type: application/json
X-Idempotency-Key: shootplan-film-123-v17-9f2c1a4b7e05

{
  "jsonrpc": "2.0",
  "id": "call-1",
  "method": "tools/call",
  "params": {
    "name": "schedule.optimize",
    "arguments": {
      "projectId": "film-123",
      "scriptVersion": "v17",
      "shootWindow": { "start": "2026-09-10", "end": "2026-09-17" },
      "constraints": {
        "unionHours": true,
        "locationAvailability": true,
        "castAvailability": true,
        "weatherSensitivity": ["EXT_DAY", "RAIN_LIMITED"]
      }
    },
    "_meta": { "idempotencyKey": "shootplan-film-123-v17-9f2c1a4b7e05" }
  }
}
```

Ожидаемый результат:

```json
{
  "jsonrpc": "2.0",
  "id": "call-1",
  "result": {
    "options": [
      { "optionId": "A", "score": 0.81, "risks": ["weather"], "days": [] },
      { "optionId": "B", "score": 0.87, "risks": ["crew overtime"], "days": [] }
    ]
  }
}
```

Клиент валидирует каждый вариант: без `optionId` элемент отбрасывается, `score`
приводится к float, `risks` — к списку строк. Невалидный ответ равнозначен
пустому: планировщик уходит на локальную эвристику с warning'ом.

## 4. Event-контракт

Универсальный push-механизм MCP ещё эволюционирует, поэтому события идут по
явному side-channel: партнёр → подписанный webhook → Eventarc/Pub/Sub → воркеры.

```http
POST /api/v1/integrations/partner-mcp/events HTTP/1.1
Content-Type: application/json
X-Signature: sha256=<hmac>
X-Event-Id: evt_01J...
X-Event-Type: asset.version.approved

{
  "eventId": "evt_01J...",
  "eventType": "asset.version.approved",
  "occurredAt": "2026-09-10T08:12:33Z",
  "tenantId": "studio-a",
  "projectId": "film-123",
  "entity": { "type": "assetVersion", "id": "av_456", "parentAssetId": "asset_123" },
  "actor": { "type": "user", "id": "u_7781" },
  "payload": { "status": "approved", "commentSummary": "Ready for assembly" }
}
```

Порядок проверок в ingress (`events.py: EventIngress`) обязателен именно такой:

1. **HMAC-SHA256 по сырому телу**, сравнение в constant time. Подпись
   проверяется до разбора JSON — неподписанное тело не парсится вообще.
2. **Валидация конверта**: обязательные `eventId`, `eventType`, `occurredAt`,
   `tenantId`, `projectId`; `occurredAt` должен быть ISO-8601.
3. **Сверка `X-Event-Id` с телом** — расхождение отвергается.
4. **Дедупликация по `eventId`** — доставка считается at-least-once.
5. **Проверка типа** по таксономии; неизвестный тип уходит в dead-letter, а не
   к агенту.

Таксономия событий v1: `asset.version.created`, `asset.version.approved`,
`review.status.changed`, `review.annotation.added`, `schedule.changed`,
`schedule.published`, `legal.approval.granted`, `script.version.published`.

Ключ подписи хранится в Secret Manager и ротируется независимо от токенов
доступа. Для ротации без простоя ingress должен на время принимать обе подписи.

## 5. Внутренний orchestration API

Внутренний API нужен даже при чат-интерфейсе: он даёт асинхронный контур,
ретраи и audit.

```http
POST /api/v1/jobs HTTP/1.1
Authorization: Bearer <user-token>
Content-Type: application/json

{
  "workflow": "shoot-day-plan",
  "projectId": "film-123",
  "priority": "high",
  "inputs": {
    "scriptVersion": "v17",
    "dateRange": { "from": "2026-09-10", "to": "2026-09-17" },
    "goal": "minimize cast idle time while preserving location blocks"
  },
  "options": { "requireHumanApproval": true, "emitDraftToTaskBoard": true }
}
```

```json
{
  "jobId": "job_01JAB...",
  "status": "queued",
  "approvalState": "not_required_yet",
  "estimatedArtifacts": ["scene_breakdown.csv", "call_sheet_draft.pdf", "risk_report.md"]
}
```

`requireHumanApproval: false` не отключает HITL для действий из обязательного
списка (`governance.md`, §2) — флаг влияет только на действия вне этого списка.

Состояния прогона: `queued` → `running` → `awaiting_approval` → `completed` |
`blocked` | `rejected` | `failed`. Ответ на approval:

```http
POST /api/v1/jobs/{jobId}/approvals/{requestId} HTTP/1.1
Authorization: Bearer <user-token>

{ "approved": true, "selectedOptionId": "B", "note": "location confirmed" }
```

Approver фиксируется по токену, а не по телу запроса: решение должно быть
атрибутируемым. Выбор варианта, которого планировщик не предлагал, отвергается.

## 6. Контрактные тесты

Минимальный набор, который должен проходить до подключения к живому партнёру
(реализовано в `tests/test_agentplatform.py`):

- `initialize` согласует версию протокола и передаёт bearer;
- `tools/call` кладёт idempotency-ключ и в `_meta`, и в заголовок;
- ретраибельный сбой повторяется **с тем же ключом**;
- `validation` и `auth` не ретраятся;
- число попыток ограничено политикой;
- задержки backoff растут экспоненциально и упираются в потолок;
- JSON-RPC ошибка отображается в общую таксономию;
- каждый вызов попадает в audit-запись с числом попыток и категорией ошибки.
