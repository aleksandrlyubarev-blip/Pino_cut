# Целевая архитектура

Status: draft для итерации
Scope: платформенный слой для кино/медиа поверх Gemini и Google Cloud Agent
Builder / Agent Platform. Движок рендера (`pinocut/`) остаётся отдельным
детерминированным слоем и вызывается как инструмент.

---

## 1. Два контура, а не один продукт

Первое архитектурное решение — не «ассистент с ролями», а **две разные системы
над общим проектом**:

```
┌──────────────────────────────────────────────────────────────┐
│ ВНУТРЕННИЙ КОНТУР (internal)                                 │
│ режиссёр · сценарист · линейный продюсер · координатор поста  │
│ сценарии, шот-листы, call sheets, бюджет, дэйлисы             │
│ data classes: private_development, restricted_production      │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ ПАРТНЁРСКИЙ КОНТУР (partner)                                  │
│ подрядчики, пост-продакшн, вендоры                            │
│ approved dailies, технические метаданные, delivery specs      │
│ data classes: partner_shared и ниже                           │
└──────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────┐
│ FAN-FACING КОНТУР (fan)                                       │
│ фанаты, маркетинг, комьюнити                                  │
│ только опубликованные материалы                               │
│ data classes: public_fan_safe                                 │
└──────────────────────────────────────────────────────────────┘
```

Контур — это не роль в промпте, а свойство запроса, которое проверяется в трёх
местах: при старте workflow, внутри retrieval и в governance-агенте.
Разделение по промпту не считается разделением.

Реализация: `pinocut/agentplatform/classification.py`,
`retrieval.py`, `orchestrator.py` (`required_contour`).

## 2. Компоненты платформы

| Слой | Выбор | Роль |
|---|---|---|
| Agent platform | Gemini Enterprise Agent Platform / Agent Builder / Agent Runtime | build-deploy-govern цикл агентов |
| Agent framework | ADK | graph-based multi-agent workflows, sessions, state, memory |
| Модели | Gemini 2.5 Flash Lite / Flash / Pro | routing, ассистентный слой, сложный reasoning |
| Голос/реалтайм | Gemini Live API | on-set voice copilot, fan-facing live Q&A |
| Приватный retrieval | Agent Search + RAG Engine | grounded ответы по приватным корпусам |
| Парсинг документов | Document AI Layout Parser | call sheets, PDF, breakdown tables |
| MCP governance | Agent Gateway + Agent Registry + Agent Identity | управляемое подключение к внешнему MCP |
| Runtime | Cloud Run | frontend, адаптеры, async workers |
| События | Pub/Sub + Eventarc | webhook ingress, ретраи, decoupling |
| Session/state | Firestore (MVP) → Spanner (production) | состояние прогонов и approvals |
| Secrets | Secret Manager + Parameter Manager | токены партнёра, ключи подписи webhook |
| Observability | Cloud Logging / Trace / Eval | audit trail, трассировка, оценка агентов |
| Runtime safety | Model Armor | prompt injection, утечки, вредный вывод |

## 3. Схема системы

```
        внутренние пользователи            фанаты / маркетинг
                 │                                │
                 ▼                                ▼
        ┌──────────────────┐            ┌──────────────────┐
        │  LB + IAP / IdP  │            │  LB + IdP        │
        └────────┬─────────┘            └────────┬─────────┘
                 ▼                                ▼
        ┌───────────────────────────────────────────────────┐
        │        Cloud Run frontend / orchestration API      │
        └───────────────────────┬───────────────────────────┘
                                ▼
        ┌───────────────────────────────────────────────────┐
        │  Agent Runtime (ADK graph)                         │
        │  Coordinator → специализированные агенты →         │
        │  Quality & Governance → Approval Gate              │
        └───┬───────────────┬───────────────┬───────────────┘
            ▼               ▼               ▼
     Gemini models   Agent Search /   Agent Gateway
     + context cache   RAG Engine     + Registry + Identity
     + Memory Bank         │                 │
                           ▼                 ▼
                    приватные корпуса   партнёрский MCP
                    (по data class)      (tools/call)
                                              │
        webhook ingress ◄─── Eventarc ◄── Pub/Sub ◄─┘
                │
                ▼
        async workers on Cloud Run
                │
                ▼
        Cloud Logging / Trace / Audit Logs
```

## 4. Роли агентов

| Агент | Ответственность | Инструменты | Модель | HITL |
|---|---|---|---|---|
| Coordinator | Маршрутизация, handoff, policy checks | tools registry, session state | Flash Lite | средняя |
| Script Analyst | Разбор сценария, beats, continuity gaps | RAG, layout parser, screenplay parser | Pro | средняя |
| Scene Breakdown | Props, локации, состав, VFX, костюм, continuity | RAG, MCP tools, asset DB | Pro / Flash | высокая |
| Schedule Planner | Черновик call sheet, конфликты, варианты плана | MCP scheduling, Calendar/Sheets | Pro / Flash | очень высокая |
| Asset Librarian | Дэйлисы, версии, ревью-комментарии, review packets | DAM API, version stacks, search | Flash | средняя |
| Collaboration | Задачи, уведомления, summaries, approvals | Chat/Docs/Tasks/Email | Flash | средняя |
| Fan Experience | FAQ, canon-safe ответы, BTS summaries | public RAG, guardrails, moderation | Flash / Live | высокая |
| Quality & Governance | Groundedness, policy, доступ, redaction | grounding, policy tools, access checks | Pro | очень высокая |
| MCP Tool Broker | Нормализация вызовов, schema validation, retries | MCP adapter / gateway | детерминированный код | — |

