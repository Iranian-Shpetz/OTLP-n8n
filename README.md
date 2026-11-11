# n8n OpenTelemetry → ntfy

Принимает трассировки n8n по OTLP/HTTP, выбирает завершённые spans workflow и отправляет краткие уведомления в ntfy. Данные и атрибуты spans не сохраняются.

## Как работает

```text
n8n -- OTLP/HTTP Protobuf + Bearer token --> OTel Collector
    -- OTLP/HTTP JSON по внутренней Docker-сети --> адаптер --> ntfy
```

Коллектор — собственная сборка OpenTelemetry Collector Builder (OCB), включающая только OTLP receiver, batch processor, OTLP/HTTP exporter и bearer-token authenticator. Адаптер принимает OTLP JSON и отправляет в ntfy только имя workflow, статус и тип ошибки. В частности, он не пересылает payload выполнения, атрибуты узлов или текст ошибки.

Исходный ответ DeepSeek ошибочно утверждал, что n8n отправляет JSON: по документации n8n стандартный протокол — OTLP/HTTP Protobuf. Здесь Collector принимает Protobuf, а JSON используется только на внутреннем соединении Collector → адаптер. Упомянутая заявка на webhook exporter — закрытая заявка на функцию, а не готовый компонент или pull request: [open-telemetry/opentelemetry-collector-contrib#38496](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/38496).

## Требования

- Docker Engine с Docker Compose plugin (`docker compose`).
- n8n с поддержкой OpenTelemetry tracing. По текущей документации n8n tracing находится в Preview и доступен начиная с n8n 2.19.0.
- Исходящий HTTPS-доступ к ntfy-серверу.

## Запуск

1. Создайте локальный `.env`:

   ```powershell
   Copy-Item .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Вставьте созданный токен в `OTLP_AUTH_TOKEN`, а в `NTFY_TOPIC` задайте свой topic. Не публикуйте `.env`: он уже исключён из Git. `NTFY_TOKEN` нужен только для ntfy-сервера с авторизацией.

2. Соберите и запустите сервисы:

   ```powershell
   docker compose up --build -d
   docker compose ps
   docker compose logs -f otel-collector intermediate-service
   ```

   Первая сборка коллектора скачивает Go-модули и может занять несколько минут.

3. Настройте n8n. Передайте переменные окружения **каждому** процессу n8n, который экспортирует spans (в том числе workers и webhook processors в queue mode):

   ```text
   N8N_OTEL_ENABLED=true
   N8N_OTEL_EXPORTER_OTLP_ENDPOINT=http://<адрес-коллектора>:4318
   N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer <тот-же-токен>
   ```

   Указывайте базовый адрес, без `/v1/traces`: n8n добавляет путь автоматически. Default protocol — `http/protobuf`; переключать его не требуется.

После выполнения production workflow уведомление появится в выбранном ntfy topic. По умолчанию наружу опубликован только порт `4318`, привязанный к `127.0.0.1`; порт адаптера `8080` доступен лишь внутри Docker-сети.

## Сеть и безопасность

- Если n8n запущен на этой же машине как процесс, используйте `http://127.0.0.1:4318`.
- Если n8n — контейнер, подключите его к Docker-сети проекта и обращайтесь к сервису по адресу `http://otel-collector:4318`; либо настройте доступ к опубликованному порту согласно вашей Docker-платформе.
- Для доступа с другой машины установите `OTLP_BIND_ADDRESS=0.0.0.0` в `.env` и ограничьте доступ firewall. Не передавайте Bearer token по недоверенной сети без TLS: разместите Collector за HTTPS reverse proxy или используйте защищённую сеть.
- `ntfy.sh` не делает topic приватным: любой, кто знает имя topic, может читать уведомления. Используйте длинное случайное имя topic, либо защищённый ntfy-сервер с `NTFY_TOKEN`.
- Для TLS между n8n и Collector используйте TLS reverse proxy. Входящая HTTP-аутентификация Collector защищает доступ токеном, но сама по себе не шифрует соединение.
- Collector/адаптер подтверждают экспорт только после обработки уведомлений. При сетевых сбоях Collector повторяет отправку; при неопределённом результате доставки возможны повторные уведомления.
- Адаптер не выводит тело spans или токены в логи. Не добавляйте секреты в имя workflow или собственные span-атрибуты: данные, намеренно включённые в уведомление, отправляются вашему ntfy-серверу.

## Проверки и обслуживание

Локальные тесты адаптера:

```powershell
python -m pip install -r intermediate/requirements.txt
python -m unittest discover -s tests -v
```

GitHub Actions выполняет тесты, проверяет Compose-конфигурацию и собирает оба Docker-образа. Версии компонентов OTel Collector зафиксированы в `builder-config.yaml` (core `0.165.0`, contrib `0.165.1`); обновляйте их согласованно и проверяйте сборку.

## Переменные окружения

| Переменная | Обязательна | Назначение |
| --- | --- | --- |
| `OTLP_AUTH_TOKEN` | Да | Общий Bearer token для n8n, Collector и адаптера (минимум 32 символа). |
| `NTFY_TOPIC` | Да | Topic ntfy, только буквы, цифры, `_` и `-`. |
| `NTFY_URL` | Нет | Базовый URL ntfy; по умолчанию `https://ntfy.sh`. |
| `NTFY_TOKEN` | Нет | Bearer token для авторизованного ntfy-сервера. |
| `OTLP_BIND_ADDRESS` | Нет | Адрес публикации OTLP-порта; по умолчанию `127.0.0.1`. |
| `OTLP_HTTP_PORT` | Нет | Порт на хосте; по умолчанию `4318`. |

## Источники

- [n8n: Trace executions with OpenTelemetry](https://docs.n8n.io/deploy/host-n8n/keep-n8n-running/trace-executions-with-opentelemetry/)
- [OpenTelemetry Collector Builder](https://github.com/open-telemetry/opentelemetry-collector/tree/main/cmd/builder)
- [OpenTelemetry Collector configuration](https://opentelemetry.io/docs/collector/configuration/)
