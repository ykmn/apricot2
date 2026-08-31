# Ускорение AD-логина — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Убрать две лишние LDAP-операции на каждый AD-логин (живой запрос схемы AD и повторный поиск primary-group DN), не трогая логику разрешения вложенных групп и структуру соединений.

**Architecture:** Точечные правки внутри `app/auth.py`: (1) `ldap3.Server(...)` строится со статическим офлайн-профилем схемы AD вместо живого запроса к DC; (2) `_get_primary_group_dn()` кеширует результат `(base_dn, primaryGroupID) → DN` на 1 час в module-level словаре; (3) debug-таймер вокруг каждой попытки логина в домен, чтобы численно подтвердить эффект на реальном AD.

**Tech Stack:** Python, `ldap3` (уже используется в проекте).

## Global Constraints

- В проекте нет автотестов (`CLAUDE.md`) — верификация только вручную: синтаксическая проверка + реальный логин на боевом AD, который сделает пользователь.
- Не коммитить без явной команды пользователя (`CLAUDE.md`).
- Не менять структуру LDAP-соединений (сервисный бинд + отдельный ре-бинд паролем) и алгоритм разрешения вложенных групп (`LDAP_MATCHING_RULE_IN_CHAIN`) — вне объёма по решению пользователя в спеке.
- Каждый коммит бампает `VERSION` в `app/main.py` через pre-commit хук автоматически — вручную его не трогать.

---

### Task 1: Офлайн-схема AD вместо живого запроса + замер времени попытки логина

**Files:**
- Modify: `app/auth.py:504` (создание `ldap3.Server`)
- Modify: `app/auth.py:705-719` (цикл перебора доменов в `_authenticate_ldap`)

**Interfaces:**
- Consumes: ничего нового — использует уже импортированный в файле `time` (строка 20) и существующий `log` (строка 27).
- Produces: ничего, что использовали бы другие задачи этого плана — Task 2 независим.

- [ ] **Step 1: Заменить `get_info=ldap3.ALL` на офлайн-профиль схемы**

В `app/auth.py` найти (около строки 504, внутри `_authenticate_one_domain`):

```python
    try:
        server = ldap3.Server(server_url, get_info=ldap3.ALL, connect_timeout=5)
    except Exception as exc:
```

Заменить на:

```python
    try:
        server = ldap3.Server(server_url, get_info=ldap3.OFFLINE_AD_2012_R2, connect_timeout=5)
    except Exception as exc:
```

`OFFLINE_AD_2012_R2` — встроенный в `ldap3` статический дамп схемы Active
Directory. Даёт то же форматирование атрибутов (включая `primaryGroupID`),
что и живой запрос через `get_info=ldap3.ALL`, но без сетевого round-trip
к DC за схемой при каждом логине.

- [ ] **Step 2: Добавить замер времени попытки логина в домен**

В `app/auth.py` в функции `_authenticate_ldap` найти цикл (около строки 705):

```python
    for dcfg in candidates:
        try:
            return _authenticate_one_domain(short_name, password, dcfg)
        except _LdapTaggedError as exc:
            if exc.tag not in _CONTINUE_TAGS:
                # Definitive failure (wrong password, no groups, config error) → stop
                raise LdapAuthError(str(exc)) from None

            last_error = exc
            label = dcfg.get("name", dcfg.get("domain", dcfg.get("server", "?")))
            if exc.tag == _TAG_NOT_FOUND:
                not_found_domains.append(label)
            else:
                conn_errors.append((label, str(exc)))
```

Заменить на:

```python
    for dcfg in candidates:
        label = dcfg.get("name", dcfg.get("domain", dcfg.get("server", "?")))
        _t0 = time.perf_counter()
        try:
            result = _authenticate_one_domain(short_name, password, dcfg)
            log.debug(
                "LDAP-логин %s через %s занял %.3fs",
                short_name, label, time.perf_counter() - _t0,
            )
            return result
        except _LdapTaggedError as exc:
            log.debug(
                "Попытка LDAP-логина %s через %s провалилась за %.3fs (%s)",
                short_name, label, time.perf_counter() - _t0, exc.tag,
            )
            if exc.tag not in _CONTINUE_TAGS:
                # Definitive failure (wrong password, no groups, config error) → stop
                raise LdapAuthError(str(exc)) from None

            last_error = exc
            if exc.tag == _TAG_NOT_FOUND:
                not_found_domains.append(label)
            else:
                conn_errors.append((label, str(exc)))
```

