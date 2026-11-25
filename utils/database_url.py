import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ALEMBIC_DATABASE_URL_PLACEHOLDER = "${DATABASE_URL}"


def to_sync_database_url(database_url: str) -> str:
    return re.sub(r"^postgresql\+(?:asyncpg|psycopg|psycopg2):", "postgresql:", database_url)


def to_async_database_url(database_url: str) -> str:
    sync_url = to_sync_database_url(database_url)
    async_url = re.sub(r"^postgresql:", "postgresql+asyncpg:", sync_url)

    parsed = urlsplit(async_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    has_sslmode = any(key.lower() == "sslmode" for key, _ in query)
    has_asyncpg_ssl = any(key.lower() == "ssl" for key, _ in query)
    translated_query = []
    added_ssl = False
    for key, value in query:
        normalized_key = key.lower()
        if normalized_key == "channel_binding":
            continue
        if normalized_key == "sslmode":
            if not has_asyncpg_ssl and not added_ssl:
                translated_query.append(("ssl", value))
                added_ssl = True
        else:
            translated_query.append((key, value))
    if has_sslmode or len(translated_query) != len(query):
        async_url = urlunsplit(parsed._replace(query=urlencode(translated_query)))

    return async_url


def resolve_alembic_database_url(
    configured_url: str | None,
    env_database_url: str | None,
) -> str | None:
    candidate = configured_url
    if not candidate or candidate == ALEMBIC_DATABASE_URL_PLACEHOLDER:
        candidate = env_database_url
    if not candidate:
        return None
    return to_sync_database_url(candidate)
