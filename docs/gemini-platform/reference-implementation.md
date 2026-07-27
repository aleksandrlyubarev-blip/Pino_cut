# Референсная реализация

Модуль `pinocut/agentplatform/` — исполняемая часть этого ТЗ. Он не имеет
внешних зависимостей и не делает собственных сетевых вызовов: транспорт, модели
и retrieval внедряются снаружи, поэтому один и тот же граф работает против
Google Cloud в production и против фейков в тестах.

---

## 1. Что реализовано

| Раздел ТЗ | Модуль | Состояние |
|---|---|---|
| Классы данных, контуры, redaction | `classification.py` | реализовано |
| Маршрутизация моделей, цены, бюджет | `models.py` | реализовано; цены — плановые ориентиры |
| Партнёрский MCP: JSON-RPC, ретраи, идемпотентность, аудит | `mcp.py` | реализовано против contract assumption |
| Подписанный webhook ingress, дедупликация | `events.py` | реализовано |
| Retrieval с фильтрацией по контуру и цитатами | `retrieval.py` | in-memory реализация; интерфейс под Agent Search / RAG Engine |
| Разбор сценария, continuity flags | `screenplay.py` | реализовано |
| Роли агентов | `agents.py` | реализовано; вызов модели — через внедряемый интерфейс |
| HITL-гейт, запросы и решения | `hitl.py` | реализовано |
| Граф прогона, audit trail, возобновление | `orchestrator.py` | реализовано |
| Состояние прогона, QA-отчёт, ledger | `state.py` | реализовано |
| Манифест корпуса с явной классификацией | `corpus.py` | реализовано |

## 2. Чего нет

Осознанно не сделано, потому что требует внешних решений и доступов:

- развёртывание в GCP (Cloud Run, Agent Runtime, Eventarc, Pub/Sub, IAM);
- подключение реальных моделей Gemini — есть только интерфейс `LanguageModel` и
  офлайн-заглушка `StubLanguageModel`, которая возвращает подготовленные
  ответы и **ничего не выдумывает**, если ответа нет;
- подключение живого партнёрского MCP — до получения спецификации работает
  адаптер против допущений из `mcp-contract.md`;
- реальные коннекторы к Frame.io / Flow PT / ftrack;
- персистентность прогонов (сейчас состояние живёт в памяти процесса; для
  production нужен Firestore/Spanner под сессии и approvals).

`InMemoryRetrievalService` — keyword-скоринг, а не поисковая система: он нужен,
чтобы проверять контракт фильтрации доступа и форму цитат, и заменяется на
Agent Search / RAG Engine без изменений в агентах.

## 3. Запуск

```bash
pinocut platform plan docs/gemini-platform/corpus.example.json \
  --goal "собери план съёмочного дня по сценам 12-18" \
  --script-version v17 \
  --project-id film-123 \
  --out output/run.json
```

Вывод: audit trail по узлам, статус прогона и запрос на утверждение с
вариантами и рисками. CLI **не утверждает** план — решение принимается в
продюсерском интерфейсе, поэтому команда останавливается на
`awaiting_approval`.

С партнёрским MCP:

```bash
export PARTNER_MCP_TOKEN=...
pinocut platform plan docs/gemini-platform/corpus.example.json \
  --goal "..." \
  --mcp-endpoint https://partner.example.com/mcp \
  --budget-cap 250
```

Если партнёр недоступен, планировщик деградирует на локальную эвристику и
помечает результат warning'ом — прогон не падает.

## 4. Программный интерфейс

```python
from pinocut.agentplatform import (
    AgentDeps, ApprovalDecision, Contour, InMemoryRetrievalService,
    ProjectContext, WorkflowState, build_shoot_day_plan_workflow,
)
from pinocut.agentplatform.corpus import load_manifest
from pathlib import Path

deps = AgentDeps(
    retrieval=InMemoryRetrievalService(load_manifest(Path("corpus.json"))),
    # llm=GeminiLanguageModel(...),      # ваша реализация LanguageModel
    # mcp=PartnerMcpClient(...),         # партнёрский MCP через Agent Gateway
)

orchestrator = build_shoot_day_plan_workflow(deps)
run = orchestrator.run(WorkflowState(
    context=ProjectContext("film-123", "u_7781", Contour.INTERNAL, script_version="v17"),
    goal="собери план съёмочного дня по сценам 12-18",
))

assert run.status.value == "awaiting_approval"
request = run.state.approval_request        # варианты, риски, цитаты

# ...решение принимает человек в UI...
run = orchestrator.resume(run, ApprovalDecision(
    request_id=request.request_id,
    approver_id="producer_1",
    approved=True,
    selected_option_id="B",
))
```

Чтобы подключить реальную модель, достаточно реализовать `LanguageModel`:

```python
class GeminiLanguageModel:
    def generate(self, *, node, prompt, tier, schema=None) -> ModelResponse:
        model_id = MODEL_IDS[tier]
        ...                                  # Interactions API / ADK
        return ModelResponse(data=parsed, input_tokens=..., output_tokens=...)
```

Агенты сами выбирают tier через `ModelRouter`, поэтому реализации не нужно
знать про политику маршрутизации и бюджет.

## 5. Инварианты, зафиксированные тестами

Эти свойства проверяются автоматически и должны сохраняться при любых
изменениях:

- fan-контур не запускает внутренний workflow — прогон блокируется **до**
  первого узла, retrieval не выполняется;
- данные выше потолка контура, попавшие в вывод, всегда блокируют прогон;
- отфильтрованные документы сообщаются, но не блокируют — иначе fan-контур
  перестаёт работать;
- ретрай MCP использует тот же idempotency-ключ, а ключ стабилен между
  процессами;
- ошибки `validation` и `auth` не ретраятся;
- повторно доставленное событие отбрасывается, тело с неверной подписью не
  парсится;
- до утверждения человеком не создаётся ни один черновик задачи;
- отказ продюсера терминален;
- выбор варианта, которого планировщик не предлагал, отвергается;
- документ цитируется один раз за прогон, независимо от числа агентов,
  которые его нашли.

Запуск: `pytest tests/test_agentplatform.py -v`.