Обратите внимание: `label` теперь вычисляется один раз в начале итерации
(до `try`), а не только в ветке `except` — это позволяет использовать его
и в строке успешного лога, и в логе ошибки.

- [ ] **Step 3: Проверить синтаксис**

Run: `python -m py_compile app/auth.py`
Expected: команда завершается без вывода и без ошибки (exit code 0).

- [ ] **Step 4: Ручная проверка на реальном AD**

Попросить пользователя выполнить логин под доменной учёткой на боевом
инстансе (как было согласовано в брейнштормe — доступ к реальному AD есть).
В логе (уровень DEBUG, `app_logger.py` пишет DEBUG в файл ротации) должна
появиться строка `LDAP-логин <user> через <domain> занял N.NNNs`. Сравнить
это значение с временем логина на `main` (до правок) тем же пользователем —
ожидается заметное уменьшение (схема AD больше не запрашивается).
Убедиться, что `is_admin` в сессии совпадает с ожидаемым (тем же, что и на
`main`) — правка не должна менять состав групп.

- [ ] **Step 5: Commit**

Коммит делает пользователь по своей команде (правило проекта). Подготовить
для него:

```bash
git add app/auth.py
git commit -m "$(cat <<'EOF'
perf: офлайн-схема AD вместо живого запроса при каждом LDAP-логине; debug-таймер попытки логина

get_info=ldap3.ALL тянул с DC полную живую схему AD на каждый логин —
самая тяжёлая сетевая операция в цепочке аутентификации, не связанная с
количеством/вложенностью групп пользователя. Заменено на встроенный в
ldap3 статический профиль OFFLINE_AD_2012_R2 — то же форматирование
атрибутов, без сетевого round-trip за схемой.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Кеш primary-group DN (RID → DN) на 1 час

**Files:**
- Modify: `app/auth.py:414-437` (`_get_primary_group_dn`)

**Interfaces:**
- Consumes: `time` (уже импортирован), `log` (уже определён) — как в Task 1, но Task 2 не зависит от Task 1 и может выполняться независимо/в любом порядке.
- Produces: ничего, что использовали бы другие задачи.

- [ ] **Step 1: Добавить module-level кеш и переписать `_get_primary_group_dn`**

В `app/auth.py` найти текущую реализацию (строки 414-437):

```python
def _get_primary_group_dn(conn, base_dn: str, primary_group_id) -> Optional[str]:
    """Return the DN of the user's primary group by primaryGroupToken.

    AD stores the user's primary group as a RID in primaryGroupID.
    The matching group can be found via the computed attribute primaryGroupToken.
    This covers Domain Users (RID 513) and any other primary group.
    """
    import ldap3  # noqa: PLC0415
    if not primary_group_id:
        return None
    try:
        conn.search(
            base_dn,
            f"(primaryGroupToken={int(primary_group_id)})",
            search_scope=ldap3.SUBTREE,
            attributes=["distinguishedName"],
        )
        if conn.entries:
            dn = str(conn.entries[0].distinguishedName)
            log.debug("Primary group for primaryGroupID=%s: %s", primary_group_id, dn)
            return dn
    except Exception as exc:
        log.warning("Primary group lookup failed (primaryGroupID=%s): %s", primary_group_id, exc)
    return None
```

Заменить на:

```python
# RID (primaryGroupID) → DN основной группы почти никогда не меняется в
# рамках домена, поэтому кешируем результат поиска на час вместо того,
# чтобы делать LDAP-запрос на каждый логин.
_primary_group_dn_cache: dict[tuple[str, int], tuple[str, float]] = {}
_PRIMARY_GROUP_CACHE_TTL = 3600  # секунд