Ключевое разделение внутри каждого агента: **факты вычисляются, мнения
генерируются**. Номера сцен, состав, тип сцены (INT/EXT, день/ночь), конфликты
локаций и ранжирование вариантов — детерминированный код
(`screenplay.py`, `SchedulePlannerAgent.rank`). Модель отвечает за резюме,
объяснение рисков и формулировки. Такое разделение делает вывод проверяемым и
воспроизводимым, а стоимость — предсказуемой.

## 5. Маршрутизация моделей

| Класс задачи | Tier | Обоснование |
|---|---|---|
| routing, metadata | Flash Lite | самый дешёвый путь для классификации и служебных задач |
| drafting, retrieval QA | Flash | основной рабочий ассистентный слой |
| deep reasoning, quality review | Pro | точечно, в gated узлах графа |
| realtime voice/video | Live | on-set copilot, fan-facing live |

Маршрутизация — политика в одном месте (`models.py: ModelRouter`), а не привычка
конкретного агента. При достижении alert-порога бюджета маршрутизатор
автоматически понижает tier (Pro → Flash → Flash Lite); у realtime-контура
понижения нет, поэтому его ограничивают квотами, а не даунгрейдом.

Большие повторяющиеся контексты (сценарий, style bible, bible сезона, список
персонажей) выносятся в context caching; долгая память проекта и пользователя —
в Memory Bank.

## 6. Поток «план съёмочного дня»

```
Режиссёр
   │ "собери план съёмочного дня по новой версии сценария"
   ▼
Coordinator ── contour check ──► отказ, если контур не internal
   ▼
Script Analyst ──► retrieval (по data class) ──► парсинг сцен + continuity flags
   ▼
Scene Breakdown ──► состав по сценам, локации, департаменты, погодные риски
   ▼
Schedule Planner ──► partner MCP: tools/call schedule.optimize (idempotency key)
   │                     └─ при отказе: локальная эвристика + warning
   ▼
Asset Librarian ──► версии и ревью-комментарии по сценам
   ▼
Quality & Governance ──► повторная проверка цитат по контуру, grounding, риски
   │                     └─ leak выше потолка контура → BLOCKED
   ▼
Approval Gate ──► ApprovalRequest продюсеру: варианты + риски + цитаты
   │                (прогон останавливается, ничего не публикуется)
   ▼
[решение человека]
   ▼
Collaboration ──► черновики задач (status: draft)
```

Три состояния останова, и все три оставляют прогон восстановимым и полностью
описанным в audit trail: `BLOCKED` (governance), `AWAITING_APPROVAL` (HITL),
`FAILED` (ошибка узла). Отказ продюсера терминален — система не переигрывает
отклонённый план, а ждёт нового запроса.

## 7. Классы задержки

| Класс | Что входит | Требование | Реализация |
|---|---|---|---|
| Sync low-latency | routing, короткий lookup, drafting | p95 < 4 c | Flash Lite / Flash, context cache |
| Async heavy | breakdown, планирование, batch enrichment | < 90 c | Pro/Flash + Pub/Sub + workers |
| Realtime | голосовой copilot на площадке, live Q&A | сессия | Live API, barge-in |

Разделение обязательно: если планирование живёт в синхронном HTTP-запросе,
первый же таймаут партнёрского планировщика превращается в потерянный прогон
вместо отложенной задачи.

## 8. Отказоустойчивость

- **Партнёрский MCP недоступен** — планировщик деградирует на локальную
  эвристику и помечает результат warning'ом. Съёмочная группа получает
  черновик, а не пустой экран.
- **Дубликаты событий** — at-least-once доставка считается нормой; ingress
  отбрасывает повторы по `eventId`, мутирующие вызовы идут с idempotency-ключом,
  стабильным между процессами и воркерами.
- **Region outage** — Cloud Run региональный, поэтому для production нужен
  warm-standby во втором регионе, а состояние — в Firestore/Spanner с DR-планом.
- **Отказ модели** — детерминированный путь агентов работает и без модели;
  отсутствие LLM снижает качество формулировок, но не ломает граф.

## 9. Интеграция с движком PinoCut

Платформенный слой не рендерит видео. Когда прогон доходит до сборки материала,
он вызывает существующий движок (`SceneToolbox`, `SceneStitcherAgent`,
`pinocut scene build`) как инструмент — так же, как партнёрский MCP. Это
сохраняет принцип из `docs/director/architecture.md`: LLM планирует и объясняет,
детерминированный код считает таймкоды и собирает таймлайн.
