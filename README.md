# CF Inventory

Cloudflare dashboard — зоны, воркеры, Workers AI.

## Быстрый старт

### 1. Установка

Требуется **Python 3.10+** — [скачать](https://python.org/downloads).  
При установке отметить **"Add Python to PATH"**.

Запустить **`install.bat`** — установит все зависимости.

### 2. Запуск

Запустить **`run.bat`** — откроет браузер автоматически.

---

## Токен Cloudflare

При первом открытии нужен API токен с правами:

| Право | Уровень |
|---|---|
| Zone Read | Zone |
| Analytics Read | Zone |
| Workers Scripts Read | Account |
| Workers AI Read | Account |
| Account Settings Read | Account |

**Создать автоматически:** на экране входа нажать  
_"Don't have a token? Create one with Global API Key →"_  
и ввести email + Global API Key.
Global API можно получить по  [ссылке](https://dash.cloudflare.com/profile/api-tokens).   


### Сохранить токен (пропускать экран входа)

Открыть `.env` в любом текстовом редакторе и добавить:

```
CF_TOKEN=ваш_токен
```

---

## Файлы

```
cf-dash.html      — интерфейс
main.py           — сервер
requirements.txt  — зависимости Python
install.bat       — установка
run.bat           — запуск
.env              — настройки (создаётся автоматически)
cf-diag.log       — диагностический лог (появляется при ошибках)
```

## Порт

По умолчанию `19232`. Изменить в `.env`:

```
PORT=8080
```