def _get_primary_group_dn(conn, base_dn: str, primary_group_id) -> Optional[str]:
    """Return the DN of the user's primary group by primaryGroupToken.

    AD stores the user's primary group as a RID in primaryGroupID.
    The matching group can be found via the computed attribute primaryGroupToken.
    This covers Domain Users (RID 513) and any other primary group.
    Result is cached per (base_dn, primary_group_id) for _PRIMARY_GROUP_CACHE_TTL
    seconds — this mapping is effectively static within a domain.
    """
    import ldap3  # noqa: PLC0415
    if not primary_group_id:
        return None

    cache_key = (base_dn, int(primary_group_id))
    now = time.time()
    cached = _primary_group_dn_cache.get(cache_key)
    if cached is not None and now - cached[1] < _PRIMARY_GROUP_CACHE_TTL:
        return cached[0]

    try:
        conn.search(
            base_dn,
            f"(primaryGroupToken={cache_key[1]})",
            search_scope=ldap3.SUBTREE,
            attributes=["distinguishedName"],
        )
        if conn.entries:
            dn = str(conn.entries[0].distinguishedName)
            log.debug("Primary group for primaryGroupID=%s: %s", primary_group_id, dn)
            _primary_group_dn_cache[cache_key] = (dn, now)
            return dn
    except Exception as exc:
        log.warning("Primary group lookup failed (primaryGroupID=%s): %s", primary_group_id, exc)
    return None
```

Промах кеша при ошибке поиска не записывается (нет отрицательного
кеширования) — при следующем логине поиск повторится, как и раньше.

- [ ] **Step 2: Проверить синтаксис**

Run: `python -m py_compile app/auth.py`
Expected: команда завершается без вывода и без ошибки (exit code 0).

- [ ] **Step 3: Ручная проверка на реальном AD**

Попросить пользователя выполнить логин под доменной учёткой дважды подряд
(в пределах часа). В debug-логе строка `Primary group for
primaryGroupID=...` должна появиться только при **первом** логине — при
повторном логине тем же пользователем в течение часа поиска быть не
должно (кеш-хит), при этом основная группа пользователя (следовательно и
`is_admin`, если она входит в `admin_groups`) должна определяться так же,
как и раньше.

Если в конфигурации несколько доменов (`domains:` в `ldap.yaml`) —
дополнительно проверить логин пользователем из другого домена и убедиться,
что кеш для него независим (свой `base_dn` → свой ключ кеша, не путается с
первым доменом).

- [ ] **Step 4: Commit**

```bash
git add app/auth.py
git commit -m "$(cat <<'EOF'
perf: кеш RID → DN основной группы AD на 1 час, чтобы не искать её на каждый логин

primaryGroupID (RID) → DN основной группы практически не меняется в
рамках домена, а _get_primary_group_dn() делал отдельный LDAP-поиск на
каждый логин. Теперь результат кешируется на час по ключу
(base_dn, primary_group_id); отрицательные результаты не кешируются.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

- **Spec coverage:** оба пункта спеки («офлайн-схема» и «кеш primary-group
  DN») покрыты Task 1 и Task 2 соответственно; замер времени из раздела
  «Тестирование» спеки реализован как постоянный debug-лог (не временный
  код для удаления) — сознательное решение, т.к. он не имеет накладных
  расходов и полезен для будущей диагностики.
- **Не в объёме (по спеке):** пул/переиспользование LDAP-соединений и
  изменение алгоритма вложенных групп — не затронуты ни в одной задаче.
- **Типы/сигнатуры:** `_get_primary_group_dn` сохраняет прежнюю сигнатуру
  `(conn, base_dn: str, primary_group_id) -> Optional[str]` — вызывающий
  код (`_authenticate_one_domain`, строки ~569 и ~646) не требует правок.
- Задачи независимы друг от друга и от порядка выполнения — можно
  выполнять и коммитить по отдельности.
