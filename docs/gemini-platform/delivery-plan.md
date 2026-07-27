# Инструменты, стоимость, план прототипа и метрики

---

## 1. Выбор production-инструментов

Своё DAM и свой планировщик с нуля не строим. Если у студии уже есть стек —
интегрируемся через его официальный API и event-модель.

| Категория | Опция | Что даёт | Слабое место | Когда выбирать |
|---|---|---|---|---|
| Review и versioning | Frame.io | upload/review, OAuth и dev tokens, SDK, webhooks, version stacks, review links | планирование и production tracking не его ядро | как review/version слой поверх внутреннего контура |
| Production tracking | Autodesk Flow Production Tracking | Python и REST API, интеграции, webhooks, сильная pipeline-интеграция | тяжелее процесс, дороже обучение группы | если студия уже живёт в ShotGrid/Flow |
| Scheduling | Autodesk Flow Generative Scheduling | импорт расписания, resource-levelling, экспорт | отдельный продуктовый контур Autodesk | для подсистемы планирования |
| Pipeline automation | ftrack | официальные JS/Python клиенты, webhooks, backend scripting | ограничения webhook-реализации (create/update; лимит включённых webhooks) | как event backbone при наличии ftrack |
| Google-native | Agent Search + Drive/Workspace + Calendar/Sheets | быстрый и дешёвый MVP, родная интеграция с платформой | слабее по DAM, ревью и медиа-версиям | если зрелого media stack ещё нет |

Функциональные детали инструментов взяты из исходного ТЗ и требуют проверки по
актуальной документации вендоров перед выбором.

## 2. Ориентиры стоимости

Не бюджет проекта, а ценовые ориентиры компонентов, формирующих monthly COGS.
Значения — плановые, из исходного ТЗ; **перепроверить перед фиксацией бюджета**.

| Компонент | Ориентир | Что это значит для архитектуры |
|---|---|---|
| Gemini 2.5 Flash Lite | $0.10 / 1M input, $0.40 / 1M output | default для routing, метаданных, summarization |
| Gemini 2.5 Flash | $0.30 / 1M input, $2.50 / 1M output | основной рабочий слой |
| Gemini 2.5 Pro | $1.25 / 1M input, $10 / 1M output | точечно, в gated узлах |
| Live API (Flash) | $0.5 / 1M text in, $3 / 1M audio in, $2 / 1M text out, $12 / 1M audio out | самый чувствительный к росту контекста контур |
| Grounding по своим данным | $2.5 / 1000 запросов | grounded QA считать отдельной статьёй |
| Grounding с Google Search | $35 / 1000 после free quota | для fan/public контуров ограничивать политикой |
| Agent Search Standard | $1.50 / 1000 запросов | внутренние keyword/hybrid сценарии |
| Agent Search Enterprise | $4.00 / 1000 запросов | если нужны core generative answers |
| Advanced Generative Answers | +$4.00 / 1000 запросов | дорого, включать не везде |
| Agent Search for Media | $2.00 / 1000 запросов | media discovery |
| Cloud Run | 2M запросов/мес бесплатно + free CPU/RAM tier | MVP держится очень экономно |
| Pub/Sub | первые 10 GiB бесплатно, далее $40 / TiB | обычно не главный драйвер |
| Spanner | от $0.03 / 100 PU / час / реплика + storage | только при реальной потребности в consistency и масштабе |

**Главный вывод по стоимости:** доминирующей статьёй становятся не токены
routing'а, а search/grounding и live-сессии. Отсюда механика в коде: Flash Lite
как default, Pro только в gated узлах, budget guard с alert-порогом ниже
потолка и автоматическим понижением tier, плюс `UsageLedger` на каждый прогон —
стоимость видна в audit trail, а не только в биллинге в конце месяца.

## 3. План прототипа

| Этап | Цель | Ключевой результат |
|---|---|---|
| Discovery и contract freeze | зафиксировать MCP-профиль, классы данных, модель утверждений | ADR, schema registry, таксономия событий |
| Core platform | Cloud Run frontend, Agent Runtime, базовый граф | рабочий coordinator + auth + audit |
| Knowledge plane | Agent Search / RAG для сценариев, call sheets, production notes | grounded retrieval с цитатами |
| MCP integration | партнёрский MCP через Gateway/адаптер | `tools/list`, `tools/call`, webhook-события |
| Production tooling | планирование и версионирование | адаптер к Flow PT / Frame.io / ftrack / Google-native |
| Governance and safety | IAM, Model Armor, redaction, approval gates, бюджеты | security hardening baseline |
| Pilot | ограниченный проект или одна производственная единица | KPI-отчёт и решение go/no-go |

Текущий статус: этапы «core platform» и «governance» покрыты референсной
реализацией на уровне логики и контрактов (`reference-implementation.md`);
не сделаны развёртывание в GCP, подключение реальных моделей и живого партнёра.

## 4. Тест-план

| Область | Что тестировать | Критерий |
|---|---|---|
| MCP contract | `initialize`, `tools/list`, `tools/call`, семантика ретраев, обновление токена | 100% прохождение контрактных тестов |
| Script analysis | извлечение сущностей, классификация сцен, continuity flags | ≥ 90% принятия редактором/AD на golden set |
| Scheduling | выполнимость, обнаружение конфликтов, полезность ранжирования | ≥ 80% планов «usable with edits» |
| Retrieval | groundedness, релевантность цитат, фильтрация по доступу | hallucination rate < 3%, access violations = 0 |
| Asset workflows | разрешение версий, приём событий, summaries комментариев | 99% успешного приёма событий, без дублей побочных эффектов |
| Security | prompt injection, эксфильтрация, эскалация привилегий | detect/block по red-team набору, 0 critical bypass |
| Performance | p95 чата, время async workflow, latency webhook | p95 чата < 4 c, async план < 90 c, webhook-to-state < 10 c |
| Reliability | ретраи, дубликаты событий, учения по failover | идемпотентность подтверждена, runbook отработан |
| Cost control | token burn, query burn, амплификация grounded-запросов | алерты срабатывают до 80% месячного потолка |

Автоматически покрыто сейчас (`tests/test_agentplatform.py`, 44 теста):
контракт MCP и ретраи, идемпотентность, подпись и дедупликация событий,
фильтрация по контуру, блокировка утечки, HITL-гейт и возобновление прогона,
деградация планировщика, маршрутизация моделей и бюджетный даунгрейд,
разбор сценария и continuity flags.

Не покрыто и требует пилота: качество на golden set, red-team по injection,
метрики latency под нагрузкой, failover-учения.

## 5. Продуктовые метрики

Мерить нужно не количество ответов модели, а снятые узкие места.

| Персона | KPI |
|---|---|
| Сценарист | время разбора новой версии сценария; число пропущенных continuity issues |
| Режиссёр | время до usable scene plan; качество поиска нужных материалов |
| Линейный продюсер | время подготовки черновика call sheet; число конфликтов, найденных до съёмочного дня |
| Координатор поста | время поиска нужной версии ассета; скорость подготовки review packets |
| Фанат / комьюнити | deflection rate FAQ; CSAT; отсутствие инцидентов со спойлерами |

Метрика, которую стоит вести отдельно: **доля прогонов, где человек согласился
с первым вариантом планировщика**. Она отличает систему, которая экономит время,
от системы, которая добавляет ещё один шаг согласования.
