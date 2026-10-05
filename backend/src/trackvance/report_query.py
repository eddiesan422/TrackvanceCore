"""Fail-closed SQL AST policy and the shared guided query compiler.

0.8 supports a single SELECT over named sources. CTEs/subqueries and SELECT *
are deliberately rejected rather than advertised with incomplete authorization.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .operations_common import OperationError

MAX_SOURCES = 8
MAX_COLUMNS = 100
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,62}\Z")
ALLOWED_NODES = frozenset({
    "select", "alias", "identifier", "column", "table", "from", "join",
    "where", "group", "having", "order", "ordered", "limit", "offset",
    "literal", "null", "boolean", "placeholder", "paren", "and", "or", "not",
    "eq", "neq", "gt", "gte", "lt", "lte", "is", "in", "between",
    "add", "sub", "mul", "div", "mod", "neg", "count", "sum", "avg", "min",
    "max", "coalesce", "cast", "datatype", "datatypeparam", "case", "if",
    "abs", "round", "distinct", "star",
})


def fail(code: str, message: str, details: Any = None) -> NoReturn:
    raise OperationError(422, code, message, details)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def quote(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 240 or "\x00" in name:
        fail("REPORT_IDENTIFIER", "Identificador inválido.")
    return '"' + name.replace('"', '""') + '"'


def column_ref(alias: str, name: str) -> str:
    return f"{quote(alias)}.{quote(name)}"


def typed_parameters(raw: list[dict]) -> dict:
    if not isinstance(raw, list) or len(raw) > 100:
        fail("REPORT_PARAMETERS", "Se admiten hasta 100 parámetros tipados.")
    result: dict = {}
    for item in raw:
        if not isinstance(item, dict):
            fail("REPORT_PARAMETERS", "Cada parámetro requiere nombre, tipo y valor.")
        name, kind, value = item.get("name"), item.get("type"), item.get("value")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in result:
            fail("REPORT_PARAMETERS", "Los nombres de parámetros deben ser únicos y válidos.")
        if kind not in ("TEXT", "INTEGER", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN"):
            fail("REPORT_PARAMETER_TYPE", "Tipo de parámetro no admitido.")
        try:
            if value is None:
                result[name] = None
            elif kind == "TEXT" and isinstance(value, str) and len(value.encode()) <= 65536:
                result[name] = value
            elif kind == "INTEGER" and (type(value) is int or isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value)) and -(2**63) <= int(value) < 2**63:
                result[name] = int(value)
            elif kind == "DECIMAL" and isinstance(value, str):
                number = Decimal(value)
                exponent = number.as_tuple().exponent
                if (not number.is_finite() or not isinstance(exponent, int)
                        or max(0, len(number.as_tuple().digits) + exponent) + max(0, -exponent) > 38):
                    raise ValueError()
                result[name] = {"type": "DECIMAL", "value": value}
            elif kind == "DATE" and isinstance(value, str):
                result[name] = {"type": "DATE", "value": date.fromisoformat(value).isoformat()}
            elif kind == "TIMESTAMP" and isinstance(value, str):
                result[name] = {"type": "TIMESTAMP", "value": datetime.fromisoformat(value).isoformat()}
            elif kind == "BOOLEAN" and (type(value) is bool or value in {"true", "false"}):
                result[name] = value if type(value) is bool else value == "true"
            else:
                raise ValueError()
        except (ValueError, InvalidOperation, TypeError):
            fail("REPORT_PARAMETER_TYPE", f"Valor incompatible con el parámetro {name}.")
    return result


def _conjunction(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjunction(node.this)
    if isinstance(node, exp.And):
        return _conjunction(node.this) + _conjunction(node.expression)
    return [node]


def validate_sql(sql: str, schemas: dict[str, list[dict]], join_policy: list[dict],
                 parameters: dict) -> dict:
    if not isinstance(sql, str) or not 1 <= len(sql.encode()) <= 65536:
        fail("REPORT_SQL_SIZE", "La consulta debe tener entre 1 y 65.536 bytes.")
    try:
        expressions = sqlglot.parse(sql, read="duckdb")
    except ParseError:
        fail("REPORT_SQL_INVALID", "La consulta SQL no es válida para el dialecto admitido.")
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        fail("REPORT_SQL_STATEMENT", "Solo se admite una sentencia SELECT.")
    tree = expressions[0]
    if len(list(tree.walk())) > 4000:
        fail("REPORT_SQL_COMPLEXITY", "La consulta supera la complejidad admitida.")
    for node in tree.walk():
        if node.key not in ALLOWED_NODES:
            fail("REPORT_SQL_NODE", "Operación SQL no admitida.", {"operation": node.key})
        if isinstance(node, exp.Select) and node is not tree:
            fail("REPORT_SQL_SUBQUERY", "CTE y subconsultas no están admitidas en 0.8.0.")
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            fail("REPORT_SQL_STAR", "Selecciona columnas explícitas; no se admite SELECT *.")
        if isinstance(node, exp.Placeholder) and node.name not in parameters:
            fail("REPORT_PARAMETER_MISSING", "Falta un parámetro tipado de la consulta.")
        if isinstance(node, exp.Cast):
            kind = node.args["to"]
            if kind.this not in {exp.DataType.Type.TEXT, exp.DataType.Type.VARCHAR,
                                 exp.DataType.Type.BIGINT, exp.DataType.Type.INT,
                                 exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE,
                                 exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ,
                                 exp.DataType.Type.DECIMAL}:
                fail("REPORT_CAST_TYPE", "Conversión no admitida; no se permite perder precisión.")
            if kind.this == exp.DataType.Type.DECIMAL:
                try:
                    values = [int(p.this.this) for p in kind.expressions]
                except (ValueError, TypeError, AttributeError):
                    fail("REPORT_DECIMAL_PRECISION", "DECIMAL requiere precisión y escala enteras explícitas.")
                if len(values) != 2 or not 1 <= values[0] <= 38 or not 0 <= values[1] <= values[0]:
                    fail("REPORT_DECIMAL_PRECISION", "DECIMAL requiere precisión y escala explícitas hasta 38.")
    allowed_columns = {a: {c["name"]: c for c in cols} for a, cols in schemas.items()}
    projection_names = [p.alias_or_name for p in tree.expressions]
    if (not projection_names or len(projection_names) > MAX_COLUMNS
            or any(not name or name.casefold().startswith("__tv_") for name in projection_names)
            or len({n.casefold() for n in projection_names}) != len(projection_names)):
        fail("REPORT_RESULT_COLUMNS", "Cada columna de resultado requiere un nombre único explícito.")
    tables = list(tree.find_all(exp.Table))
    aliases: dict[str, str] = {}
    for table in tables:
        if table.db or table.catalog or table.name not in schemas or not isinstance(table.this, exp.Identifier):
            fail("REPORT_SQL_SOURCE", "La consulta solo puede leer los alias autorizados.")
        alias = table.alias_or_name
        if alias != table.name or alias in aliases:
            fail("REPORT_SQL_ALIAS", "Usa los alias congelados sin renombrarlos ni repetirlos.")
        aliases[alias] = table.name
    if set(aliases) != set(schemas):
        fail("REPORT_SQL_SOURCE", "La consulta debe usar exactamente las fuentes seleccionadas.")
    for column in tree.find_all(exp.Column):
        if column.table:
            if column.table not in allowed_columns or column.name not in allowed_columns[column.table]:
                fail("REPORT_SQL_COLUMN", "La consulta referencia una columna no autorizada.")
        else:
            parent = column.parent
            in_order = False
            while parent is not None and parent is not tree:
                in_order = in_order or isinstance(parent, exp.Order)
                parent = parent.parent
            if not in_order or column.name not in projection_names:
                fail("REPORT_SQL_QUALIFICATION", "Califica cada columna con su alias de fuente.")
    for aggregate in tree.find_all(exp.Avg):
        if any(allowed_columns[c.table][c.name].get("logical_type") == "DECIMAL"
               for c in aggregate.find_all(exp.Column)):
            fail("REPORT_AVG_DECIMAL", "AVG decimal del motor retorna float; usa SUM y COUNT para conservar exactitud.")
    joins = tree.args.get("joins") or []
    if len(joins) != len(schemas) - 1 or len(join_policy) != len(joins):
        fail("REPORT_JOIN_POLICY", "Cada cruce necesita una política de cardinalidad explícita.")
    source = tree.args.get("from_")
    if not source or not isinstance(source.this, exp.Table):
        fail("REPORT_SQL_SOURCE", "Selecciona una fuente base autorizada.")
    available = {source.this.name}
    normalized_joins = []
    prefix = f"FROM {quote(source.this.name)}"
    for join, policy in zip(joins, join_policy, strict=True):
        right = join.this.name if isinstance(join.this, exp.Table) else ""
        side = join.args.get("side") or "INNER"
        kind = join.args.get("kind") or ""
        if side not in {"INNER", "LEFT", "RIGHT", "FULL"} or kind not in {"", "OUTER", "INNER"}:
            fail("REPORT_JOIN_TYPE", "Solo se admiten INNER, LEFT, RIGHT y FULL por igualdad.")
        if join.args.get("method") or join.args.get("using") or not join.args.get("on") or right in available:
            fail("REPORT_JOIN_RELATION", "Cada cruce requiere una relación explícita entre fuentes distintas.")
        pairs = []
        for condition in _conjunction(join.args["on"]):
            if not isinstance(condition, exp.EQ) or not isinstance(condition.this, exp.Column) or not isinstance(condition.expression, exp.Column):
                fail("REPORT_JOIN_RELATION", "Las llaves del JOIN deben ser igualdades entre columnas de ambos lados.")
            left_col, right_col = condition.this, condition.expression
            if left_col.table == right:
                left_col, right_col = right_col, left_col
            if left_col.table not in available or right_col.table != right:
                fail("REPORT_JOIN_RELATION", "Una llave debe vincular la fuente nueva con las anteriores.")
            left_type = allowed_columns[left_col.table][left_col.name].get("logical_type")
            right_type = allowed_columns[right][right_col.name].get("logical_type")
            if left_type != right_type:
                fail("REPORT_JOIN_KEY_TYPE", "Las llaves deben tener el mismo tipo lógico; convierte explícitamente antes de crear fuentes compatibles.")
            pairs.append({"left_alias": left_col.table, "left_column": left_col.name,
                          "right_column": right_col.name})
        if not pairs or len(pairs) > 16 or len({digest(p) for p in pairs}) != len(pairs):
            fail("REPORT_JOIN_KEYS", "Se admiten entre 1 y 16 pares de llaves distintos.")
        expected = policy.get("expected_cardinality", "1:1")
        allow_nm = policy.get("allow_many_to_many", False)
        if expected not in {"1:1", "1:N", "N:1", "N:M"} or type(allow_nm) is not bool:
            fail("REPORT_CARDINALITY", "Declara cardinalidad y autorización N:M válidas.")
        if expected == "N:M" and not allow_nm:
            fail("REPORT_MANY_TO_MANY", "N:M requiere autorización explícita en la definición.")
        clause = join.sql(dialect="duckdb")
        normalized_joins.append({"right_alias": right, "type": side, "keys": pairs,
                                 "expected_cardinality": expected, "allow_many_to_many": allow_nm,
                                 "left_from": prefix, "clause": clause})
        prefix += " " + clause
        available.add(right)
    # A total business-value order suffices for grouped rows; equal results are
    # indistinguishable. Ordinary rows additionally tie-break by verified source position.
    grouped = bool(tree.args.get("group")) or any(isinstance(n, exp.AggFunc) for n in tree.walk()) or bool(tree.args.get("distinct"))
    order = tree.args.get("order") or exp.Order(expressions=[])
    if grouped:
        ties = [exp.Ordered(this=exp.column(n, quoted=True), nulls_first=False) for n in projection_names]
    else:
        ties = [exp.Ordered(this=exp.column("__tv_source_pos", table=a, quoted=True), nulls_first=False)
                for a in aliases]
    order.set("expressions", list(order.expressions) + ties)
    tree.set("order", order)
    # Named placeholders are rendered with $name by DuckDB's dialect.
    return {"sql": tree.sql(dialect="duckdb"), "joins": normalized_joins,
            "columns": projection_names, "used_columns": {a: sorted({c.name for c in tree.find_all(exp.Column)
                if c.table == a and c.name != "__tv_source_pos"}) for a in aliases},
            "query_limited": tree.args.get("limit") is not None,
            "parameters": parameters}


def _filter(group: dict, schemas: dict, params: dict, default_alias: str | None = None, depth: int = 0) -> str:
    if depth > 8:
        fail("REPORT_FILTER_DEPTH", "El filtro supera ocho niveles de grupos.")
    if not isinstance(group, dict):
        fail("REPORT_FILTER", "Filtro inválido.")
    if "conditions" in group:
        operator = group.get("operator", "AND")
        conditions = group["conditions"]
        if operator not in {"AND", "OR"} or not isinstance(conditions, list) or not 1 <= len(conditions) <= 100:
            fail("REPORT_FILTER", "Los grupos de filtros requieren condiciones Y/O.")
        return "(" + f" {operator} ".join(_filter(c, schemas, params, default_alias, depth + 1) for c in conditions) + ")"
    alias = group.get("source_alias") or default_alias
    column, operator = group.get("column"), group.get("operator")
    if not isinstance(alias, str):
        fail("REPORT_FILTER_COLUMN", "El filtro requiere un alias de fuente válido.")
    columns = {c["name"]: c for c in schemas.get(alias, [])}
    if not isinstance(alias, str) or not isinstance(column, str) or column not in columns or (default_alias is not None and alias != default_alias):
        fail("REPORT_FILTER_COLUMN", "El filtro debe usar una columna autorizada de su fuente.")
    reference = column_ref(alias, column)
    if operator in {"IS_NULL", "IS_NOT_NULL"}:
        return reference + (" IS NULL" if operator == "IS_NULL" else " IS NOT NULL")
    if operator not in {"EQ", "NE", "GT", "GE", "LT", "LE", "IN"}:
        fail("REPORT_FILTER_OPERATOR", "Operador de filtro no admitido.")
    values = group.get("value") if operator == "IN" else [group.get("value")]
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        fail("REPORT_FILTER_VALUE", "El filtro IN requiere entre 1 y 100 valores.")
    placeholders = []
    logical = columns[column].get("logical_type", "STRING")
    parameter_type = {"STRING": "TEXT", "INTEGER": "INTEGER", "INT64": "INTEGER", "DECIMAL": "DECIMAL",
                      "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "BOOLEAN": "BOOLEAN"}.get(logical)
    if parameter_type is None:
        fail("REPORT_FILTER_TYPE", "Tipo lógico no admitido para filtros.")
    for value in values:
        if value is None:
            fail("REPORT_FILTER_NULL", "Usa Es nulo o No es nulo para comparar nulos.")
        name = f"tv_filter_{len(params)}"
        params.update(typed_parameters([{"name": name, "type": parameter_type, "value": value}]))
        placeholders.append("$" + name)
    if operator == "IN":
        return reference + " IN (" + ",".join(placeholders) + ")"
    return reference + " " + {"EQ": "=", "NE": "<>", "GT": ">", "GE": ">=", "LT": "<", "LE": "<="}[operator] + " " + placeholders[0]


def compile_draft(draft: dict, schemas: dict[str, list[dict]]) -> dict:
    sources = draft.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        fail("REPORT_SOURCE_LIMIT", f"Se admiten entre 1 y {MAX_SOURCES} fuentes.")
    if any(not isinstance(source, dict) for source in sources):
        fail("REPORT_SOURCE_INVALID", "Cada fuente requiere un objeto con su alias.")
    aliases = [s.get("alias") for s in sources]
    if any(not isinstance(a, str) or not IDENTIFIER.fullmatch(a) or a.startswith("tv_") for a in aliases) or len({a.casefold() for a in aliases}) != len(aliases):
        fail("REPORT_ALIAS", "Los alias deben ser únicos y contener letras, números o guion bajo.")
    if set(aliases) != set(schemas):
        fail("REPORT_SOURCE_SCHEMA", "No se resolvió el esquema de cada fuente.")
    params = typed_parameters(draft.get("parameters", []))
    source_filters = {}
    raw_filters = draft.get("source_filters", {})
    if not isinstance(raw_filters, dict) or not set(raw_filters) <= set(schemas):
        fail("REPORT_SOURCE_FILTER", "Filtros previos de fuente inválidos.")
    for alias, group in raw_filters.items():
        if group:
            source_filters[alias] = _filter(group, schemas, params, alias)
    joins = draft.get("joins", [])
    if not isinstance(joins, list) or any(not isinstance(join, dict) for join in joins):
        fail("REPORT_JOIN_POLICY", "Cruces inválidos.")
    mode = draft.get("mode", "GUIDED")
    if mode == "SQL":
        plan = validate_sql(draft.get("sql", ""), schemas, joins, params)
    elif mode == "GUIDED":
        selected = draft.get("columns", [])
        if not isinstance(selected, list) or not selected:
            fail("REPORT_COLUMNS", "Selecciona al menos una columna de resultado.")
        projections = []
        for item in selected:
            if not isinstance(item, dict):
                fail("REPORT_COLUMN", "Cada columna debe indicar su fuente y nombre.")
            alias, column = item.get("source_alias"), item.get("column")
            if not isinstance(alias, str) or not isinstance(column, str) or alias not in schemas or column not in {c["name"] for c in schemas[alias]}:
                fail("REPORT_COLUMN", "Selecciona columnas autorizadas.")
            projections.append(column_ref(alias, column) + " AS " + quote(item.get("alias") or column))
        sql = "SELECT " + ", ".join(projections) + " FROM " + quote(aliases[0])
        for join in joins:
            left, right = join.get("left_alias"), join.get("right_alias")
            kind, pairs = join.get("type"), join.get("keys", [])
            if kind not in {"INNER", "LEFT", "RIGHT", "FULL"} or not isinstance(pairs, list) or not pairs or any(not isinstance(pair, dict) for pair in pairs):
                fail("REPORT_JOIN", "Completa tipo y llaves del cruce.")
            sql += f" {kind} JOIN {quote(right)} ON " + " AND ".join(
                column_ref(left, p.get("left_column")) + "=" + column_ref(right, p.get("right_column")) for p in pairs)
        if draft.get("post_filter"):
            sql += " WHERE " + _filter(draft["post_filter"], schemas, params)
        order = draft.get("order_by", [])
        if not isinstance(order, list) or any(not isinstance(item, dict) for item in order):
            fail("REPORT_ORDER", "El orden debe contener columnas explícitas.")
        if order:
            terms = []
            for item in order:
                direction = item.get("direction", "ASC")
                if direction not in {"ASC", "DESC"}:
                    fail("REPORT_ORDER", "Orden no admitido.")
                terms.append((column_ref(item["source_alias"], item["column"]) if item.get("source_alias")
                              else quote(item["column"])) + " " + direction + " NULLS LAST")
            sql += " ORDER BY " + ",".join(terms)
        plan = validate_sql(sql, schemas, joins, params)
    else:
        fail("REPORT_MODE", "Selecciona Constructor guiado o SQL.")
    plan["source_filters"] = source_filters
    for alias, condition in source_filters.items():
        fields = {c.name for c in sqlglot.parse_one(condition, read="duckdb").find_all(exp.Column)}
        plan["used_columns"][alias] = sorted(set(plan["used_columns"][alias]) | fields)
    plan["expected_schemas"] = schemas
    plan["query_hash"] = digest({"draft": draft, "plan": plan})
    return plan
