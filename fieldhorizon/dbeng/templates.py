"""
The only statements the privileged executor can send, and the patterns that recognise them.

One module holds both sides so they cannot drift apart:

* the executor composes each statement from a fixed template and validated
  identifiers (psycopg.sql, never string formatting of untrusted text);
* bench/lab provisioning installs a server-side event trigger that refuses,
  for the operator role, any DDL whose text does not match these same
  patterns exactly -- before anything is created (an after-the-fact refusal
  of CREATE INDEX CONCURRENTLY was measured to leave an invalid index behind).

Identifiers are restricted to lower-case unquoted-safe names. Anything else
-- quotes, semicolons, upper case, non-ASCII look-alikes, over-long names --
is refused before it can reach SQL at all.
"""
from __future__ import annotations

import re

from psycopg import sql

IDENTIFIER_PATTERN = r"[a-z_][a-z0-9_]{0,62}"
# IndexSpec.name: "fhx_" + stem, cut so the whole name stays within PostgreSQL's 63 bytes.
INDEX_NAME_PATTERN = r"fhx_[a-z0-9_]{1,59}"
MAX_INDEX_COLUMNS = 3

IDENTIFIER_RE = re.compile(rf"^{IDENTIFIER_PATTERN}$")
INDEX_NAME_RE = re.compile(rf"^{INDEX_NAME_PATTERN}$")

# Server-side recognisers (POSIX regexes, also valid Python regexes) for the exact text psycopg emits.
_Q = rf'"{IDENTIFIER_PATTERN}"'
CREATE_INDEX_TEXT = (
    rf'^CREATE INDEX CONCURRENTLY "{INDEX_NAME_PATTERN}" ON {_Q}\.{_Q} USING btree '
    rf'\({_Q}(, {_Q}){{0,{MAX_INDEX_COLUMNS - 1}}}\)$'
)
DROP_INDEX_TEXT = rf'^DROP INDEX CONCURRENTLY IF EXISTS {_Q}\."{INDEX_NAME_PATTERN}"$'
COMMENT_INDEX_TEXT = rf"^COMMENT ON INDEX {_Q}\.\"{INDEX_NAME_PATTERN}\" IS '[^'\\]*'$"


class InvalidIdentifier(ValueError):
    pass


def check_identifier(value: str, what: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
        raise InvalidIdentifier(f"{what} {value!r} is not an accepted identifier (lower-case [a-z_][a-z0-9_]*, at most 63 characters)")
    return value


def check_index_name(value: str) -> str:
    if not isinstance(value, str) or not INDEX_NAME_RE.match(value):
        raise InvalidIdentifier(f"index name {value!r} is not a Field Horizon index name (fhx_...)")
    return value


def create_index_statement(schema: str, table: str, columns: tuple[str, ...], name: str) -> sql.Composed:
    check_identifier(schema, "schema")
    check_identifier(table, "table")
    check_index_name(name)
    if not 0 < len(columns) <= MAX_INDEX_COLUMNS:
        raise InvalidIdentifier(f"{len(columns)} columns; 1 to {MAX_INDEX_COLUMNS} allowed")
    for c in columns:
        check_identifier(c, "column")
    return sql.SQL("CREATE INDEX CONCURRENTLY {name} ON {schema}.{table} USING btree ({cols})").format(
        name=sql.Identifier(name), schema=sql.Identifier(schema), table=sql.Identifier(table),
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )


def drop_index_statement(schema: str, name: str) -> sql.Composed:
    check_identifier(schema, "schema")
    check_index_name(name)
    return sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}").format(schema=sql.Identifier(schema), name=sql.Identifier(name))


def comment_index_statement(schema: str, name: str, marker_json: str) -> sql.Composed:
    check_identifier(schema, "schema")
    check_index_name(name)
    if "'" in marker_json or "\\" in marker_json:
        raise InvalidIdentifier("marker text must not contain quotes or backslashes")
    return sql.SQL("COMMENT ON INDEX {schema}.{name} IS {marker}").format(
        schema=sql.Identifier(schema), name=sql.Identifier(name), marker=sql.Literal(marker_json)
    )


def set_role_statement(role: str) -> sql.Composed:
    check_identifier(role, "role")
    return sql.SQL("SET ROLE {}").format(sql.Identifier(role))


def analyze_statement(schema: str, table: str) -> sql.Composed:
    check_identifier(schema, "schema")
    check_identifier(table, "table")
    return sql.SQL("ANALYZE {}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def vacuum_statement(schema: str, table: str) -> sql.Composed:
    """Not DDL (no event trigger fires): an owner power the operator holds anyway; sent only as this template."""
    check_identifier(schema, "schema")
    check_identifier(table, "table")
    return sql.SQL("VACUUM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def matches_server_template(text: str) -> bool:
    """What the server guard decides for a statement text (used by tests to prove the two layers agree)."""
    t = text.strip().rstrip(";").strip()
    return any(re.match(p, t) for p in (CREATE_INDEX_TEXT, DROP_INDEX_TEXT, COMMENT_INDEX_TEXT))
