"""Table-scoped, name-squat-safe SQLite index migrations."""
import hashlib
import re


_SQL_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INDEX_NAME_DIGEST_CHARACTERS = 12


def _quoted_schema_identifier(identifier):
    if not isinstance(identifier, str) or not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQLite schema identifier: {identifier!r}")
    return f'"{identifier}"'


def table_index_coverages(connection, table):
    """Return ordered columns and uniqueness for indexes physically attached to ``table``."""
    _quoted_schema_identifier(table)
    coverages = []
    for index_row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if index_row[4]:
            # A predicate-limited index cannot cover a whole-table requirement.
            continue
        index_name = index_row[1]
        columns = tuple(
            info_row[2]
            for info_row in connection.execute(
                f"PRAGMA index_info({_quoted_schema_identifier(index_name)})"
            ).fetchall()
        )
        coverages.append((columns, bool(index_row[2]), index_name))
    return tuple(coverages)


def _available_derived_index_name(connection, table, columns, unique):
    semantic_stem = "_".join((table, "unique" if unique else "index", *columns))
    derivation_seed = semantic_stem
    while True:
        digest = hashlib.sha256(derivation_seed.encode("utf-8")).hexdigest()[:_INDEX_NAME_DIGEST_CHARACTERS]
        candidate = f"idx_{semantic_stem}_{digest}"
        occupant = connection.execute(
            "SELECT type,tbl_name,name FROM sqlite_schema WHERE name=?", (candidate,)
        ).fetchone()
        if occupant is None:
            return candidate
        derivation_seed = "|".join((derivation_seed, *(str(value) for value in occupant)))


def ensure_ordered_index_coverage(connection, table, columns, *, unique=False):
    """Ensure exact ordered-column coverage on the live table, independent of global index names."""
    ordered_columns = tuple(columns)
    if not ordered_columns:
        raise ValueError("an index requires at least one column")
    quoted_table = _quoted_schema_identifier(table)
    live_columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    }
    if not set(ordered_columns) <= live_columns:
        missing = tuple(column for column in ordered_columns if column not in live_columns)
        raise ValueError(f"index columns absent from {table}: {missing!r}")
    for existing_columns, existing_unique, existing_name in table_index_coverages(connection, table):
        if existing_columns == ordered_columns and (not unique or existing_unique):
            return existing_name
    index_name = _available_derived_index_name(connection, table, ordered_columns, unique)
    unique_sql = "UNIQUE " if unique else ""
    quoted_columns = ", ".join(_quoted_schema_identifier(column) for column in ordered_columns)
    connection.execute(
        f"CREATE {unique_sql}INDEX {_quoted_schema_identifier(index_name)} "
        f"ON {quoted_table}({quoted_columns})"
    )
    return index_name


def ensure_ordered_indexes_coverage(connection, table, index_specs):
    """Batch form of :func:`ensure_ordered_index_coverage`, with one table index inventory read."""
    quoted_table = _quoted_schema_identifier(table)
    live_columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    }
    coverages = list(table_index_coverages(connection, table))
    ensured_names = []
    for columns, unique in index_specs:
        ordered_columns = tuple(columns)
        if not ordered_columns or not set(ordered_columns) <= live_columns:
            missing = tuple(column for column in ordered_columns if column not in live_columns)
            raise ValueError(f"index columns absent from {table}: {missing!r}")
        existing_name = next(
            (name for indexed_columns, indexed_unique, name in coverages
             if indexed_columns == ordered_columns and (not unique or indexed_unique)),
            None,
        )
        if existing_name is not None:
            ensured_names.append(existing_name)
            continue
        index_name = _available_derived_index_name(connection, table, ordered_columns, unique)
        unique_sql = "UNIQUE " if unique else ""
        quoted_columns = ", ".join(_quoted_schema_identifier(column) for column in ordered_columns)
        connection.execute(
            f"CREATE {unique_sql}INDEX {_quoted_schema_identifier(index_name)} "
            f"ON {quoted_table}({quoted_columns})"
        )
        coverages.append((ordered_columns, unique, index_name))
        ensured_names.append(index_name)
    return tuple(ensured_names)
