"""Authentication module — LDAP (Active Directory) and local users.

Auth is enabled only when config/ldap.yaml exists.
If the file is absent, all requests pass through without authentication.

Priority: Local users checked first; if username not found locally → try LDAP/AD.

Multi-domain support:
  - Single domain: top-level server/domain/base_dn keys (backward compatible)
  - Multiple domains: 'domains:' list; tried sequentially if no domain hint in username
  - Username formats accepted:  user  |  DOMAIN\\user  |  user@domain.com
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Optional

import yaml


# ── Auth exception hierarchy ──────────────────────────────────────────────────

class AuthError(Exception):
    """User-visible authentication error (wrong password, access denied, etc.)."""

class LdapAuthError(AuthError):
    """LDAP/AD-specific authentication error with descriptive message."""


CONFIG_DIR    = Path(__file__).parent.parent / "config"
LDAP_YAML     = CONFIG_DIR / "ldap.yaml"
USERS_YAML    = CONFIG_DIR / "users.yaml"
SESSIONS_FILE = CONFIG_DIR / "sessions.yaml"

COOKIE_NAME = "avocado_session"
SESSION_TTL = 7 * 24 * 3600   # default: 1 week


def configure(session_ttl: int) -> None:
    """Apply runtime settings loaded from settings.yaml."""
    global SESSION_TTL
    SESSION_TTL = session_ttl

# Tags used internally to decide whether to continue to next domain
_TAG_NOT_FOUND   = "NOT_FOUND"    # user not in this domain → try next
_TAG_CONN_ERROR  = "CONN_ERROR"   # server unreachable → try next
_TAG_WRONG_PWD   = "WRONG_PWD"    # password wrong → stop
_TAG_NO_ACCESS   = "NO_ACCESS"    # groups check failed → stop
_TAG_CFG_ERROR   = "CFG_ERROR"    # service account / config bad → stop

# ── In-memory session store ───────────────────────────────────────────────────
# {token: {username, is_admin, auth_type, expires}}
_sessions: dict[str, dict] = {}


# ── Session persistence ───────────────────────────────────────────────────────

def load_sessions() -> None:
    """Load persisted sessions from YAML on startup, discarding expired ones."""
    if not SESSIONS_FILE.exists():
        return
    try:
        with SESSIONS_FILE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        now = time.time()
        loaded = 0
        for token, sess in (data.get("sessions") or {}).items():
            if isinstance(sess, dict) and sess.get("expires", 0) > now:
                _sessions[token] = sess
                loaded += 1
    except Exception:
        pass  # non-critical — start with empty sessions


def _save_sessions() -> None:
    """Persist active (non-expired) sessions to YAML."""
    _cleanup_sessions()
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSIONS_FILE.open("w", encoding="utf-8") as f:
            yaml.dump(
                {"sessions": dict(_sessions)},
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
    except Exception:
        pass  # non-critical


# ── Config ────────────────────────────────────────────────────────────────────

def load_auth_config() -> Optional[dict]:
    """Return parsed ldap.yaml or None if auth is not configured."""
    if not LDAP_YAML.exists():
        return None
    with LDAP_YAML.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def auth_required() -> bool:
    """Return True when authentication is configured and at least one method enabled."""
    cfg = load_auth_config()
    if cfg is None:
        return False
    return bool(cfg.get("LDAP") or cfg.get("Local"))


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(username: str, is_admin: bool, auth_type: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username":  username,
        "is_admin":  is_admin,
        "auth_type": auth_type,
        "expires":   time.time() + SESSION_TTL,
    }
    _save_sessions()
    return token


def get_session(token: str) -> Optional[dict]:
    s = _sessions.get(token)
    if not s:
        return None
    if time.time() > s["expires"]:
        _sessions.pop(token, None)
        _save_sessions()
        return None
    return s


def delete_session(token: str) -> None:
    _sessions.pop(token, None)
    _save_sessions()


def _cleanup_sessions() -> None:
    now = time.time()
    for k in [k for k, v in _sessions.items() if now > v["expires"]]:
        del _sessions[k]


# ── Local authentication ──────────────────────────────────────────────────────

def _load_local_users() -> list[dict]:
    if not USERS_YAML.exists():
        return [{"username": "admin", "password": "admin", "is_admin": True}]
    with USERS_YAML.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("users", [])


# ── LDAP helpers ──────────────────────────────────────────────────────────────

def _resolve_secret(secret_id) -> Optional[dict]:
    """Return {username, password, domain} for a secret id from secret.yaml."""
    if secret_id is None:
        return None
    secret_file = CONFIG_DIR / "secret.yaml"
    if not secret_file.exists():
        return None
    with secret_file.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for entry in data.get("authorization", []):
        if entry.get("id") == secret_id:
            return {
                "username": entry.get("username", ""),
                "password": entry.get("password", ""),
                "domain":   entry.get("domain", ""),
            }
    return None


def _base_dn_to_upn_suffix(base_dn: str) -> str:
    """'DC=corp,DC=local' → 'corp.local'"""
    parts = [
        p.split("=", 1)[1]
        for p in base_dn.split(",")
        if p.strip().upper().startswith("DC=")
    ]
    return ".".join(parts)


def _parse_username(raw: str) -> tuple[str, Optional[str]]:
    """Parse username, returning (short_name, domain_hint_or_None).

    DOMAIN\\user   → ('user', 'DOMAIN')
    user@corp.local → ('user', 'corp.local')
    user            → ('user', None)
    """
    if "\\" in raw:
        domain, name = raw.split("\\", 1)
        return name.strip(), domain.strip().upper()
    if "@" in raw:
        name, domain = raw.split("@", 1)
        return name.strip(), domain.strip().lower()
    return raw.strip(), None


def _domain_matches(domain_cfg: dict, hint: str) -> bool:
    """Check if a domain config entry matches a domain hint (NetBIOS or UPN suffix)."""
    hint_upper = hint.upper()
    hint_lower = hint.lower()

    # NetBIOS / short domain name
    name = domain_cfg.get("name", domain_cfg.get("domain", "")).upper()
    if name and hint_upper == name:
        return True

    # UPN suffix derived from base_dn
    upn = _base_dn_to_upn_suffix(domain_cfg.get("base_dn", ""))
    if upn and hint_lower == upn.lower():
        return True

    # Explicit upn_suffix override
    explicit = domain_cfg.get("upn_suffix", "")
    if explicit and hint_lower == explicit.lower():
        return True

    return False


def _get_domain_configs(cfg: dict) -> list[dict]:
    """Normalise single-domain and multi-domain configs into a uniform list.

    Each entry inherits top-level admin_groups / access_groups if not set locally.
    """
    top_admin  = cfg.get("admin_groups")  or []
    top_access = cfg.get("access_groups") or []

    if "domains" in cfg:
        result = []
        for d in cfg["domains"]:
            entry = dict(d)
            entry.setdefault("admin_groups",  top_admin)
            entry.setdefault("access_groups", top_access)
            result.append(entry)
        return result

    # Single-domain (backward compatible)
    return [cfg]


# ── Single-domain LDAP authentication ────────────────────────────────────────

class _LdapTaggedError(LdapAuthError):
    """Internal: carries a tag so the multi-domain loop knows whether to continue."""
    def __init__(self, tag: str, message: str) -> None:
        super().__init__(message)
        self.tag = tag


def _authenticate_one_domain(short_name: str, password: str, dcfg: dict) -> dict:
    """Try to authenticate short_name against a single domain config.

    Returns user dict on success.
    Raises _LdapTaggedError with a tag describing why it failed.
    """
    import ldap3                          # noqa: PLC0415  (lazy import)
    import ldap3.core.exceptions as lexc  # noqa: PLC0415

    server_url    = dcfg.get("server", "")
    domain        = dcfg.get("domain", dcfg.get("name", ""))
    base_dn       = dcfg.get("base_dn", "")
    admin_groups  = dcfg.get("admin_groups")  or []
    access_groups = dcfg.get("access_groups") or []
    bind_secret   = _resolve_secret(dcfg.get("bind_secret"))
    domain_label  = dcfg.get("name", domain or server_url)

    safe_name = ldap3.utils.conv.escape_filter_chars(short_name)

    # ── Подключение к серверу ─────────────────────────────────────────────
    try:
        server = ldap3.Server(server_url, get_info=ldap3.ALL, connect_timeout=5)
    except Exception as exc:
        raise _LdapTaggedError(
            _TAG_CONN_ERROR,
            f"[{domain_label}] Не удалось создать объект сервера «{server_url}»: {exc}",
        )

    if bind_secret:
        # ── Двухфазная: сервисный аккаунт → поиск DN → ре-бинд пользователя ─

        svc_login = (
            f"{bind_secret['domain']}\\{bind_secret['username']}"
            if bind_secret.get("domain") else bind_secret["username"]
        )
        try:
            svc_conn = ldap3.Connection(
                server,
                user=svc_login,
                password=bind_secret["password"],
                auto_bind=ldap3.AUTO_BIND_NO_TLS,
                authentication=ldap3.NTLM if bind_secret.get("domain") else ldap3.SIMPLE,
            )
            svc_ok = svc_conn.bind()
        except lexc.LDAPSocketOpenError as exc:
            raise _LdapTaggedError(
                _TAG_CONN_ERROR,
                f"[{domain_label}] Нет соединения с сервером «{server_url}»: {exc}",
            )
        except Exception as exc:
            raise _LdapTaggedError(
                _TAG_CONN_ERROR,
                f"[{domain_label}] Ошибка подключения к серверу «{server_url}»: {exc}",
            )

        if not svc_ok:
            raise _LdapTaggedError(
                _TAG_CFG_ERROR,
                f"[{domain_label}] Сервисный аккаунт «{svc_login}» не прошёл "
                f"аутентификацию в AD.\n"
                f"Проверьте параметр bind_secret (id={dcfg.get('bind_secret')}) "
                f"и соответствующую запись в secret.yaml.",
            )

        # Поиск пользователя по sAMAccountName
        svc_conn.search(
            base_dn,
            f"(sAMAccountName={safe_name})",
            attributes=["distinguishedName", "memberOf"],
        )
        if not svc_conn.entries:
            svc_conn.unbind()
            raise _LdapTaggedError(
                _TAG_NOT_FOUND,
                f"[{domain_label}] Пользователь «{short_name}» не найден "
                f"(base_dn: {base_dn}).",
            )

        user_dn    = str(svc_conn.entries[0].distinguishedName)
        raw_groups = svc_conn.entries[0].memberOf
        member_of  = list(raw_groups) if raw_groups else []
        svc_conn.unbind()

        # Проверка пароля ре-биндом от имени пользователя
        try:
            user_conn = ldap3.Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=ldap3.AUTO_BIND_NO_TLS,
                authentication=ldap3.SIMPLE,
            )
            pwd_ok = user_conn.bind()
        except Exception as exc:
            raise _LdapTaggedError(
                _TAG_CONN_ERROR,
                f"[{domain_label}] Ошибка проверки пароля: {exc}",
            )

        if not pwd_ok:
            raise _LdapTaggedError(
                _TAG_WRONG_PWD,
                f"Неверный пароль для пользователя «{short_name}» "
                f"в домене {domain_label}.",
            )
        user_conn.unbind()

    else:
        # ── Однофазная: NTLM-бинд учётными данными пользователя ──────────
        bind_user = f"{domain}\\{short_name}" if domain else short_name
        try:
            conn = ldap3.Connection(
                server,
                user=bind_user,
                password=password,
                auto_bind=ldap3.AUTO_BIND_NO_TLS,
                authentication=ldap3.NTLM if domain else ldap3.SIMPLE,
            )
            ok = conn.bind()
        except lexc.LDAPSocketOpenError as exc:
            raise _LdapTaggedError(
                _TAG_CONN_ERROR,
                f"[{domain_label}] Нет соединения с сервером «{server_url}»: {exc}",
            )
        except Exception as exc:
            raise _LdapTaggedError(
                _TAG_CONN_ERROR,
                f"[{domain_label}] Ошибка подключения: {exc}",
            )

        if not ok:
            raise _LdapTaggedError(
                _TAG_WRONG_PWD,
                f"[{domain_label}] Не удалось войти как «{bind_user}».\n"
                f"Проверьте имя пользователя и пароль.\n"
                f"Подсказка: для точной диагностики настройте bind_secret в ldap.yaml.",
            )

        conn.search(
            base_dn,
            f"(sAMAccountName={safe_name})",
            attributes=["memberOf"],
        )
        member_of: list[str] = []
        if conn.entries:
            raw = conn.entries[0].memberOf
            member_of = list(raw) if raw else []
        conn.unbind()

    # ── Проверка групп доступа ────────────────────────────────────────────
    is_admin   = any(g in member_of for g in admin_groups)
    has_access = is_admin or any(g in member_of for g in access_groups)

    if (admin_groups or access_groups) and not has_access:
        allowed = ", ".join((admin_groups + access_groups)[:3])
        raise _LdapTaggedError(
            _TAG_NO_ACCESS,
            f"Пользователь «{short_name}» успешно аутентифицирован в "
            f"{domain_label}, но не входит ни в одну из групп с доступом.\n"
            f"Необходимые группы (первые три): {allowed}\n"
            f"Обратитесь к администратору для получения доступа.",
        )

    return {"username": short_name, "is_admin": is_admin, "auth_type": "ldap"}


# ── Multi-domain LDAP dispatcher ──────────────────────────────────────────────

def _authenticate_ldap(username: str, password: str, cfg: dict) -> dict:
    """Authenticate against one or multiple AD domains.

    Parses domain hint from username (DOMAIN\\user or user@domain.com).
    If hint given → tries only matching domains.
    If no hint    → tries all configured domains; stops on first definitive result.

    Raises LdapAuthError on failure.
    """
    try:
        import ldap3  # noqa: F401  check installation before anything else
    except ImportError:
        raise LdapAuthError(
            "Для доменной авторизации требуется пакет ldap3.\n"
            "Установите его: pip install ldap3"
        )

    short_name, domain_hint = _parse_username(username)
    all_domains = _get_domain_configs(cfg)

    if domain_hint:
        candidates = [d for d in all_domains if _domain_matches(d, domain_hint)]
        if not candidates:
            known = ", ".join(
                d.get("name", d.get("domain", d.get("server", "?")))
                for d in all_domains
            )
            raise LdapAuthError(
                f"Домен «{domain_hint}» не найден в конфигурации ldap.yaml.\n"
                f"Настроенные домены: {known}"
            )
    else:
        candidates = all_domains

    # Errors that mean "try next domain"
    _CONTINUE_TAGS = {_TAG_NOT_FOUND, _TAG_CONN_ERROR}

    last_error: Optional[_LdapTaggedError] = None
    not_found_domains: list[str] = []
    conn_errors: list[str] = []

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
                conn_errors.append(label)

    # All domains exhausted without success
    parts: list[str] = []
    if not_found_domains:
        parts.append(
            f"Пользователь «{short_name}» не найден в домен(ах): "
            + ", ".join(not_found_domains)
        )
    if conn_errors:
        parts.append(
            "Не удалось подключиться к серверам: " + ", ".join(conn_errors)
        )

    if not parts and last_error:
        raise LdapAuthError(str(last_error)) from None

    raise LdapAuthError(
        "\n".join(parts) or f"Пользователь «{short_name}» не найден."
    )


# ── Main authenticate entry point ─────────────────────────────────────────────

def authenticate(username: str, password: str) -> Optional[dict]:
    """Authenticate user. Priority: Local → LDAP.

    Returns {username, is_admin, auth_type} on success.
    Raises AuthError (or LdapAuthError) with a user-visible message on failure.
    Returns None only when auth is not configured (no ldap.yaml).
    """
    cfg = load_auth_config()
    if not cfg:
        return None   # auth not configured — caller should allow access

    # 1. Local: check by username first ───────────────────────────────────────
    if cfg.get("Local"):
        users = _load_local_users()
        local_entry = next((u for u in users if u.get("username") == username), None)
        if local_entry is not None:
            if str(local_entry.get("password", "")) == password:
                return {
                    "username":  username,
                    "is_admin":  bool(local_entry.get("is_admin", False)),
                    "auth_type": "local",
                }
            # Username found locally but password wrong — don't fall through to AD
            raise AuthError("Неверный пароль.")

    # 2. LDAP: username not in local list (or Local disabled) ─────────────────
    if cfg.get("LDAP"):
        return _authenticate_ldap(username, password, cfg)

    # Username not found anywhere
    raise AuthError(
        f"Пользователь «{username}» не найден.\n"
        f"Проверьте имя пользователя или обратитесь к администратору."
    )
