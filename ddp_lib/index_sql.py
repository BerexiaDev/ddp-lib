"""Strict PostgreSQL index requests; raw expression text is never rewritten."""
from dataclasses import dataclass
import json

from pglast import ast, parse_sql, scan


def quote(value):
    if not isinstance(value, str) or not value or '\x00' in value or len(value.encode('utf8')) > 63:
        raise ValueError('PostgreSQL identifiers must contain 1–63 UTF-8 bytes')
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class IndexDefinition:
    schema: str
    table: str
    index_name: str
    unique: bool
    only: bool
    concurrently: bool
    body: str
    signature: dict

    def render(self, schema=None, table=None, *, concurrently=None, name=None, only=None):
        concurrent = self.concurrently if concurrently is None else concurrently
        scope = self.only if only is None else only
        return ('CREATE ' + ('UNIQUE ' if self.unique else '') + 'INDEX '
                + ('CONCURRENTLY ' if concurrent else '') + quote(name or self.index_name)
                + ' ON ' + ('ONLY ' if scope else '') + quote(schema or self.schema) + '.'
                + quote(table or self.table) + ' ' + self.body)


def _structure(value):
    if isinstance(value, dict):
        return {k: _structure(v) for k, v in value.items() if k not in {'location', 'stmt_location', 'stmt_len'}}
    if isinstance(value, (tuple, list)):
        return [_structure(v) for v in value]
    return value


def parse_index(statement):
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError('A CREATE INDEX statement is required')
    try:
        tokens = scan(statement)
        # The PostgreSQL parser truncates long identifiers. Reject BEFORE parsing.
        for token in tokens:
            if token.name in {'IDENT', 'UIDENT'}:
                raw = statement[token.start:token.end + 1]
                if token.name == 'UIDENT':
                    raise ValueError('Use literal UTF-8 quoted identifiers instead of Unicode escapes')
                name = raw[1:-1].replace('""', '"') if raw.startswith('"') else raw
                quote(name)
        parsed = parse_sql(statement)
    except Exception as exc:
        raise ValueError(f'Invalid CREATE INDEX: {exc}') from exc
    if len(parsed) != 1 or not isinstance(parsed[0].stmt, ast.IndexStmt):
        raise ValueError('Exactly one CREATE INDEX statement is required')
    node = parsed[0].stmt
    rel = node.relation
    if not node.idxname or not rel.schemaname or rel.catalogname:
        raise ValueError('A named index on an explicit schema.table target is required')
    for name in (node.idxname, rel.schemaname, rel.relname):
        quote(name)
    # RangeVar location and scanner offsets are character offsets in pglast.
    target_tokens = [t for t in tokens if t.start >= rel.location]
    if len(target_tokens) < 4 or target_tokens[1].name != 'ASCII_46':
        raise ValueError('Expected a schema-qualified index target')
    body = statement[target_tokens[2].end + 1:].strip().rstrip(';').rstrip()
    structure = node()
    ignored = {'idxname', 'relation', 'concurrent', 'if_not_exists', 'idxcomment',
               'oldNumber', 'oldCreateSubid', 'oldFirstRelfilelocatorSubid'}
    signature = _structure({k: v for k, v in structure.items() if k not in ignored})
    signature['only'] = not rel.inh
    # Force JSON types, so Mongo round trips retain identical comparisons.
    signature = json.loads(json.dumps(signature))
    return IndexDefinition(rel.schemaname, rel.relname, node.idxname, bool(node.unique),
                           not rel.inh, bool(node.concurrent), body, signature)
