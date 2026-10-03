import io
import os
import re
import sys
import builtins
import time
import threading
import textwrap
import requests
import logging
import json
import tomllib
import configparser
import hashlib
import pathlib
import dataclasses
import contextlib
import faulthandler
import sqlite3
import tree_sitter
import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript
import tree_sitter_go
import mcp.server.mcpserver

# Set up logging for service.

class LogExceptionHandler(logging.StreamHandler):
    class ExceptionFormatter(logging.Formatter):
        def formatException(self, ei):
            return "ERROR " + super().formatException(ei).replace("\n", " ")

    def __init__(self, stream=None, fmt=None):
        super().__init__(stream=stream)
        self.setFormatter(LogExceptionHandler.ExceptionFormatter(fmt))

logging.basicConfig(
    handlers=[LogExceptionHandler(stream=sys.stdout, fmt="%(asctime)s %(levelname)s: %(message)s")],
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    force=True,
)

log = logging.getLogger()

# Native crashes (e.g. in a tree-sitter parser) can't be caught; at least print where they happened.
faulthandler.enable()

# Initialize configuration.

service_name: str = os.environ.get("SERVICE_NAME", "analyze-graph")
service_role: str = os.environ.get("SERVICE_ROLE", "analyze")
service_port: int = int(os.environ.get("PORT", "8000"))
endpoint_url: str = os.environ.get("ENDPOINT_URL", "")
registry_url: str = os.environ.get("REGISTRY_URL", "")

# Create the mcp server.

mcp = mcp.server.mcpserver.MCPServer(service_name, log_level="WARNING")

class GraphClient:
    """
    Builds knowledge graphs with tree-sitter parsers and answers queries
    against them, as stored in sqlite database.
    """

    _schema: str = """
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, language TEXT, sha256 TEXT);
        CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY, kind TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT REFERENCES files(path) ON DELETE CASCADE,
            start_line INT, end_line INT, parent_id TEXT,
            signature TEXT, doc TEXT, exported INT
        );
        CREATE TABLE IF NOT EXISTS refs (
            src_id TEXT, kind TEXT, target TEXT,
            file_path TEXT REFERENCES files(path) ON DELETE CASCADE,
            line INT, qualifier TEXT, receiver TEXT
        );
        CREATE TABLE IF NOT EXISTS bindings (
            file_path TEXT REFERENCES files(path) ON DELETE CASCADE,
            local TEXT, module TEXT, name TEXT, line INT
        );
        CREATE TABLE IF NOT EXISTS edges (
            src_id TEXT, dst_id TEXT, kind TEXT, file_path TEXT, line INT, confidence TEXT
        );
        CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file_path);
        CREATE INDEX IF NOT EXISTS refs_file ON refs(file_path);
        CREATE INDEX IF NOT EXISTS bindings_file ON bindings(file_path, local);
        CREATE INDEX IF NOT EXISTS edges_src ON edges(src_id, kind);
        CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst_id, kind);
        CREATE INDEX IF NOT EXISTS edges_file ON edges(file_path, kind);
    """

    @dataclasses.dataclass(frozen=True)
    class LanguageSpec:
        name: str
        language: tree_sitter.Language
        definitions: dict[str, str]                    # node type -> symbol kind
        calls: dict[str, str]                          # node type -> field holding the callee
        imports: dict[str, str]                        # node type -> field holding the module
        separator: str                                 # module path separator in imports: "." or "/"
        index_names: list[str]                         # file stems that stand for their directory
        inherits: dict[str, str]                       # definition node type -> type of the child holding its bases
        imported_names: dict[str, str]                 # import node type -> field holding names that may be submodules
        name_fields: dict[str, str]                    # definition node type -> field holding its name, when not "name"
        function_values: list[str]                     # value node types that make a variable a function
        type_annotations: list[str]                    # node types holding a type expression, e.g. `x: User`
        import_calls: list[str]                        # callee names whose string argument is a module, e.g. `require`
        decorators: list[str]                          # node types of decorators, which call the definition they apply to
        docstrings: bool                               # the first string in a body documents it
        default_exports: dict[str, str]                # unnamed `export default` value node type -> kind, named after its file
        object_values: list[str]                       # value node types that make a variable a namespace for its members
        receivers: dict[str, str]                      # definition node type -> field holding the receiver whose type qualifies it
        kinds_by_type: dict[str, str]                  # node type of a definition's "type" child -> kind, e.g. go's struct_type
        packages: bool                                 # imports name directories under a go.mod module, whose files share names
        declarations: dict[str, tuple[str, str, str]]  # node type -> fields of its names, type, and values, "" if none
        constructors: list[str]                        # value node types that construct a type or call what returns one, e.g. `new Store()`
        self_names: list[str]                          # names for the enclosing class, e.g. `self`, `this`
        parameter_properties: list[str]                # node types marking a parameter as an attribute, e.g. ts's `private`
        builtins: frozenset[str]                       # the language's own names, which bare calls mean unless defined, e.g. `len`
        builtin_methods: frozenset[str]                # methods of its own types, which calls on unknown values likely mean
        value_holders: dict[str, str]                  # node type -> field holding names used as values, "" for all children
        binders: dict[str, str]                        # node type -> field binding local names, "" for the node itself
        elements: list[str]                            # jsx element node types, whose capitalized names are components rendered
        embeddings: list[str]                          # node types that embed their type, when unnamed, as a base: go's `struct { *Base }`
        doc_types: bool                                # doc comments type what the code doesn't: jsdoc's `@param {Store} s`
        visibility: str                                # how names are exported: "underscore" (python), "export" (ts, js), "capital" (go)

    _languages: list[tuple[list[str], LanguageSpec]] = [
        ([".py"],
            LanguageSpec(
                name="python",
                language=tree_sitter.Language(tree_sitter_python.language()),
                definitions={
                    "function_definition": "function",
                    "class_definition": "class",
                    "assignment": "variable",
                    "type_alias_statement": "type",
                },
                calls={"call": "function"},
                imports={"import_statement": "name", "import_from_statement": "module_name"},
                separator=".",
                index_names=["__init__"],
                inherits={"class_definition": "argument_list"},
                imported_names={"import_from_statement": "name"},
                name_fields={"assignment": "left", "type_alias_statement": "left"},
                function_values=["lambda"],
                type_annotations=["type"],
                import_calls=["import_module", "__import__"],
                decorators=["decorator"],
                docstrings=True,
                default_exports={},
                object_values=[],
                receivers={},
                kinds_by_type={},
                packages=False,
                declarations={
                    "typed_parameter": ("", "type", ""),
                    "typed_default_parameter": ("name", "type", "value"),
                    "assignment": ("left", "type", "right"),
                },
                constructors=["call", "await"],
                self_names=["self", "cls"],
                parameter_properties=[],
                builtins=frozenset(dir(builtins)),
                value_holders={
                    "argument_list": "",
                    "keyword_argument": "value",
                    "return_statement": "",
                    "assignment": "right",
                    "pair": "value",
                    "except_clause": "value",
                    "raise_statement": "",
                    "default_parameter": "value",
                    "typed_default_parameter": "value",
                    "comparison_operator": "",
                    "boolean_operator": "",
                    "conditional_expression": "",
                    "list": "",
                    "set": "",
                },
                binders={
                    "parameters": "",
                    "lambda_parameters": "",
                    "for_statement": "left",
                    "for_in_clause": "left",
                    "as_pattern_target": "",
                },
                elements=[],
                embeddings=[],
                doc_types=False,
                visibility="underscore",
                builtin_methods=frozenset(
                    name
                    for builtin in (str, bytes, list, tuple, dict, set, frozenset, int, float, io.TextIOWrapper, io.BufferedRandom)
                    for name in dir(builtin)
                ),
            )
        ),
        ([".ts"],
            LanguageSpec(
                name="typescript",
                language=tree_sitter.Language(tree_sitter_typescript.language_typescript()),
                definitions={
                    "function_declaration": "function",
                    "generator_function_declaration": "function",
                    "class_declaration": "class",
                    "method_definition": "method",
                    "method_signature": "method",
                    "abstract_method_signature": "method",
                    "abstract_class_declaration": "class",
                    "interface_declaration": "interface",
                    "type_alias_declaration": "type",
                    "enum_declaration": "enum",
                    "variable_declarator": "variable",
                    "public_field_definition": "variable",
                    "pair": "variable",
                },
                calls={"call_expression": "function", "new_expression": "constructor"},
                imports={"import_statement": "source", "export_statement": "source"},
                separator="/",
                index_names=["index"],
                inherits={
                    "class_declaration": "class_heritage",
                    "abstract_class_declaration": "class_heritage",
                    "class": "class_heritage",
                    "interface_declaration": "extends_type_clause",
                },
                imported_names={},
                name_fields={"pair": "key"},
                function_values=["arrow_function", "function_expression", "generator_function"],
                type_annotations=["type_annotation"],
                import_calls=["require", "import"],
                decorators=["decorator"],
                docstrings=False,
                default_exports={
                    "class": "class",
                    "function_expression": "function",
                    "arrow_function": "function",
                    "generator_function": "function",
                    "object": "variable",
                },
                object_values=["object"],
                receivers={},
                kinds_by_type={},
                packages=False,
                declarations={
                    "required_parameter": ("pattern", "type", ""),
                    "optional_parameter": ("pattern", "type", ""),
                    "variable_declarator": ("name", "type", "value"),
                    "public_field_definition": ("name", "type", "value"),
                    "assignment_expression": ("left", "", "right"),
                },
                constructors=["new_expression", "call_expression", "await_expression"],
                self_names=["this"],
                parameter_properties=["accessibility_modifier", "readonly"],
                builtins=frozenset({
                    "parseInt", "parseFloat", "isNaN", "isFinite", "setTimeout", "clearTimeout", "setInterval",
                    "clearInterval", "fetch", "require", "structuredClone", "queueMicrotask", "encodeURIComponent",
                    "decodeURIComponent", "encodeURI", "decodeURI", "String", "Number", "Boolean", "Array", "Object",
                    "Symbol", "BigInt", "Date", "Error", "Map", "Set", "Promise", "RegExp", "alert",
                }),
                builtin_methods=frozenset({
                    # Array
                    "at", "concat", "every", "fill", "filter", "find", "findIndex", "findLast", "findLastIndex", "flat",
                    "flatMap", "forEach", "includes", "indexOf", "join", "keys", "lastIndexOf", "map", "pop", "push",
                    "reduce", "reduceRight", "reverse", "shift", "slice", "some", "sort", "splice", "toReversed",
                    "toSorted", "toSpliced", "unshift", "values", "entries", "with",
                    # Map and Set
                    "clear", "delete", "get", "has", "set", "add",
                    # String
                    "charAt", "charCodeAt", "codePointAt", "endsWith", "localeCompare", "match", "matchAll", "normalize",
                    "padEnd", "padStart", "repeat", "replace", "replaceAll", "search", "split", "startsWith", "substring",
                    "toLowerCase", "toUpperCase", "trim", "trimEnd", "trimStart",
                    # Promise, Object, Number, Date, JSON, console
                    "then", "catch", "finally", "hasOwnProperty", "toString", "toLocaleString", "valueOf", "toFixed",
                    "toPrecision", "getTime", "toISOString", "getFullYear", "getMonth", "getDate", "getDay", "getHours",
                    "getMinutes", "getSeconds", "getMilliseconds", "toLocaleDateString", "toLocaleTimeString", "parse",
                    "stringify", "log", "warn", "error", "info", "debug",
                    # Math
                    "abs", "ceil", "floor", "round", "max", "min", "pow", "sqrt", "random", "sign", "trunc", "hypot",
                }),
                value_holders={
                    "arguments": "",
                    "return_statement": "",
                    "variable_declarator": "value",
                    "assignment_expression": "right",
                    "pair": "value",
                    "object": "",
                    "array": "",
                    "jsx_expression": "",
                    "binary_expression": "",
                    "ternary_expression": "",
                    "arrow_function": "body",
                },
                binders={
                    "formal_parameters": "",
                    "for_in_statement": "left",
                    "catch_clause": "parameter",
                    "arrow_function": "parameter",
                },
                elements=["jsx_opening_element", "jsx_self_closing_element"],
                embeddings=[],
                doc_types=False,
                visibility="export",
            )
        ),
        ([".tsx"],
            LanguageSpec(
                name="tsx",
                language=tree_sitter.Language(tree_sitter_typescript.language_tsx()),
                definitions={
                    "function_declaration": "function",
                    "generator_function_declaration": "function",
                    "class_declaration": "class",
                    "method_definition": "method",
                    "method_signature": "method",
                    "abstract_method_signature": "method",
                    "abstract_class_declaration": "class",
                    "interface_declaration": "interface",
                    "type_alias_declaration": "type",
                    "enum_declaration": "enum",
                    "variable_declarator": "variable",
                    "public_field_definition": "variable",
                    "pair": "variable",
                },
                calls={"call_expression": "function", "new_expression": "constructor"},
                imports={"import_statement": "source", "export_statement": "source"},
                separator="/",
                index_names=["index"],
                inherits={
                    "class_declaration": "class_heritage",
                    "abstract_class_declaration": "class_heritage",
                    "class": "class_heritage",
                    "interface_declaration": "extends_type_clause",
                },
                imported_names={},
                name_fields={"pair": "key"},
                function_values=["arrow_function", "function_expression", "generator_function"],
                type_annotations=["type_annotation"],
                import_calls=["require", "import"],
                decorators=["decorator"],
                docstrings=False,
                default_exports={
                    "class": "class",
                    "function_expression": "function",
                    "arrow_function": "function",
                    "generator_function": "function",
                    "object": "variable",
                },
                object_values=["object"],
                receivers={},
                kinds_by_type={},
                packages=False,
                declarations={
                    "required_parameter": ("pattern", "type", ""),
                    "optional_parameter": ("pattern", "type", ""),
                    "variable_declarator": ("name", "type", "value"),
                    "public_field_definition": ("name", "type", "value"),
                    "assignment_expression": ("left", "", "right"),
                },
                constructors=["new_expression", "call_expression", "await_expression"],
                self_names=["this"],
                parameter_properties=["accessibility_modifier", "readonly"],
                builtins=frozenset({
                    "parseInt", "parseFloat", "isNaN", "isFinite", "setTimeout", "clearTimeout", "setInterval",
                    "clearInterval", "fetch", "require", "structuredClone", "queueMicrotask", "encodeURIComponent",
                    "decodeURIComponent", "encodeURI", "decodeURI", "String", "Number", "Boolean", "Array", "Object",
                    "Symbol", "BigInt", "Date", "Error", "Map", "Set", "Promise", "RegExp", "alert",
                }),
                builtin_methods=frozenset({
                    # Array
                    "at", "concat", "every", "fill", "filter", "find", "findIndex", "findLast", "findLastIndex", "flat",
                    "flatMap", "forEach", "includes", "indexOf", "join", "keys", "lastIndexOf", "map", "pop", "push",
                    "reduce", "reduceRight", "reverse", "shift", "slice", "some", "sort", "splice", "toReversed",
                    "toSorted", "toSpliced", "unshift", "values", "entries", "with",
                    # Map and Set
                    "clear", "delete", "get", "has", "set", "add",
                    # String
                    "charAt", "charCodeAt", "codePointAt", "endsWith", "localeCompare", "match", "matchAll", "normalize",
                    "padEnd", "padStart", "repeat", "replace", "replaceAll", "search", "split", "startsWith", "substring",
                    "toLowerCase", "toUpperCase", "trim", "trimEnd", "trimStart",
                    # Promise, Object, Number, Date, JSON, console
                    "then", "catch", "finally", "hasOwnProperty", "toString", "toLocaleString", "valueOf", "toFixed",
                    "toPrecision", "getTime", "toISOString", "getFullYear", "getMonth", "getDate", "getDay", "getHours",
                    "getMinutes", "getSeconds", "getMilliseconds", "toLocaleDateString", "toLocaleTimeString", "parse",
                    "stringify", "log", "warn", "error", "info", "debug",
                    # Math
                    "abs", "ceil", "floor", "round", "max", "min", "pow", "sqrt", "random", "sign", "trunc", "hypot",
                }),
                value_holders={
                    "arguments": "",
                    "return_statement": "",
                    "variable_declarator": "value",
                    "assignment_expression": "right",
                    "pair": "value",
                    "object": "",
                    "array": "",
                    "jsx_expression": "",
                    "binary_expression": "",
                    "ternary_expression": "",
                    "arrow_function": "body",
                },
                binders={
                    "formal_parameters": "",
                    "for_in_statement": "left",
                    "catch_clause": "parameter",
                    "arrow_function": "parameter",
                },
                elements=["jsx_opening_element", "jsx_self_closing_element"],
                embeddings=[],
                doc_types=False,
                visibility="export",
            )
        ),
        ([".js", ".mjs", ".cjs", ".jsx"],
            LanguageSpec(
                name="javascript",
                language=tree_sitter.Language(tree_sitter_javascript.language()),
                definitions={
                    "function_declaration": "function",
                    "generator_function_declaration": "function",
                    "class_declaration": "class",
                    "method_definition": "method",
                    "variable_declarator": "variable",
                    "field_definition": "variable",
                    "pair": "variable",
                },
                calls={"call_expression": "function", "new_expression": "constructor"},
                imports={"import_statement": "source", "export_statement": "source"},
                separator="/",
                index_names=["index"],
                inherits={"class_declaration": "class_heritage", "class": "class_heritage"},
                imported_names={},
                name_fields={"field_definition": "property", "pair": "key"},
                function_values=["arrow_function", "function_expression", "generator_function"],
                type_annotations=[],
                import_calls=["require", "import"],
                decorators=["decorator"],
                docstrings=False,
                default_exports={
                    "class": "class",
                    "function_expression": "function",
                    "arrow_function": "function",
                    "generator_function": "function",
                    "object": "variable",
                },
                object_values=["object"],
                receivers={},
                kinds_by_type={},
                packages=False,
                declarations={
                    "variable_declarator": ("name", "", "value"),
                    "field_definition": ("property", "", "value"),
                    "assignment_expression": ("left", "", "right"),
                },
                constructors=["new_expression", "call_expression", "await_expression"],
                self_names=["this"],
                parameter_properties=[],
                builtins=frozenset({
                    "parseInt", "parseFloat", "isNaN", "isFinite", "setTimeout", "clearTimeout", "setInterval",
                    "clearInterval", "fetch", "require", "structuredClone", "queueMicrotask", "encodeURIComponent",
                    "decodeURIComponent", "encodeURI", "decodeURI", "String", "Number", "Boolean", "Array", "Object",
                    "Symbol", "BigInt", "Date", "Error", "Map", "Set", "Promise", "RegExp", "alert",
                }),
                builtin_methods=frozenset({
                    # Array
                    "at", "concat", "every", "fill", "filter", "find", "findIndex", "findLast", "findLastIndex", "flat",
                    "flatMap", "forEach", "includes", "indexOf", "join", "keys", "lastIndexOf", "map", "pop", "push",
                    "reduce", "reduceRight", "reverse", "shift", "slice", "some", "sort", "splice", "toReversed",
                    "toSorted", "toSpliced", "unshift", "values", "entries", "with",
                    # Map and Set
                    "clear", "delete", "get", "has", "set", "add",
                    # String
                    "charAt", "charCodeAt", "codePointAt", "endsWith", "localeCompare", "match", "matchAll", "normalize",
                    "padEnd", "padStart", "repeat", "replace", "replaceAll", "search", "split", "startsWith", "substring",
                    "toLowerCase", "toUpperCase", "trim", "trimEnd", "trimStart",
                    # Promise, Object, Number, Date, JSON, console
                    "then", "catch", "finally", "hasOwnProperty", "toString", "toLocaleString", "valueOf", "toFixed",
                    "toPrecision", "getTime", "toISOString", "getFullYear", "getMonth", "getDate", "getDay", "getHours",
                    "getMinutes", "getSeconds", "getMilliseconds", "toLocaleDateString", "toLocaleTimeString", "parse",
                    "stringify", "log", "warn", "error", "info", "debug",
                    # Math
                    "abs", "ceil", "floor", "round", "max", "min", "pow", "sqrt", "random", "sign", "trunc", "hypot",
                }),
                value_holders={
                    "arguments": "",
                    "return_statement": "",
                    "variable_declarator": "value",
                    "assignment_expression": "right",
                    "pair": "value",
                    "object": "",
                    "array": "",
                    "jsx_expression": "",
                    "binary_expression": "",
                    "ternary_expression": "",
                    "arrow_function": "body",
                },
                binders={
                    "formal_parameters": "",
                    "for_in_statement": "left",
                    "catch_clause": "parameter",
                    "arrow_function": "parameter",
                },
                elements=["jsx_opening_element", "jsx_self_closing_element"],
                embeddings=[],
                doc_types=True,
                visibility="export",
            )
        ),
        ([".go"],
            LanguageSpec(
                name="go",
                language=tree_sitter.Language(tree_sitter_go.language()),
                definitions={
                    "function_declaration": "function",
                    "method_declaration": "method",
                    "method_elem": "method",
                    "type_spec": "type",
                    "type_alias": "type",
                    "const_spec": "variable",
                    "var_spec": "variable",
                    "field_declaration": "variable",
                },
                calls={"call_expression": "function"},
                imports={"import_spec": "path"},
                separator="/",
                index_names=[],
                inherits={},
                imported_names={},
                name_fields={},
                function_values=["func_literal"],
                type_annotations=["type_identifier", "qualified_type"],
                import_calls=[],
                decorators=[],
                docstrings=False,
                default_exports={},
                object_values=[],
                receivers={"method_declaration": "receiver"},
                kinds_by_type={"struct_type": "class", "interface_type": "interface"},
                packages=True,
                declarations={
                    "parameter_declaration": ("name", "type", ""),
                    "var_spec": ("name", "type", "value"),
                    "short_var_declaration": ("left", "", "right"),
                    "field_declaration": ("name", "type", ""),
                },
                constructors=["composite_literal", "unary_expression", "call_expression"],
                self_names=[],
                parameter_properties=[],
                builtins=frozenset({
                    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag", "len", "make", "max", "min",
                    "new", "panic", "print", "println", "real", "recover",
                }),
                builtin_methods=frozenset({"Error", "String"}),
                value_holders={
                    "argument_list": "",
                    "return_statement": "",
                    "assignment_statement": "right",
                    "short_var_declaration": "right",
                    "var_spec": "value",
                    "binary_expression": "",
                },
                binders={"range_clause": "left"},
                elements=[],
                embeddings=["field_declaration", "type_elem"],
                doc_types=False,
                visibility="capital",
            )
        ),
    ]

    # Symbol kinds the graph can hold; "file" is the outermost scope added by _extract.
    _kinds_of_symbols: list[str] = sorted(
        {"file"} | {kind for _, spec in _languages for kind in spec.definitions.values()}
    )

    # Version of what parsing extracts, part of each file's hash, so a build after a bump parses every file again,
    # resuming where it left off when partial. Bump it when parsing records something differently: _extract, _describe,
    # the language specs, or a grammar; not for changes to resolving, as edges are resolved again on every build.
    _extractor_version: str = "7"

    _ignored_dirs: set[str] = {
        ".git",
        ".cache",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "dist",
        "build",
        ".tox",
    }

    # Test code, by its file's path: test directories, and names like `test_x.py`, `x_test.go`, `x.spec.ts`,
    # `app.e2e-spec.ts`; a symbol is test code when its file is.
    _test_paths: re.Pattern = re.compile(
        r"(^|/)(tests?|__tests__|e2e|spec)/"
        r"|(^|/)(test_[^/]*|conftest)\.py$"
        r"|_test\.(py|go)$"
        r"|[.-](test|spec)\.[cm]?[jt]sx?$"
    )

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextlib.contextmanager
    def connect(self, path_to_storage: str, read_only: bool = False):
        # Tools run on worker threads, so each call gets its own connection.
        if read_only:
            # Fails rather than creating an empty database when the file is missing;
            # WAL mode persists in the file, so readers don't block a running build.
            uri = pathlib.Path(path_to_storage).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            connection = sqlite3.connect(path_to_storage, timeout=30)
        try:
            if not read_only:
                connection.executescript(self._schema)
            yield connection
            if not read_only:
                connection.commit()
        finally:
            connection.close()

    def build_database(
        self,
        path_to_analyze: str,
        path_to_storage: str,
        timeout_after_s: int,
    ) -> dict[str, str | float | int | list[str]]:
        root = pathlib.Path(path_to_analyze).resolve()
        if not root.is_dir():
            return {"status": "failed", "error": f"not a directory: {root}"}

        # sqlite creates a missing database file, but not its directory.
        storage = pathlib.Path(path_to_storage)
        if storage.is_dir():
            return {"status": "failed", "error": f"a directory, not a database file: {storage}"}
        if not storage.parent.is_dir():
            return {"status": "failed", "error": f"directory does not exist: {storage.parent}"}

        with self._guard:
            lock = self._locks.setdefault(path_to_storage, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"status": "busy", "error": f"already building {path_to_storage}"}

        started = time.monotonic()
        deadline = started + max(1, timeout_after_s)
        try:
            with self.connect(path_to_storage) as db:
                self._set_meta(db, state="building", root=str(root), started_at=time.time())
                parsed, unchanged, failed, complete = self._extract_all(db, root, deadline, self._languages, self._ignored_dirs)
                # Parsing is kept, even if resolving runs out of time or the process dies. Edges are resolved once
                # every file is parsed, since a partial build is called again; until then, the last build's remain.
                db.commit()
                note = None
                if complete:
                    try:
                        self._resolve(db, self._languages, root, deadline)
                    except TimeoutError:
                        db.rollback()
                        complete = False
                        log.warning("~ resolving timed out, graph is partial")
                        if not parsed:
                            # Nothing was left to parse, so calling again with the same time would only time out again.
                            note = "resolving edges takes longer than timeout_after_s; call again with a larger one"
                state = "complete" if complete else "partial"
                counts = {
                    "files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
                    "symbols": db.execute("SELECT COUNT(*) FROM symbols WHERE kind != 'file'").fetchone()[0],
                    "edges": db.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                }
                self._set_meta(db, state=state, finished_at=time.time(), **counts)
            elapsed_s = round(time.monotonic() - started, 3)
            log.info(f". built graph {state} for {root} in {elapsed_s}s: {counts}")
            return {
                "status": state,
                "parsed": parsed,
                "unchanged": unchanged,
                "failed": failed,
                "elapsed_s": elapsed_s,
                **counts,
                "kinds_of_symbols": self._kinds_of_symbols,
                **({"note": note} if note else {}),
            }
        finally:
            lock.release()

    def load_status_of(
        self,
        path_to_storage: str,
    ) -> dict[str, str | float | int | list[str]]:
        if not pathlib.Path(path_to_storage).is_file():
            return {"status": "missing"}

        with self.connect(path_to_storage, read_only=True) as db:
            meta = {key: json.loads(value) for key, value in db.execute("SELECT key, value FROM meta")}

        # A build that died leaves "building" behind with no lock held.
        with self._guard:
            lock = self._locks.get(path_to_storage)
        state = meta.pop("state", None)
        if lock is not None and lock.locked():
            state = "building"
        elif state in (None, "building"):
            state = "interrupted"

        return {"status": state, **meta, "kinds_of_symbols": self._kinds_of_symbols}

    def find_symbols(
        self,
        path_to_storage: str,
        kind: str,
        query: str,
        limit: int,
    ) -> dict[str, str | list[dict]]:
        # "any" is reserved for searching a name of every kind; it lists nothing on its own.
        query = query.strip()
        if kind != "any" and kind not in self._kinds_of_symbols:
            return {"status": "failed", "error": f"unknown kind {kind!r}, expected \"any\" or one of {self._kinds_of_symbols}"}
        if kind == "any" and not query:
            return {
                "status": "failed",
                "error": 'kind "any" needs a query; use "file" to find where to start, or a kind for its most referenced symbols',
            }
        status = str(self.load_status_of(path_to_storage)["status"])
        if status == "missing":
            return {"status": status, "results": []}
        limit = max(1, limit)

        with self.connect(path_to_storage, read_only=True) as db:
            if not query and kind == "file":
                # Top-level files: nothing imports them, but they define symbols or import files. Ordered by
                # how many files their imports reach, so each project's entry points come first.
                imports: dict[str, set[str]] = {}
                for file_path, dst_id in db.execute("SELECT file_path, dst_id FROM edges WHERE kind = 'IMPORTS'"):
                    imports.setdefault(file_path, set()).add(dst_id)
                imported = set().union(*imports.values())
                results = []
                for path, doc, count in db.execute(
                    "SELECT f.path, s.doc, COUNT(c.id) FROM files f JOIN symbols s ON s.id = f.path "
                    "LEFT JOIN symbols c ON c.file_path = f.path AND c.kind != 'file' GROUP BY f.path"
                ):
                    if path in imported or not (count or path in imports):
                        continue
                    reached, pending = {path}, [path]
                    while pending:
                        for dst_id in imports.get(pending.pop(), set()) - reached:
                            reached.add(dst_id)
                            pending.append(dst_id)
                    results.append({
                        "id": path, "kind": "file", "doc": doc, "symbols": count, "reach": len(reached) - 1,
                        "test": self._test_paths.search(path) is not None,
                    })
                # Tests import what they test, but aren't where to start.
                results.sort(key=lambda result: (result["test"], -result["reach"], -result["symbols"], result["id"]))
                return {"status": status, "results": results[:limit]}

            # Exact names first, matching case before not, then prefixes, then substrings, each most referenced
            # first, then exported; an empty query lists the kind's most referenced symbols.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute(
                """
                SELECT id, kind, qualified_name, file_path, start_line, end_line, signature, doc,
                    (SELECT COUNT(*) FROM edges WHERE dst_id = s.id AND kind != 'CONTAINS') AS refs, exported,
                    CASE
                        WHEN name = :query OR qualified_name = :query THEN 0
                        WHEN name = :query COLLATE NOCASE OR qualified_name = :query COLLATE NOCASE THEN 1
                        WHEN name LIKE :prefix ESCAPE '\\' OR qualified_name LIKE :prefix ESCAPE '\\' THEN 2
                        ELSE 3
                    END AS rank
                FROM symbols s
                WHERE (name LIKE :within ESCAPE '\\' OR qualified_name LIKE :within ESCAPE '\\') AND (:kind = 'any' OR kind = :kind)
                ORDER BY rank, refs DESC, exported DESC, length(qualified_name), id
                LIMIT :limit
                """,
                {"query": query, "prefix": f"{escaped}%", "within": f"%{escaped}%", "kind": kind, "limit": limit},
            ).fetchall()
        keys = ("id", "kind", "qualified_name", "file_path", "start_line", "end_line", "signature", "doc", "references")
        return {
            "status": status,
            "results": [
                {**dict(zip(keys, row)), "exported": bool(row[9]), "test": self._test_paths.search(row[3]) is not None}
                for row in rows
            ],
        }

    def describe_file(
        self,
        path_to_storage: str,
        file_path: str,
        max_symbols: int,
    ) -> dict:
        status = str(self.load_status_of(path_to_storage)["status"])
        if status == "missing":
            return {"status": status}
        file_path = file_path.strip().removeprefix("./")

        with self.connect(path_to_storage, read_only=True) as db:
            found = db.execute(
                "SELECT f.language, s.doc, s.end_line FROM files f JOIN symbols s ON s.id = f.path WHERE f.path = ?",
                (file_path,),
            ).fetchone()
            if found is None:
                # Paths ending in the same file name, e.g. "store.py" -> "src/app/store.py".
                matches = db.execute(
                    "SELECT path FROM files WHERE path = :name OR path LIKE '%/' || :name ORDER BY length(path), path LIMIT 10",
                    {"name": pathlib.PurePosixPath(file_path).name},
                ).fetchall()
                return {"status": "failed", "error": f"not in the graph: {file_path}", "matches": [path for (path,) in matches]}
            language, doc, lines = found

            symbols = db.execute(
                "SELECT id, kind, name, parent_id, start_line, end_line, signature, doc, "
                "(SELECT COUNT(*) FROM edges WHERE dst_id = s.id AND kind != 'CONTAINS'), exported "
                "FROM symbols s WHERE file_path = ? AND kind != 'file' ORDER BY start_line, id",
                (file_path,),
            ).fetchall()
            imports = db.execute(
                "SELECT DISTINCT dst_id FROM edges WHERE kind = 'IMPORTS' AND file_path = ? ORDER BY dst_id", (file_path,)
            ).fetchall()
            imported_by = db.execute(
                "SELECT DISTINCT file_path FROM edges WHERE kind = 'IMPORTS' AND dst_id = ? ORDER BY file_path", (file_path,)
            ).fetchall()
            # Imports with no edge on their line name modules outside the graph: packages, or files it doesn't parse.
            external = [target for (target,) in db.execute(
                "SELECT DISTINCT target FROM refs r WHERE kind = 'IMPORTS' AND file_path = ? AND NOT EXISTS ("
                "SELECT 1 FROM edges e WHERE e.kind = 'IMPORTS' AND e.src_id = r.src_id AND e.line = r.line) ORDER BY target",
                (file_path,),
            )]
        if language == "python":
            # `from typing import List` also refers to "typing.List" in case it's a submodule.
            external = [target for target in external if not any(target.startswith(f"{other}.") for other in external)]

        nodes: dict[str, dict] = {}
        children: dict[str, list[str]] = {}
        for symbol_id, kind, name, parent_id, start_line, end_line, signature, symbol_doc, references, exported in symbols:
            nodes[symbol_id] = {
                "id": symbol_id, "kind": kind, "name": name, "start_line": start_line, "end_line": end_line,
                "signature": signature, "doc": symbol_doc, "references": references, "exported": bool(exported),
                "parent_id": parent_id,
            }
            children.setdefault(parent_id, []).append(symbol_id)
        order = list(children.get(file_path, []))
        depth = dict.fromkeys(order, 0)
        for symbol_id in order:
            for member_id in children.get(symbol_id, []):
                depth[member_id] = depth[symbol_id] + 1
                order.append(member_id)
        weight = {symbol_id: nodes[symbol_id]["references"] for symbol_id in order}
        for symbol_id in reversed(order):
            if nodes[symbol_id]["parent_id"] in weight:
                weight[nodes[symbol_id]["parent_id"]] += weight[symbol_id]

        # The most relevant max_symbols: most referenced, counting references to members, then outer before
        # inner, definitions before variables, and earlier before later. A parent outranks its members, so
        # the outline stays a tree; it keeps the file's order.
        included = sorted(
            order,
            key=lambda i: (-weight[i], depth[i], nodes[i]["kind"] == "variable", nodes[i]["start_line"], i),
        )[:max(1, max_symbols)]
        outline = []
        for symbol_id in sorted(included, key=lambda i: (nodes[i]["start_line"], i)):
            node = nodes[symbol_id]
            parent_id = node.pop("parent_id")
            (outline if parent_id == file_path else nodes[parent_id].setdefault("members", [])).append(node)

        return {
            "status": status,
            "id": file_path,
            "language": language,
            "doc": doc,
            "lines": lines,
            "test": self._test_paths.search(file_path) is not None,
            "imports": [path for (path,) in imports],
            "imported_by": [path for (path,) in imported_by],
            "external_imports": external,
            "symbols": len(symbols),
            "truncated": len(included) < len(symbols),
            "outline": outline,
        }

    def locate_symbols(
        self,
        path_to_storage: str,
        file_path: str,
        start_line: int,
        end_line: int | None,
        limit: int,
    ) -> dict:
        meta = self.load_status_of(path_to_storage)
        status = str(meta["status"])
        if status == "missing":
            return {"status": status}
        file_path = file_path.strip().removeprefix("./")
        first = max(1, start_line)
        last = max(first, end_line or first)

        with self.connect(path_to_storage, read_only=True) as db:
            found = db.execute("SELECT sha256 FROM files WHERE path = ?", (file_path,)).fetchone()
            if found is None:
                return {"status": "failed", "error": f"not in the graph: {file_path}", "matches": self._matches(db, file_path)}
            # Each symbol spanning any of the lines, innermost first; the file spans them all, so comes last.
            rows = db.execute(
                "SELECT id, kind, signature, start_line, end_line, exported FROM symbols "
                "WHERE file_path = ? AND start_line <= ? AND end_line >= ? ORDER BY kind = 'file', end_line - start_line, start_line, id",
                (file_path, last, first),
            ).fetchall()

        # A file changed or removed since the build has lines that may be out of date.
        try:
            source = (pathlib.Path(str(meta["root"])) / file_path).read_bytes()
        except (KeyError, OSError):
            source = None
        keys = ("id", "kind", "signature", "start_line", "end_line")
        return {
            "status": status,
            "file_path": file_path,
            "start_line": first,
            "end_line": last,
            "test": self._test_paths.search(file_path) is not None,
            "stale": source is None or self._digest(source) != found[0],
            "results": [{**dict(zip(keys, row)), "exported": bool(row[5])} for row in rows[:max(1, limit)]],
            "count": len(rows),
            "truncated": len(rows) > max(1, limit),
        }

    def describe_symbol(
        self,
        path_to_storage: str,
        symbol_id: str,
        include_source: bool,
        max_source_lines: int,
        limit: int,
    ) -> dict:
        meta = self.load_status_of(path_to_storage)
        status = str(meta["status"])
        if status == "missing":
            return {"status": status}
        symbol_id = symbol_id.strip()
        limit = max(1, limit)

        with self.connect(path_to_storage, read_only=True) as db:
            found = db.execute(
                "SELECT s.kind, s.qualified_name, s.file_path, s.start_line, s.end_line, s.parent_id, s.signature, s.doc, "
                "s.exported, f.sha256 "
                "FROM symbols s JOIN files f ON f.path = s.file_path WHERE s.id = ?",
                (symbol_id,),
            ).fetchone()
            if found is None:
                return {"status": "failed", "error": f"not in the graph: {symbol_id}", "matches": self._matches(db, symbol_id)}
            kind, qualified_name, file_path, start_line, end_line, parent_id, signature, doc, exported, sha256 = found

            parent = db.execute("SELECT id, kind, signature FROM symbols WHERE id = ?", (parent_id,)).fetchone()
            members = db.execute(
                "SELECT id, kind, signature, exported FROM symbols WHERE parent_id = ? ORDER BY start_line, id", (symbol_id,)
            ).fetchall()
            counts = {"members": len(members)}
            lists: dict[str, list[dict]] = {}
            # Each relationship as (edge kind, this symbol's end, the other's end); exact edges first.
            for relationship, (edge_kind, this, other) in {
                "callers": ("CALLS", "dst_id", "src_id"),
                "callees": ("CALLS", "src_id", "dst_id"),
                "bases": ("INHERITS", "src_id", "dst_id"),
                "subclasses": ("INHERITS", "dst_id", "src_id"),
                "uses": ("USES", "src_id", "dst_id"),
                "used_by": ("USES", "dst_id", "src_id"),
                "references": ("REFERENCES", "src_id", "dst_id"),
                "referenced_by": ("REFERENCES", "dst_id", "src_id"),
                "overrides": ("OVERRIDES", "src_id", "dst_id"),
                "overridden_by": ("OVERRIDES", "dst_id", "src_id"),
            }.items():
                grouped: dict[str, dict] = {}
                for other_id, other_kind, other_signature, confidence, line in db.execute(
                    f"SELECT e.{other}, s.kind, s.signature, e.confidence, e.line FROM edges e JOIN symbols s ON s.id = e.{other} "
                    f"WHERE e.{this} = ? AND e.kind = ? "
                    f"ORDER BY CASE e.confidence WHEN 'exact' THEN 0 WHEN 'inferred' THEN 1 ELSE 2 END, e.{other}, e.line",
                    (symbol_id, edge_kind),
                ):
                    entry = grouped.setdefault(other_id, {
                        "id": other_id, "kind": other_kind, "signature": other_signature, "confidence": confidence,
                        "test": self._test_paths.search(other_id.split("::")[0]) is not None, "lines": [],
                    })
                    entry["lines"].append(line)
                counts[relationship] = len(grouped)
                lists[relationship] = list(grouped.values())[:limit]

        # A file changed or removed since the build has edges and lines that may be out of date.
        try:
            source = (pathlib.Path(str(meta["root"])) / file_path).read_bytes()
        except (KeyError, OSError):
            source = None
        result = {
            "status": status,
            "id": symbol_id,
            "kind": kind,
            "qualified_name": qualified_name,
            "file_path": file_path,
            "start_line": start_line,
            "end_line": end_line,
            "signature": signature,
            "doc": doc,
            "exported": bool(exported),
            "test": self._test_paths.search(file_path) is not None,
            "stale": source is None or self._digest(source) != sha256,
            "parent": dict(zip(("id", "kind", "signature"), parent)) if parent else None,
            "members": [
                {"id": member_id, "kind": member_kind, "signature": member_signature, "exported": bool(member_exported)}
                for member_id, member_kind, member_signature, member_exported in members[:limit]
            ],
            **lists,
            "counts": counts,
        }
        if include_source and source is not None:
            lines = source.decode("utf-8", errors="replace").splitlines()[start_line - 1:end_line]
            result["source"] = "\n".join(lines[:max(1, max_source_lines)])
            result["source_truncated"] = len(lines) > max(1, max_source_lines)
        return result

    def walk_edges(
        self,
        path_to_storage: str,
        symbol_id: str,
        kinds: tuple[str, ...],
        forward: bool,
        depth: int,
        limit: int,
        skip_guesses: bool,
        only_tests: bool,
    ) -> dict:
        status = str(self.load_status_of(path_to_storage)["status"])
        if status == "missing":
            return {"status": status}
        symbol_id = symbol_id.strip().removeprefix("./")
        # Imports go from file to file, whichever symbol in the importer holds them.
        source = "file_path" if kinds == ("IMPORTS",) else "src_id"
        this, other = (source, "dst_id") if forward else ("dst_id", source)

        with self.connect(path_to_storage, read_only=True) as db:
            if db.execute("SELECT 1 FROM symbols WHERE id = ?", (symbol_id,)).fetchone() is None:
                return {"status": "failed", "error": f"not in the graph: {symbol_id}", "matches": self._matches(db, symbol_id)}

            # Breadth first, so each symbol is reached at its shortest distance, through every symbol one
            # step closer. A path is as sure as its least sure edge, and a symbol as its surest path.
            surety = {"exact": 0, "inferred": 1, "guess": 2}
            reached: dict[str, dict] = {symbol_id: {"confidence": "exact"}}
            frontier = [symbol_id]
            for distance in range(1, max(1, depth) + 1):
                found: dict[str, dict] = {}
                for via in frontier:
                    for other_id, confidence in db.execute(
                        f"SELECT {other}, confidence FROM edges WHERE {this} = ? AND kind IN (SELECT value FROM json_each(?)) "
                        f"ORDER BY {other}",
                        (via, json.dumps(kinds)),
                    ):
                        if other_id in reached or (skip_guesses and confidence == "guess"):
                            continue
                        entry = found.setdefault(other_id, {"depth": distance, "confidence": "guess", "via": []})
                        confidence = max(confidence, reached[via]["confidence"], key=surety.__getitem__)
                        entry["confidence"] = min(confidence, entry["confidence"], key=surety.__getitem__)
                        if via not in entry["via"]:
                            entry["via"].append(via)
                if not found:
                    break
                reached |= found
                frontier = list(found)
            del reached[symbol_id]
            tests = {i for i in reached if self._test_paths.search(i.split("::")[0]) is not None}
            if only_tests:
                reached = {i: entry for i, entry in reached.items() if i in tests}

            ordered = sorted(reached, key=lambda i: (reached[i]["depth"], surety[reached[i]["confidence"]], i))[:max(1, limit)]
            described = {
                i: (k, s) for i, k, s in db.execute(
                    "SELECT id, kind, signature FROM symbols WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ordered),)
                )
            }
        results = [
            {"id": i, "kind": described[i][0], "signature": described[i][1], **reached[i], "test": i in tests}
            for i in ordered if i in described
        ]
        return {"status": status, "id": symbol_id, "results": results, "count": len(reached), "truncated": len(reached) > len(ordered)}

    def read_source(
        self,
        path_to_analyze: str,
        file_path: str,
        start_line: int | None,
        end_line: int | None,
        max_lines: int,
    ) -> dict:
        root = pathlib.Path(path_to_analyze).resolve()
        path = (root / file_path.strip()).resolve()
        if not path.is_relative_to(root):
            return {"status": "failed", "error": f"outside {root}: {file_path}"}
        if not path.is_file():
            return {"status": "failed", "error": f"not a file: {path}"}

        lines = path.read_text(errors="replace").splitlines()
        first = max(1, start_line or 1)
        if first > len(lines):
            return {"status": "failed", "error": f"start_line {first} is past the end of {len(lines)} lines"}
        if end_line is not None and end_line < first:
            return {"status": "failed", "error": f"end_line {end_line} is before start_line {first}"}
        wanted = min(len(lines), end_line or first + max(1, max_lines) - 1)
        last = min(wanted, first + max(1, max_lines) - 1)
        return {
            "status": "ok",
            "file_path": path.relative_to(root).as_posix(),
            "start_line": first,
            "end_line": last,
            "lines": len(lines),
            "source": "\n".join(lines[first - 1:last]),
            "truncated": last < wanted,
        }

    @classmethod
    def _digest(
        cls,
        source: bytes,
    ) -> str:
        """
        Hash of a file's source as parsed by this version of extraction, which tells unchanged files from changed ones.
        """

        return hashlib.sha256(cls._extractor_version.encode() + b"\0" + source).hexdigest()

    @classmethod
    def _matches(
        cls,
        db: sqlite3.Connection,
        symbol_id: str,
    ) -> list[str]:
        """
        Ids like one that isn't in the graph: symbols of the same name, or files of the same file name.
        """

        file_path, _, qualified_name = symbol_id.rpartition("::")
        name = qualified_name.rpartition(".")[2] if file_path else pathlib.PurePosixPath(qualified_name).name
        return [i for (i,) in db.execute(
            "SELECT id FROM symbols WHERE name = :name COLLATE NOCASE "
            "OR (kind = 'file' AND (id = :name OR id LIKE '%/' || :name)) ORDER BY length(id), id LIMIT 10",
            {"name": name},
        )]

    @classmethod
    def _import_target(
        cls,
        node: tree_sitter.Node,
    ) -> str | None:
        """
        Module named by an import: `import a.b as c` -> "a.b", `"./x"` -> "./x".
        """

        if node.type == "aliased_import":
            node = node.child_by_field_name("name") or node
        if node.type == "string":
            fragment = next((c for c in node.named_children if c.type == "string_fragment"), None)
            if fragment is not None and fragment.text is not None:
                return fragment.text.decode("utf-8", errors="replace")
        if node.text is None:
            return None
        return node.text.decode("utf-8", errors="replace").strip("'\"`") or None

    @classmethod
    def _bindings(
        cls,
        node: tree_sitter.Node,
        module: tree_sitter.Node,
        target: str,
    ) -> list[tuple[str, str, str | None]]:
        """
        Local names an import binds, each to a module and a name in it, or None for
        the module itself; "*" for all of its names. `from .x import y as z` -> [("z", ".x", "y")].
        """

        def text(n: tree_sitter.Node | None) -> str | None:
            return n.text.decode("utf-8", errors="replace") if n is not None and n.text else None

        bound: list[tuple[str, str | None]] = []
        if node.type == "import_statement" and module.type == "aliased_import":
            # python `import a.b as m`
            bound.append((text(module.child_by_field_name("alias")) or target, None))
        elif node.type == "import_statement" and module.type == "dotted_name":
            # python `import a.b.c` binds `a` to module `a`; later segments are followed as submodules.
            return [(target.split(".")[0], target.split(".")[0], None)]
        elif node.type == "import_from_statement":
            # python `from x import y, z as w, *`
            for imported in node.children_by_field_name("name"):
                if imported.type == "aliased_import":
                    name = text(imported.child_by_field_name("name"))
                    bound.append((text(imported.child_by_field_name("alias")) or name or "", name))
                else:
                    bound.append((text(imported) or "", text(imported)))
            if any(c.type == "wildcard_import" for c in node.named_children):
                bound.append(("*", None))
        elif node.type in ("import_statement", "export_statement"):
            # js/ts `import D, { a, b as c } from`, `import * as ns from`, and the same forms re-exported.
            clauses = [c for c in node.named_children if c.type in ("import_clause", "export_clause", "namespace_export")]
            for clause in clauses:
                parts = [clause] if clause.type == "namespace_export" else clause.named_children
                for part in parts:
                    if part.type == "identifier":
                        bound.append((text(part) or "", "default"))
                    elif part.type in ("namespace_import", "namespace_export"):
                        bound.append((text(part.named_children[0] if part.named_children else None) or "", None))
                    elif part.type in ("named_imports",):
                        for specifier in part.named_children:
                            name = text(specifier.child_by_field_name("name"))
                            bound.append((text(specifier.child_by_field_name("alias")) or name or "", name))
                    elif part.type == "export_specifier":
                        name = text(part.child_by_field_name("name"))
                        bound.append((text(part.child_by_field_name("alias")) or name or "", name))
            if node.type == "export_statement" and not clauses:
                # `export * from`
                bound.append(("*", None))
        elif node.type == "import_spec":
            # go `import st "x/store"`, `. "x"`, `_ "x"`, or the package name from the path's last segment.
            name = node.child_by_field_name("name")
            if name is None:
                bound.append((target.rsplit("/", 1)[-1], None))
            elif name.type == "dot":
                bound.append(("*", None))
            elif name.type != "blank_identifier":
                bound.append((text(name) or "", None))
        return [(local, target, name) for local, name in bound if local]

    @classmethod
    def _describe(
        cls,
        node: tree_sitter.Node,
        spec: LanguageSpec,
    ) -> tuple[str | None, str | None]:
        """
        Signature and first line of documentation of a definition, or of a file
        for the root node: `def f(a: int) -> str`, "Refresh the token.".
        """

        signature = None
        if node.parent is not None and node.text is not None:
            # Up to the body, past any decorators; functions in variables end at their own body.
            value = next((c for c in node.named_children if c.type in spec.function_values), None)
            body = node.child_by_field_name("body") or (value.child_by_field_name("body") if value is not None else None)
            start = next((c.start_byte for c in node.children if c.type not in spec.decorators), node.start_byte)
            if body is not None:
                text = node.text[start - node.start_byte : body.start_byte - node.start_byte]
            else:
                text = node.text[start - node.start_byte :].split(b"\n", 1)[0]
            signature = " ".join(text.decode("utf-8", errors="replace").split())
            signature = signature.removesuffix("{").rstrip().removesuffix(";").removesuffix("=>").removesuffix(":").rstrip()[:200] or None

        # Python's docstring: the first statement of the body, or of the file, being a string.
        body = node if node.parent is None else node.child_by_field_name("body")
        first = body.named_children[0] if spec.docstrings and body is not None and body.named_children else None
        text = None
        if first is not None and first.type == "expression_statement" and first.named_children and first.named_children[0].type == "string":
            content = next((c for c in first.named_children[0].named_children if c.type == "string_content"), None)
            text = content.text if content is not None else None
        elif node.parent is not None or any(c.type == "package_clause" for c in node.named_children):
            # Otherwise the comments right above it, past wrappers like `export` and decorators; for a go file, those
            # above its package clause, by convention.
            text = cls._comments_above(
                node if node.parent is not None else next(c for c in node.named_children if c.type == "package_clause"), spec,
            )
        elif not spec.docstrings:
            # A file's leading comments, past `#!` lines and directives like "use strict", that aren't a license or a
            # tool's directive, e.g. `// eslint-disable`, nor the doc of the code right after them, unless they say
            # they're the file's: `/** @fileoverview Serves files. */`.
            blocks: list[list[bytes]] = []
            last_row, following = -2, None
            for child in node.named_children:
                if child.type == "comment" and child.text:
                    if blocks and child.start_point.row <= last_row + 1:
                        blocks[-1].append(child.text)
                    else:
                        blocks.append([child.text])
                    last_row = child.end_point.row
                elif child.type != "hash_bang_line" and not (
                    child.type == "expression_statement" and child.named_children and child.named_children[0].type == "string"
                ):
                    following = child
                    break
            if (
                blocks
                and following is not None
                and following.start_point.row <= last_row + 1
                and not re.search(rb"@file(overview)?\b", b"\n".join(blocks[-1]))
            ):
                blocks.pop()
            text = next(
                (
                    re.sub(rb"@(fileoverview|file|packageDocumentation)\b", b"", joined)
                    for joined in map(b"\n".join, blocks)
                    if not re.search(rb"(?i)copyright|spdx-license-identifier|@license|\A[/*\s]*(eslint|jshint|prettier-ignore|@ts-|@jsx|@flow\b|<reference\s|global\s|istanbul\s)", joined)
                ),
                None,
            )

        # First line with words, without comment markers or quotes.
        lines = (line.strip().strip("/*#'\"").strip() for line in (text or b"").decode("utf-8", errors="replace").splitlines())
        doc = next((line[:200] for line in lines if line), None)
        return signature, doc

    @classmethod
    def _comments_above(
        cls,
        node: tree_sitter.Node,
        spec: LanguageSpec,
    ) -> bytes | None:
        """
        Comments right above a definition, past wrappers like `export` and decorators: its doc comment.
        """

        holder = node
        while holder.parent is not None and holder.parent.type in (
            "decorated_definition", "export_statement", "lexical_declaration", "variable_declaration", "expression_statement",
            "type_declaration", "const_declaration", "var_declaration",
        ):
            holder = holder.parent
        top = holder.start_point.row
        above = holder.prev_named_sibling
        while above is not None and above.type in spec.decorators:
            top, above = above.start_point.row, above.prev_named_sibling
        comments: list[bytes] = []
        while (
            above is not None
            and above.type == "comment"
            and above.end_point.row >= top - 1
            and above.text
            # On its own line, not trailing the code before it.
            and (above.prev_sibling is None or above.prev_sibling.end_point.row < above.start_point.row)
        ):
            comments.insert(0, above.text)
            top, above = above.start_point.row, above.prev_named_sibling
        return b"\n".join(comments) or None

    @classmethod
    def _doc_type(
        cls,
        expression: str,
    ) -> tuple[str, str | None] | None:
        """
        Dotted name of the one type a jsdoc type expression names, and the module it's imported from, if any:
        `?Store` -> ("Store", None), `Promise<Store|null>` -> ("Store", None), `import("./store").Store` ->
        ("Store", "./store"); None for several or generic ones, `Store[]`.
        """

        while True:
            expression = expression.strip().lstrip("?!").rstrip("=").strip()
            wrapped = re.fullmatch(r"\((.*)\)|Promise\.?<(.*)>", expression, flags=re.S)
            if wrapped is not None:
                expression = wrapped[1] if wrapped[1] is not None else wrapped[2]
                continue
            # A union of one type and nothing: `Store|null`.
            parts, depth, start = [], 0, 0
            for index, character in enumerate(expression):
                depth += (character in "<({[") - (character in ">)}]")
                if character == "|" and depth == 0:
                    parts.append(expression[start:index])
                    start = index + 1
            parts = [p.strip() for p in (*parts, expression[start:]) if p.strip() not in ("null", "undefined", "void")]
            if len(parts) != 1:
                return None
            if parts[0] == expression:
                break
            expression = parts[0]
        imported = re.fullmatch(r"""import\(\s*["'](.+?)["']\s*\)\.([\w$.]+)""", expression)
        if imported is not None:
            return imported[2], imported[1]
        return (expression, None) if all(part.replace("$", "_").isidentifier() for part in expression.split(".")) else None

    @classmethod
    def _type_name(
        cls,
        node: tree_sitter.Node | None,
    ) -> str | None:
        """
        Dotted name of the type a type expression or construction names, or of what a call calls:
        `Optional[m.Store]` -> "m.Store", `Promise<Store> | null` -> "Store", `new Store()` -> "Store",
        go's `&Store{}` -> "Store", `await make()` -> "make".
        """

        while node is not None:
            if node.type in (
                "identifier", "type_identifier", "attribute", "member_expression", "nested_type_identifier", "qualified_type",
                "selector_expression",
            ):
                text = node.text.decode("utf-8", errors="replace") if node.text else ""
                return text if text and all(part.replace("$", "_").isidentifier() for part in text.split(".")) else None
            if node.type in ("type", "type_annotation", "parenthesized_type", "pointer_type", "await", "await_expression"):
                node = node.named_children[0] if node.named_children else None
            elif node.type == "generic_type":
                # The generic itself, except python's `Optional[X]` and ts's `Promise<X>`, which are X.
                base = node.child_by_field_name("name") or node.child_by_field_name("type") or node.named_children[0]
                parameters = next((c for c in node.named_children if c.type in ("type_parameter", "type_arguments")), None)
                wrapped = base.text in (b"Optional", b"Promise") and parameters is not None and parameters.named_children
                node = parameters.named_children[0] if wrapped else base
            elif node.type in ("binary_operator", "union_type"):
                # `X | None` is X; a union of several types has no one type.
                members = [
                    c for c in node.named_children
                    if c.type != "none" and c.text not in (b"null", b"undefined", b"None")
                ]
                node = members[0] if len(members) == 1 else None
            elif node.type == "string":
                # Python's forward references: `s: "Store"`.
                node = next((c for c in node.named_children if c.type == "string_content"), None)
                if node is not None and node.text:
                    text = node.text.decode("utf-8", errors="replace")
                    return text if all(part.isidentifier() for part in text.split(".")) else None
            elif node.type in ("call", "call_expression", "new_expression", "composite_literal", "unary_expression"):
                node = (
                    node.child_by_field_name("function")
                    or node.child_by_field_name("constructor")
                    or node.child_by_field_name("type")
                    or node.child_by_field_name("operand")
                )
            else:
                return None
        return None

    @classmethod
    def _extract(
        cls,
        tree: tree_sitter.Tree,
        rel_path: str,
        spec: LanguageSpec,
    ) -> tuple[list[tuple], list[tuple], list[tuple]]:
        symbols: list[tuple] = []
        refs: list[tuple] = []
        bindings: list[tuple] = []  # (file_path, local, module, name, line), as the bindings table
        named: list[tuple[str, str, tree_sitter.Node | None, int, int]] = []  # (kind, src_id, expression, line, frame)
        owners: dict[int, str] = {}  # annotation node id -> variable it types, e.g. a dataclass field
        # Each symbol's enclosing scope, kind, and qualified name, and those that are object namespaces.
        parents: dict[str, str | None] = {rel_path: None}
        kinds: dict[str, str] = {rel_path: "file"}
        qualifieds: dict[str, str] = {}
        namespaces: set[str] = set()
        # Types declared or constructed for names in each scope, and for attributes of each class;
        # None when declared as different types.
        local_types: dict[tuple[str, str], str | None] = {}  # (scope, name) -> type
        attribute_types: dict[tuple[str, str], str | None] = {}  # (class, attribute) -> type
        attribute_lines: dict[tuple[str, str], int] = {}
        # Function bodies, named or not, by node id, with the one around each and its kind; the names
        # bound in each, which values in it, or in those in it, of the same name mean, not the file's; and
        # value nodes already collected, as holders nest: `f(a == b)`.
        frame_parents: dict[int, int | None] = {tree.root_node.id: None}
        local_names: set[tuple[int, str]] = set()
        # Names in a frame that aren't its locals though bound in it: python's `global log` and `nonlocal
        # count`, of the file or a function around, and functions and classes defined in it, which are symbols.
        not_locals: set[tuple[int, str]] = set()
        valued: set[int] = set()
        # Whether each symbol is visible outside its file on its own, before its owner's, and the names a file
        # exports by listing them: python's `__all__`, ts's `export { a }` and `export default a`.
        visible: dict[str, bool] = {}
        listed: set[str] | None = None
        export_listed: set[str] = set()

        def enclosing(scope_id: str | None) -> str | None:
            # The class or object namespace around a scope, which `self` and `this` refer to.
            while scope_id is not None and kinds.get(scope_id) != "class" and scope_id not in namespaces:
                scope_id = parents.get(scope_id)
            return scope_id

        def typed(scope_id: str | None, expression: str) -> str | None:
            # A dotted expression with its first name replaced by that name's type in a scope, or one around
            # it: `self.make` -> "Cache.make", `repo.find` -> "Repo.find" after `repo: Repo`. Unchanged when
            # the first name has no known type, None when it has several.
            head, dot, rest = expression.partition(".")
            if head == "super" or head in spec.self_names:
                owner = enclosing(scope_id)
                return f"{qualifieds[owner]}{dot}{rest}" if owner in qualifieds else None
            while scope_id is not None:
                if kinds.get(scope_id) != "class" and (scope_id, head) in local_types:
                    known = local_types[(scope_id, head)]
                    return f"{known}{dot}{rest}" if known is not None else None
                scope_id = parents.get(scope_id)
            return expression

        doc_imports: set[tuple[str, str]] = set()

        def doc_types(node: tree_sitter.Node, line: int) -> list[tuple[str, str, str]]:
            # What jsdoc's comment above a node types, as (tag, name, type): `@param {Store} s`, `@returns {Store}`,
            # `@type {Store}`. Types from modules, `import("./store").Store`, import them, binding their names.
            found = []
            for tag, expression, name in re.findall(
                rb"@(param|arg|argument|returns?|type)\s*\{((?:[^{}]|\{[^{}]*\})*)\}\s*\[?([\w$]*)",
                cls._comments_above(node, spec) or b"",
            ):
                typed_as = cls._doc_type(expression.decode("utf-8", errors="replace"))
                if typed_as is None:
                    continue
                type_name, module = typed_as
                if module is not None and (module, type_name) not in doc_imports:
                    doc_imports.add((module, type_name))
                    head = type_name.split(".")[0]
                    refs.append((rel_path, "IMPORTS", module, rel_path, line, None, None))
                    bindings.append((rel_path, head, module, head, line))
                tag = {b"arg": "param", b"argument": "param", b"returns": "return"}.get(tag, tag.decode())
                found.append((tag, name.decode("utf-8", errors="replace"), type_name))
            return found

        def aliases(name: str, value: tree_sitter.Node | None) -> bool:
            # A local bound to what has its name means the same, so isn't another: python's `deepcopy=deepcopy`
            # default, `_wrap = _bootstrap._wrap`.
            text = (value.text or b"").decode("utf-8", errors="replace") if value is not None else ""
            return text.rpartition(".")[2] == name

        # What an unnamed default export is named after: its file, or its directory for index files: `Button.tsx` -> Button,
        # without the dots of hidden files, which would read as qualifying it: `.eslintrc.js` -> eslintrc.
        file = pathlib.PurePosixPath(rel_path)
        unnamed = (file.parent.name if file.stem in spec.index_names and file.parent.name else file.stem).lstrip(".") or file.stem

        # The file itself is the outermost scope; module-level calls belong to it.
        root = tree.root_node
        symbols.append((rel_path, "file", rel_path, rel_path, rel_path, 1, root.end_point.row + 1, None, *cls._describe(root, spec)))

        # Walk with an explicit stack, as deep trees can exceed the recursion limit.
        stack: list[tuple[tree_sitter.Node, str, str, str, int]] = [(root, rel_path, "file", "", root.id)]
        while stack:
            node, scope, scope_kind, prefix, frame = stack.pop()
            line = node.start_point.row + 1

            # An unnamed `export default class {}` or `export default () => ...` defines one too, named after its file,
            # as does assigning commonjs's `module.exports = ...` at file level; `exports.load = function () {}` and
            # `module.exports.load = ...` define one by its name.
            exported_name = None
            around = node.parent
            if node.type in spec.default_exports and around is not None:
                if around.type == "export_statement" and around.child_by_field_name("value") == node:
                    exported_name = unnamed
                elif around.type == "assignment_expression" and around.child_by_field_name("right") == node and scope_kind == "file":
                    left = around.child_by_field_name("left")
                    assigned = (left.text or b"").decode("utf-8", errors="replace").removeprefix("module.") if left is not None else ""
                    if assigned in ("exports", "exports.default"):
                        exported_name = unnamed
                    elif assigned.startswith("exports.") and assigned.removeprefix("exports.").replace("$", "_").isidentifier():
                        exported_name = assigned.removeprefix("exports.")
            default_kind = spec.default_exports.get(node.type) if exported_name is not None else None

            # An unnamed function, e.g. a callback, is a body of its own whose names are its locals, though
            # what it calls belongs to the symbol around it: `describe(() => { const store = ... })`.
            if node.type in spec.function_values and (node.parent is None or node.parent.type not in spec.definitions) and not (
                node.parent is not None and node.parent.type == "export_statement"
            ) and default_kind is None:
                frame_parents[node.id], frame, scope_kind = frame, node.id, "function"

            # Types declarations give names: `s: Store`, `s = Store()`, `self.store = Store()`, go's
            # `s := &Store{}`. Names in a class body or on `self` are its attributes, others its scope's.
            if node.type in spec.declarations:
                name_field, type_field, value_field = spec.declarations[node.type]
                names = node.children_by_field_name(name_field) if name_field else node.named_children[:1]
                values = node.children_by_field_name(value_field) if value_field else []
                if len(names) == 1 and names[0].type == "expression_list":
                    names = names[0].named_children
                if len(values) == 1 and values[0].type == "expression_list":
                    values = values[0].named_children
                declared = cls._type_name(node.child_by_field_name(type_field)) if type_field else None
                if declared is None and spec.doc_types:
                    declared = next((t for tag, _, t in doc_types(node, line) if tag == "type"), None)
                # ts's `constructor(private store: Store)` is also an attribute.
                is_property = any(c.type in spec.parameter_properties for c in node.children)
                for index, name_node in enumerate(names):
                    value = values[index] if index < len(values) else None
                    constructed = cls._type_name(value) if value is not None and value.type in spec.constructors else None
                    type_name = declared or (typed(scope, constructed) if constructed is not None else None)
                    holder, _, name = (name_node.text or b"").decode("utf-8", errors="replace").rpartition(".")
                    if not holder and scope_kind not in ("file", "class", "variable") and not aliases(name, value):
                        local_names.add((frame, name))
                    if type_name is None or not name.replace("$", "_").isidentifier():
                        continue
                    if holder and holder in spec.self_names:
                        declaring = [(attribute_types, (enclosing(scope), name))]
                    elif holder:
                        continue
                    elif scope_kind == "class":
                        declaring = [(attribute_types, (scope, name))]
                    else:
                        declaring = [(local_types, (scope, name))]
                        if is_property:
                            declaring.append((attribute_types, (enclosing(scope), name)))
                    for table, key in declaring:
                        table[key] = type_name if table.get(key, type_name) == type_name else None
                        if table is attribute_types:
                            attribute_lines.setdefault(key, line)

            # Other names bound in functions: parameters, loop variables, `except E as e`, `catch (e)`.
            if node.type in spec.binders and scope_kind not in ("file", "class", "variable"):
                field = spec.binders[node.type]
                pending = [node.child_by_field_name(field) if field else node]
                while pending:
                    bound_node = pending.pop()
                    if bound_node is None:
                        continue
                    if bound_node.type in ("identifier", "shorthand_property_identifier_pattern") and bound_node.text:
                        name = bound_node.text.decode("utf-8", errors="replace")
                        holder = bound_node.parent
                        default = holder.child_by_field_name("value") or holder.child_by_field_name("right") if holder else None
                        if default is None or default == bound_node or not aliases(name, default):
                            local_names.add((frame, name))
                        continue
                    # Not defaults or annotations: `(a = DEFAULT)`, `(a: Store)`.
                    pending.extend(
                        c for i, c in enumerate(bound_node.children)
                        if c.is_named and bound_node.field_name_for_child(i) not in ("value", "type", "right", "default", "body")
                    )

            if node.type in ("global_statement", "nonlocal_statement"):
                not_locals.update((frame, c.text.decode("utf-8", errors="replace")) for c in node.named_children if c.text)

            # Names used as values: `map(format)`, `onClick={save}`, `except StoreError`, `== Role.ADMIN`,
            # looking through expressions that only combine them: `(A, B)`, `a if c else b`.
            # Not a class's bases, which inherit: python's `class Store(Base)`.
            if node.type in spec.value_holders and not (
                node.parent is not None and spec.inherits.get(node.parent.type) == node.type
            ):
                field = spec.value_holders[node.type]
                pending = list(node.children_by_field_name(field) if field else node.named_children)
                while pending:
                    value = pending.pop()
                    if value.id in valued:
                        continue
                    valued.add(value.id)
                    if value.type in (
                        "identifier", "attribute", "member_expression", "selector_expression", "shorthand_property_identifier",
                    ):
                        named.append(("REFERENCES", scope, value, value.start_point.row + 1, frame))
                    elif value.type in (
                        "expression_list", "tuple", "as_pattern", "parenthesized_expression", "conditional_expression",
                        "ternary_expression", "comparison_operator", "binary_expression", "boolean_operator", "list",
                        "array", "set", "spread_element", "list_splat", "jsx_expression", "await", "await_expression",
                        "not_operator", "unary_expression",
                    ):
                        pending.extend(value.named_children)

            # Rendering a component calls it: `<Layout.Main>`, `<Child />`; lowercase names are html, `<div>`.
            if node.type in spec.elements:
                tag = node.child_by_field_name("name")
                if tag is not None and tag.text and (tag.type != "identifier" or tag.text[:1].isupper()):
                    named.append(("CALLS", scope, tag, line, frame))

            # Commonjs's `module.exports = ...` is what importing the file's default gives, as is `exports.default = ...`,
            # and exports the names it gives: `module.exports = Store`, `module.exports = { load, save: store }`,
            # `exports.load = load`. Exporting a name for another, `exports.save = store` or `{ save: store }`, makes
            # it stand for that.
            if node.type == "assignment_expression" and spec.visibility == "export" and scope_kind == "file":
                left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
                assigned = (left.text or b"").decode("utf-8", errors="replace").removeprefix("module.") if left is not None else ""
                value = (right.text or b"").decode("utf-8", errors="replace") if right is not None else ""
                dotted = bool(value) and all(part.replace("$", "_").isidentifier() for part in value.split("."))
                if assigned in ("exports", "exports.default") and right is not None and right.type in spec.default_exports:
                    refs.append((rel_path, "DEFAULT", unnamed, rel_path, line, None, None))
                    for pair in right.named_children if right.type in spec.object_values else []:
                        key, member = (pair, pair) if pair.type == "shorthand_property_identifier" else (
                            pair.child_by_field_name("key"), pair.child_by_field_name("value"),
                        )
                        if key is None or member is None or member.type not in ("identifier", "shorthand_property_identifier") or not member.text:
                            continue
                        local = member.text.decode("utf-8", errors="replace")
                        export_listed.add(local)
                        if key.text != member.text and key.text:
                            refs.append((rel_path, "EXPORTS", key.text.decode("utf-8", errors="replace"), rel_path, line, None, local))
                elif assigned in ("exports", "exports.default") and right is not None and right.type == "identifier":
                    refs.append((rel_path, "DEFAULT", value, rel_path, line, None, None))
                    export_listed.add(value)
                elif assigned.startswith("exports.") and dotted:
                    export_listed.add(value)
                    if assigned != f"exports.{value}":
                        refs.append((rel_path, "EXPORTS", assigned.removeprefix("exports."), rel_path, line, None, value))

            if node.type in spec.definitions or default_kind is not None:
                kind = default_kind or spec.definitions[node.type]
                # Some kinds depend on what is defined, e.g. go's `type Store struct` is a class.
                type_child = node.child_by_field_name("type")
                if type_child is not None and type_child.type in spec.kinds_by_type:
                    kind = spec.kinds_by_type[type_child.type]
                name_node = None if default_kind else node.child_by_field_name(spec.name_fields.get(node.type, "name"))
                # Type aliases wrap the name, e.g. python's `type Pair[T] = ...`.
                while name_node is not None and name_node.type in ("type", "generic_type") and name_node.named_children:
                    name_node = name_node.named_children[0]
                namespace = False
                if kind == "variable":
                    # Plain names at file or class level, or in an object namespace; skips locals,
                    # `a, b = ...`, `self.x = ...`, `{ a } = o`. Object values name their members, e.g. `api.get`,
                    # as do objects exported themselves, e.g. `module.exports = { get() {} }`.
                    function_valued = any(c.type in spec.function_values for c in node.named_children)
                    namespace = node.type in spec.object_values or any(c.type in spec.object_values for c in node.named_children)
                    # Names bound to what a module loads are imports, not definitions: `const Store = require("./store")`,
                    # or as compiled typescript wraps it, `const store_1 = __importDefault(require("./store"))`.
                    value = node.child_by_field_name("value") or node.child_by_field_name("right")
                    loader = ""
                    while value is not None:
                        if value.type in ("member_expression", "attribute"):
                            value = value.child_by_field_name("object")
                            continue
                        callee = value.child_by_field_name(spec.calls[value.type]) if value.type in spec.calls else None
                        loader = (callee.text or b"").decode("utf-8", errors="replace").rsplit(".", 1)[-1] if callee is not None else ""
                        arguments = value.child_by_field_name("arguments") if loader.startswith("__import") and loader not in spec.import_calls else None
                        if arguments is None or not arguments.named_children:
                            break
                        value = arguments.named_children[0]
                    if loader in spec.import_calls:
                        name_node = None
                    elif (
                        scope_kind not in ("file", "class", "variable")
                        or (scope_kind == "variable" and not (function_valued or namespace))
                        or (node.type == "pair" and scope_kind != "variable")
                        or name_node is None
                        or name_node.type not in (
                            "identifier", "property_identifier", "private_property_identifier", "field_identifier",
                        )
                    ):
                        name_node = None
                    elif function_valued:
                        kind = "function"
                elif kind == "method" and scope_kind not in ("class", "interface", "variable") and node.type not in spec.receivers:
                    # Object methods outside a namespace, e.g. callbacks in `app.use({ handler() {} })`.
                    name_node = None
                if default_kind is not None:
                    name = exported_name
                elif name_node is not None and name_node.text is not None:
                    name = name_node.text.decode("utf-8", errors="replace")
                else:
                    name = None
                if name is None and kind in ("function", "method"):
                    # Unnamed here, e.g. `registerHooks({ resolve() { ... } })`, it is a body of its own.
                    frame_parents[node.id], frame, scope_kind = frame, node.id, "function"
                if name is not None:
                    if kind == "function" and scope_kind in ("class", "variable"):
                        kind = "method"
                    # Methods declared apart from their type take its name: go's `func (s *Store) Add()` -> Store.Add.
                    receiver = node.child_by_field_name(spec.receivers.get(node.type, ""))
                    receivers = [receiver] if receiver is not None else []
                    while receivers and receivers[0].type != "type_identifier":
                        receivers[:1] = receivers[0].named_children
                    owner = f"{receivers[0].text.decode('utf-8', errors='replace')}." if receivers and receivers[0].text else ""
                    qualified = f"{prefix}.{owner}{name}" if prefix else f"{owner}{name}"
                    symbol_id = f"{rel_path}::{qualified}"
                    symbols.append((
                        symbol_id, kind, name, qualified,
                        rel_path, line, node.end_point.row + 1, scope,
                        *cls._describe(node, spec),
                    ))
                    parents[symbol_id], kinds[symbol_id], qualifieds[symbol_id] = scope, kind, qualified
                    if kind != "variable":
                        not_locals.add((frame, name))
                    if namespace:
                        namespaces.add(symbol_id)
                    # Visible on its own: python's names without a leading underscore, or dunders; go's capitalized
                    # names, of capitalized types for methods; ts's top-level `export`s and members not `private`.
                    if spec.visibility == "underscore":
                        visible[symbol_id] = not name.startswith("_") or (name.startswith("__") and name.endswith("__"))
                    elif spec.visibility == "capital":
                        visible[symbol_id] = name[:1].isupper() and owner[:1].isupper() if owner else name[:1].isupper()
                    elif scope == rel_path:
                        wrapper = node.parent
                        while wrapper is not None and wrapper.type in ("lexical_declaration", "variable_declaration"):
                            wrapper = wrapper.parent
                        visible[symbol_id] = default_kind is not None or (wrapper is not None and wrapper.type == "export_statement")
                    else:
                        visible[symbol_id] = (name_node is None or name_node.type != "private_property_identifier") and not any(
                            c.type == "accessibility_modifier" and c.text in (b"private", b"protected") for c in node.children
                        )
                    if spec.visibility == "underscore" and scope_kind == "file" and name == "__all__":
                        listing = node.child_by_field_name("right")
                        if listing is not None and listing.type in ("list", "tuple"):
                            listed = {cls._import_target(s) or "" for s in listing.named_children if s.type == "string"}
                    # What a function returns: `-> Store`, `(): Promise<Store>`, go's `(*Store, error)`.
                    if kind in ("function", "method"):
                        returned = next(
                            (
                                n.child_by_field_name(field)
                                for n in (node, *(c for c in node.named_children if c.type in spec.function_values))
                                for field in ("return_type", "result")
                                if n.child_by_field_name(field) is not None
                            ),
                            None,
                        )
                        if returned is not None and returned.type == "parameter_list":
                            returned = returned.named_children[0].child_by_field_name("type") if returned.named_children else None
                        type_name = cls._type_name(returned)
                        if type_name == "Self":
                            type_name = qualifieds.get(enclosing(scope) or "")
                        if type_name is not None:
                            refs.append((symbol_id, "RETURNS", name, rel_path, line, None, type_name))
                    # Jsdoc types what javascript doesn't: its parameters and what it returns, or a variable; each a use.
                    if spec.doc_types and kind in ("function", "method", "variable"):
                        for tag, typed_name, type_name in doc_types(node, line):
                            if tag == "param" and typed_name and kind != "variable":
                                key = (symbol_id, typed_name)
                                local_types[key] = type_name if local_types.get(key, type_name) == type_name else None
                            elif tag == "return" and kind != "variable" and type_name is not None:
                                refs.append((symbol_id, "RETURNS", name, rel_path, line, None, type_name))
                            qualifier, _, target = type_name.rpartition(".")
                            refs.append((symbol_id, "USES", target, rel_path, line, qualifier or None, None))
                    # Calls in a variable's value belong to the enclosing scope, unless it is a namespace;
                    # its annotation belongs to itself.
                    if kind == "variable":
                        owners.update((c.id, symbol_id) for c in node.named_children if c.type in spec.type_annotations)
                    if kind != "variable" or namespace:
                        scope, scope_kind, prefix = symbol_id, kind, qualified
                        frame_parents[node.id], frame = frame, node.id

                    # Bases sit in one child, e.g. `(A, B)` or `extends A implements B`.
                    heritage = next((c for c in node.named_children if c.type == spec.inherits.get(node.type)), None)
                    bases = list(heritage.named_children) if heritage is not None else []
                    while bases:
                        base = bases.pop(0)
                        if base.type.endswith("_clause"):
                            bases[:0] = base.named_children
                        elif base.type != "keyword_argument":  # e.g. python's `metaclass=M`
                            named.append(("INHERITS", symbol_id, base, base.start_point.row + 1, frame))

                    # An object typed as an interface implements it: `const repo: Saver = { save() {} }`.
                    annotation = node.child_by_field_name("type") if namespace and node.type not in spec.object_values else None
                    declared = annotation.named_children[0] if annotation is not None and annotation.named_children else None
                    if declared is not None and declared.type == "generic_type":
                        declared = declared.child_by_field_name("name")
                    if declared is not None and declared.type in ("type_identifier", "nested_type_identifier"):
                        named.append(("INHERITS", symbol_id, declared, line, frame))

                    # Go's types embed others, whose fields and methods they then have, as bases: `struct { *Base }`,
                    # `interface { Reader; io.Writer }`, but not type sets, `interface { ~int | ~string }`. An embedded
                    # struct is also the field named after it: `s.Base.Save()`.
                    embedding = [] if not spec.embeddings or type_child is None else [
                        m for c in type_child.named_children for m in (c.named_children if c.type == "field_declaration_list" else [c])
                    ]
                    for member in embedding:
                        if member.type not in spec.embeddings or member.child_by_field_name("name") is not None:
                            continue
                        embedded = member.child_by_field_name("type") or (member.named_children[0] if len(member.named_children) == 1 else None)
                        if embedded is not None and embedded.type == "generic_type":
                            embedded = embedded.child_by_field_name("type")
                        if embedded is None or embedded.type not in ("type_identifier", "qualified_type") or not embedded.text:
                            continue
                        named.append(("INHERITS", symbol_id, embedded, embedded.start_point.row + 1, frame))
                        if member.type == "field_declaration":
                            type_name = embedded.text.decode("utf-8", errors="replace")
                            attribute_types.setdefault((symbol_id, type_name.rpartition(".")[2]), type_name)
                            attribute_lines.setdefault((symbol_id, type_name.rpartition(".")[2]), embedded.start_point.row + 1)

                    # Decorators call what they decorate: its own decorator children (ts classes, fields),
                    # plus those right before it (python, ts methods and `export`s). `@app.route("/")` -> route.
                    decorators = [c for c in node.named_children if c.type in spec.decorators]
                    sibling = node.prev_named_sibling
                    while sibling is not None and sibling.type in spec.decorators:
                        decorators.append(sibling)
                        sibling = sibling.prev_named_sibling
                    # What their arguments call and use as values is the decorated symbol's, in the scope around it:
                    # `@pytest.mark.parametrize("case", CASES)`, nest's `@Module({ controllers: [UsersController] })`.
                    for decorator in decorators:
                        expression = decorator.named_children[0] if decorator.named_children else None
                        if expression is not None and expression.type in spec.calls:
                            arguments = expression.child_by_field_name("arguments")
                            if arguments is not None:
                                stack.append((arguments, symbol_id, "function", qualified, frame_parents.get(node.id, frame)))
                            expression = expression.child_by_field_name(spec.calls[expression.type])
                        named.append(("CALLS", symbol_id, expression, decorator.start_point.row + 1, frame))

            elif node.type in spec.decorators:
                # Recorded with the definition they decorate.
                continue

            elif node.type in spec.calls:
                callee = node.child_by_field_name(spec.calls[node.type])
                # Compiled typescript calls functions of modules as `(0, store_1.make)()`, so not as methods.
                while callee is not None and callee.type in ("parenthesized_expression", "sequence_expression") and callee.named_children:
                    callee = callee.named_children[-1]
                named.append(("CALLS", scope, callee, line, frame))
                # Compiled typescript exports names for others with a getter or a value:
                # `Object.defineProperty(exports, "make", { get: function () { return store_1.make; } })`.
                arguments = node.child_by_field_name("arguments")
                given = arguments.named_children if arguments is not None else []
                if (
                    callee is not None
                    and callee.text == b"Object.defineProperty"
                    and scope_kind == "file"
                    and len(given) == 3
                    and (given[0].text or b"").removeprefix(b"module.") == b"exports"
                    and given[1].type == "string"
                    and given[2].type in spec.object_values
                ):
                    exported = cls._import_target(given[1])
                    for pair in (c for c in given[2].named_children if c.type in ("pair", "method_definition")):
                        key = (pair.child_by_field_name("key") or pair.child_by_field_name("name"))
                        value = pair.child_by_field_name("value") if key is not None and key.text == b"value" else None
                        getter = (pair.child_by_field_name("value") if pair.type == "pair" else pair) if key is not None and key.text == b"get" else None
                        body = getter.child_by_field_name("body") if getter is not None else None
                        if body is not None and body.type == "statement_block":
                            body = next((c.named_children[0] for c in body.named_children if c.type == "return_statement" and c.named_children), None)
                        local = (value or body).text.decode("utf-8", errors="replace") if (value or body) is not None and (value or body).text else ""
                        if exported and exported != "__esModule" and local and all(p.replace("$", "_").isidentifier() for p in local.split(".")):
                            refs.append((rel_path, "EXPORTS", exported, rel_path, line, None, local))
                            export_listed.add(local)
                # `require("./x")`, `import("./x")`, and `importlib.import_module("x")` load a module.
                arguments = node.child_by_field_name("arguments")
                first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
                if (
                    callee is not None
                    and callee.text is not None
                    and callee.text.decode("utf-8", errors="replace").rsplit(".", 1)[-1] in spec.import_calls
                    and first is not None
                    and first.type == "string"
                    and not any(c.type == "interpolation" for c in first.named_children)
                ):
                    target = cls._import_target(first)
                    if target is not None:
                        refs.append((rel_path, "IMPORTS", target, rel_path, line, None, None))
                        # `const x = require("./x")` and `x = importlib.import_module("x")` bind the module to x;
                        # `const { a, b: c } = require("./x")` and `const a = require("./x").a` bind names in it, and
                        # `module.exports = require("./x")` exports all of them.
                        # Compiled typescript wraps them: `__importDefault(require("./x"))`, `__exportStar(require("./x"), exports)`.
                        loaded, wrapper = node, node.parent.parent if node.parent is not None and node.parent.type == "arguments" else None
                        wrapping = wrapper.child_by_field_name(spec.calls[wrapper.type]) if wrapper is not None and wrapper.type in spec.calls else None
                        wrapped_by = (wrapping.text or b"").decode("utf-8", errors="replace").rsplit(".", 1)[-1] if wrapping is not None else ""
                        if wrapped_by.startswith("__import"):
                            loaded = wrapper
                        holder, member = loaded.parent, None
                        if holder is not None and holder.type == "member_expression" and holder.child_by_field_name("object") == loaded:
                            member, holder = holder.child_by_field_name("property"), holder.parent
                        local = (
                            holder.child_by_field_name("name") or holder.child_by_field_name("left")
                            if holder is not None and holder.type in ("variable_declarator", "assignment", "assignment_expression")
                            else None
                        )
                        text = (local.text or b"").decode("utf-8", errors="replace") if local is not None else ""
                        if local is not None and local.type == "identifier" and text:
                            name = member.text.decode("utf-8", errors="replace") if member is not None and member.text else None
                            bindings.append((rel_path, text, target, name, line))
                        elif local is not None and local.type == "object_pattern" and member is None:
                            for part in local.named_children:
                                key, value = (part, part) if part.type == "shorthand_property_identifier_pattern" else (
                                    part.child_by_field_name("key"), part.child_by_field_name("value")
                                )
                                if key is not None and value is not None and value.type in ("identifier", "shorthand_property_identifier_pattern") and key.text and value.text:
                                    bindings.append((
                                        rel_path, value.text.decode("utf-8", errors="replace"), target,
                                        key.text.decode("utf-8", errors="replace"), line,
                                    ))
                        elif (text.removeprefix("module.") == "exports" and member is None or wrapped_by.endswith("__exportStar")) and scope_kind == "file":
                            bindings.append((rel_path, "*", target, None, line))

            elif node.type in spec.type_annotations:
                # Every type named in it: `list[m.Item]` -> list, Item; `Promise<Store<T>>` -> Promise, Store, T.
                owner = owners.get(node.id, scope)
                types = [node]
                while types:
                    t = types.pop()
                    if t.type in ("identifier", "type_identifier", "attribute", "nested_type_identifier", "member_expression"):
                        named.append(("USES", owner, t, t.start_point.row + 1, frame))
                    else:
                        types.extend(t.named_children)
                # Nested annotations were collected above, so don't walk into them again.
                continue

            elif node.type in spec.imports:
                for module in node.children_by_field_name(spec.imports[node.type]):
                    target = cls._import_target(module)
                    if target is None:
                        continue
                    refs.append((rel_path, "IMPORTS", target, rel_path, line, None, None))
                    # `from pkg import mod` may import a submodule, so try `pkg.mod` as a module too.
                    joiner = "" if target.endswith(spec.separator) else spec.separator
                    for imported in node.children_by_field_name(spec.imported_names.get(node.type, "")):
                        name = cls._import_target(imported)
                        if name is not None:
                            refs.append((rel_path, "IMPORTS", f"{target}{joiner}{name}", rel_path, line, None, None))
                    bindings.extend((rel_path, local, bound, name, line) for local, bound, name in cls._bindings(node, module, target))

                # `export { a, b as c }` exports names the file defines.
                if node.type == "export_statement" and not node.children_by_field_name("source"):
                    for clause in (c for c in node.named_children if c.type == "export_clause"):
                        for specifier in clause.named_children:
                            local = specifier.child_by_field_name("name")
                            if local is not None and local.text:
                                export_listed.add(local.text.decode("utf-8", errors="replace"))

                # `export default ...` names what importing the file's default gives.
                if node.type == "export_statement" and any(c.type == "default" for c in node.children):
                    declaration = node.child_by_field_name("declaration")
                    value = node.child_by_field_name("value")
                    default = declaration.child_by_field_name("name") if declaration is not None else value
                    if default is not None and default.type in spec.default_exports:
                        # Unnamed, so named after its file like the symbol it defines.
                        refs.append((rel_path, "DEFAULT", unnamed, rel_path, line, None, None))
                    elif default is not None and default.type in ("identifier", "type_identifier") and default.text:
                        refs.append((rel_path, "DEFAULT", default.text.decode("utf-8", errors="replace"), rel_path, line, None, None))
                        export_listed.add(default.text.decode("utf-8", errors="replace"))

            stack.extend((child, scope, scope_kind, prefix, frame) for child in reversed(node.named_children))

        local_names -= not_locals

        # Last name in each expression: `a.b.c()` -> "c", `new Foo()` -> "Foo", `Generic[T]` -> "Generic".
        # What it is reached through is its qualifier, when a plain dotted name: `a.b.c()` -> "a.b".
        for kind, src_id, expression, line, frame in named:
            qualifier = None
            through = next(
                (
                    expression.child_by_field_name(field)
                    for field in ("object", "operand", "module", "package")
                    if expression is not None and expression.child_by_field_name(field) is not None
                ),
                None,
            )
            if through is not None and through.text:
                text = through.text.decode("utf-8", errors="replace")
                if all(part.replace("$", "_").isidentifier() for part in text.split(".")):
                    qualifier = text
                elif through.type == "call" and through.child_by_field_name("function") is not None and (
                    through.child_by_field_name("function").text == b"super"
                ):
                    qualifier = "super"  # python's `super().save()`, like ts's `super.save()`
                elif kind in ("CALLS", "REFERENCES"):
                    qualifier = ""  # on some other expression: `rows.filter(f).map(g)`

            # The type of what it's called on, when known: the enclosing class for `self.save()`,
            # `this.save()`, and `super().save()`, else the type a declaration in its scope, or one
            # around it, gives the first name: `s.add()` after `s = Store()`. On a call, what that call
            # names, whose return type it is: `make().add()`, `self.make().add()`.
            receiver = None
            if kind in ("CALLS", "REFERENCES") and qualifier == "":
                called = cls._type_name(through) if through is not None and through.type in spec.calls else None
                receiver = typed(src_id, called) if called is not None else None
            elif kind in ("CALLS", "REFERENCES") and qualifier is not None:
                head = qualifier.split(".")[0]
                receiver = typed(src_id, head)
                receiver = None if receiver == head else receiver
            while expression is not None and expression.type not in (
                "identifier", "property_identifier", "private_property_identifier", "type_identifier", "field_identifier",
                "shorthand_property_identifier",
            ):
                expression = (
                    expression.child_by_field_name("attribute")
                    or expression.child_by_field_name("property")
                    or expression.child_by_field_name("field")
                    or expression.child_by_field_name("name")
                    or expression.child_by_field_name("value")
                )
            if expression is None or not expression.text:
                continue
            target = expression.text.decode("utf-8", errors="replace")
            # A value or bare call named by a local name, `f(data)` or `num(v)` in a function with its own
            # `data` or `const num = ...`, isn't the file's.
            if (kind == "REFERENCES" or (kind == "CALLS" and qualifier is None)) and receiver is None:
                head, around, local = qualifier.split(".")[0] if qualifier else target, frame, False
                while around is not None and not local:
                    local, around = (around, head) in local_names, frame_parents.get(around)
                if local:
                    continue
            refs.append((src_id, kind, target, rel_path, line, qualifier, receiver))

        # Exported: a top-level symbol visible on its own or listed, though python's `__all__` decides alone
        # when there is one; a member visible on its own of an exported class or namespace; nothing in a
        # function. Owners come before their members.
        exported: dict[str, bool] = {}
        for symbol_id, _, name, _, _, _, _, parent_id, *_ in symbols[1:]:
            if parent_id == rel_path:
                exported[symbol_id] = name in listed if listed is not None else visible[symbol_id] or name in export_listed
            elif kinds.get(parent_id) in ("class", "interface") or parent_id in namespaces:
                exported[symbol_id] = visible[symbol_id] and exported.get(parent_id, False)
            else:
                exported[symbol_id] = False
        symbols = [(*symbol, exported.get(symbol[0])) for symbol in symbols]

        # Attribute types, for calls on attributes from other files: `s.store.add()`.
        refs.extend(
            (owner, "TYPED", name, rel_path, attribute_lines[(owner, name)], None, type_name)
            for (owner, name), type_name in attribute_types.items()
            if owner is not None and type_name is not None
        )
        return symbols, refs, bindings

    @classmethod
    def _extract_all(
        cls,
        db: sqlite3.Connection,
        root: pathlib.Path,
        deadline: float,
        languages: list[tuple[list[str], LanguageSpec]],
        ignored_dirs: set[str],
        max_file_bytes: int = 1_000_000,
    ) -> tuple[int, int, int, bool]:
        known = dict(db.execute("SELECT path, sha256 FROM files"))
        seen: set[str] = set()
        parsers: dict[str, tree_sitter.Parser] = {}
        parsed = unchanged = failed = 0

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in ignored_dirs)
            for filename in sorted(filenames):
                spec = next(
                    (rule for suffixes, rule in languages if any(filename.endswith(pattern) for pattern in suffixes)),
                    None,
                )
                if spec is None:
                    continue
                if time.monotonic() > deadline:
                    # Stop early; unchanged files are skipped on the next call,
                    # so repeated builds pick up where this one left off.
                    log.warning("~ build timed out, graph is partial")
                    return parsed, unchanged, failed, False

                path = pathlib.Path(dirpath) / filename
                rel_path = path.relative_to(root).as_posix()
                try:
                    rel_path.encode("utf-8")
                except UnicodeEncodeError:
                    # sqlite can't store a file name that isn't utf-8, and would fail the whole build.
                    log.warning(f"~ skipped file name that isn't utf-8: {rel_path!r}")
                    failed += 1
                    continue
                seen.add(rel_path)
                try:
                    # Regular files only; a pipe would block and a device could never end.
                    if not path.is_file() or path.stat().st_size > max_file_bytes:
                        continue
                    source = path.read_bytes()
                except OSError:
                    log.warning(f"~ failed to read {rel_path}")
                    failed += 1
                    continue

                digest = cls._digest(source)
                if known.get(rel_path) == digest:
                    unchanged += 1
                    continue

                if spec.name not in parsers:
                    parsers[spec.name] = tree_sitter.Parser(spec.language)
                # Logged first, so the last one names the file if the parser crashes the process.
                log.debug(f". parsing {rel_path}")
                try:
                    symbols, refs, bindings = cls._extract(parsers[spec.name].parse(source), rel_path, spec)
                except Exception:
                    log.exception(f"! failed to parse {rel_path}")
                    failed += 1
                    continue

                # Symbols, refs, and bindings of the file go with it, via ON DELETE CASCADE.
                db.execute("DELETE FROM files WHERE path = ?", (rel_path,))
                db.execute("INSERT INTO files VALUES (?, ?, ?)", (rel_path, spec.name, digest))
                db.executemany("INSERT OR REPLACE INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", symbols)
                db.executemany("INSERT INTO refs VALUES (?, ?, ?, ?, ?, ?, ?)", refs)
                db.executemany("INSERT INTO bindings VALUES (?, ?, ?, ?, ?)", bindings)
                parsed += 1
                if parsed % 256 == 0:
                    # Kept as it goes, so a build that dies has parsed what it did when called again.
                    db.commit()

        # Files gone from disk; their symbols and refs go with them.
        db.executemany("DELETE FROM files WHERE path = ?", [(rel_path,) for rel_path in known.keys() - seen])
        return parsed, unchanged, failed, True

    @classmethod
    def _resolve(
        cls,
        db: sqlite3.Connection,
        languages: list[tuple[list[str], LanguageSpec]],
        root: pathlib.Path,
        deadline: float,
    ):
        """
        Rebuild edges from symbols and raw refs, or raise TimeoutError past the deadline. Calls, bases, and types resolve
        exactly through definitions and import bindings when they can, else by
        name, preferring the same file, then imported files, then a unique match.
        Methods override their bases' namesakes, and calls through a type also
        call the overrides in its subtypes.
        """

        def on_time(count: int):
            # Checked every so often in long loops; a build that runs out of time keeps the last edges.
            if count % 1024 == 0 and time.monotonic() > deadline:
                raise TimeoutError("resolving edges timed out")

        listings: dict[pathlib.PurePosixPath, set[str]] = {}

        def has(directory: pathlib.PurePosixPath, name: str) -> bool:
            # Whether a directory of the tree has a file of a name, listing each directory once, as looking for
            # each config's name in each directory costs more, on slow file systems most of all.
            if directory not in listings:
                try:
                    listings[directory] = {entry.name for entry in os.scandir(root / directory) if entry.is_file()}
                except OSError:
                    listings[directory] = set()
            return name in listings[directory]

        files = {path for (path,) in db.execute("SELECT path FROM files")}
        by_name: dict[str, list[tuple[str, str, str]]] = {}
        by_qualified: dict[tuple[str, str], tuple[str, str]] = {}  # (file, qualified name) -> (id, kind)
        for symbol_id, name, qualified_name, file_path, kind in db.execute(
            "SELECT id, name, qualified_name, file_path, kind FROM symbols WHERE kind != 'file'"
        ):
            by_name.setdefault(name, []).append((symbol_id, file_path, kind))
            by_qualified[(file_path, qualified_name)] = (symbol_id, kind)

        # Each file's bindings: local name -> [(module, name)], name None for the module itself, "*" for all its names.
        bound: dict[tuple[str, str], list[tuple[str, str | None]]] = {}
        for file_path, local, module, name in db.execute("SELECT file_path, local, module, name FROM bindings"):
            bound.setdefault((file_path, local), []).append((module, name))
        defaults = dict(db.execute("SELECT file_path, target FROM refs WHERE kind = 'DEFAULT'"))
        # Names a file exports for others: `exports.save = store` -> (file, "save") -> "store".
        exporting = {(file_path, target): receiver for file_path, target, receiver in db.execute(
            "SELECT file_path, target, receiver FROM refs WHERE kind = 'EXPORTS'"
        )}

        db.execute("DELETE FROM edges")
        db.execute(
            "INSERT INTO edges SELECT parent_id, id, 'CONTAINS', file_path, start_line, 'exact' "
            "FROM symbols WHERE parent_id IS NOT NULL"
        )

        # Package languages (go) group files by directory, and go.mod files map module paths to directories.
        # Files of one family share import syntax, so can call each other: python, js and ts, or go.
        packages: dict[str, set[str]] = {}
        specs: dict[str, GraphClient.LanguageSpec] = {}
        for path in files:
            spec = next(
                (rule for suffixes, rule in languages if any(path.endswith(pattern) for pattern in suffixes)),
                None,
            )
            if spec is not None:
                specs[path] = spec
            if spec is not None and spec.packages:
                packages.setdefault(pathlib.PurePosixPath(path).parent.as_posix(), set()).add(path)
        modules: dict[str, str] = {}
        for directory in {d for package in packages for d in (pathlib.PurePosixPath(package), *pathlib.PurePosixPath(package).parents)}:
            manifest = root / directory / "go.mod"
            if has(directory, "go.mod"):
                declaration = next((l for l in manifest.read_text(errors="replace").splitlines() if l.startswith("module ")), None)
                if declaration is not None:
                    modules[declaration.split()[1].strip('"')] = directory.as_posix()

        # Packages of the tree by their package.json name, and package.json files by their directory, from the
        # directories of js and ts files and those around them.
        manifests: dict[str, tuple[pathlib.PurePosixPath, dict]] = {}
        package_dirs: dict[pathlib.PurePosixPath, dict] = {}
        for directory in sorted({
            d for path, spec in specs.items() if spec.separator == "/" and not spec.packages for d in pathlib.PurePosixPath(path).parents
        }):
            manifest = root / directory / "package.json"
            try:
                package = json.loads(manifest.read_text(errors="replace")) if has(directory, "package.json") else None
            except (OSError, ValueError) as ex:
                log.warning(f"~ skipping {manifest}: {ex}")
                continue
            if isinstance(package, dict):
                package_dirs[directory] = package
            if isinstance(package, dict) and isinstance(package.get("name"), str):
                manifests.setdefault(package["name"], (directory, package))

        # Python distributions of the tree by their top-level import names: the packages each project's packaging
        # config names, or those in the directories it says hold them, else in its src and itself, with modules too
        # where it says; though not ones conventionally not shipped, e.g. tests.
        packages_in: dict[pathlib.PurePosixPath, set[str]] = {}  # directory -> names of packages in it
        modules_in: dict[pathlib.PurePosixPath, set[str]] = {}  # directory -> names of modules in it
        for path, spec in specs.items():
            if spec.separator == ".":
                posix = pathlib.PurePosixPath(path)
                if posix.name == "__init__.py":
                    packages_in.setdefault(posix.parent.parent, set()).add(posix.parent.name)
                else:
                    modules_in.setdefault(posix.parent, set()).add(posix.stem)
        distributions: dict[str, list[str]] = {}
        for project in sorted({
            d for path, spec in specs.items() if spec.separator == "." for d in pathlib.PurePosixPath(path).parents
            if any(has(d, name) for name in ("pyproject.toml", "setup.cfg", "setup.py")) and not has(d, "__init__.py")
        }):
            named, roots = cls._python_layout(root / project)
            found = {name: pathlib.PurePosixPath(os.path.normpath(project / path)) for name, path in named.items()}
            for holder in roots or ["src", "."]:
                directory = pathlib.PurePosixPath(os.path.normpath(project / holder))
                for name in sorted(packages_in.get(directory, set()) | (modules_in.get(directory, set()) if roots else set())):
                    if name not in cls._unshipped:
                        found.setdefault(name, directory / name)
            for name, path in found.items():
                distributions.setdefault(name, []).append(path.as_posix())

        configs: dict[pathlib.PurePosixPath, tuple[str | None, dict[str, list[str]]] | None] = {}  # directory -> its tsconfig's aliases
        bundlers: dict[pathlib.PurePosixPath, dict[str, list[str]] | None] = {}  # directory -> its vite or webpack config's aliases
        imported: dict[str, set[str]] = {}
        module_files: dict[tuple[str, str], set[str]] = {}  # (importer, module as written) -> files it resolves to
        import_edges: list[tuple] = []
        for index, (src_id, target, file_path, line) in enumerate(db.execute(
            "SELECT src_id, target, file_path, line FROM refs WHERE kind = 'IMPORTS'"
        )):
            on_time(index)
            spec = next(
                (rule for suffixes, rule in languages if any(file_path.endswith(pattern) for pattern in suffixes)),
                None,
            )
            if spec is None:
                continue

            if spec.packages:
                # Go: a module path from go.mod plus a directory in it; standard and external packages have no edge.
                module = max((m for m in modules if target == m or target.startswith(f"{m}/")), key=len, default=None)
                if module is None:
                    continue
                directory = os.path.normpath(os.path.join(modules[module], target[len(module):].lstrip("/")))
                for dst_id in sorted(packages.get(directory, set())):
                    imported.setdefault(file_path, set()).add(dst_id)
                    module_files.setdefault((file_path, target), set()).add(dst_id)
                    import_edges.append((src_id, dst_id, "IMPORTS", file_path, line, "exact"))
                continue

            base = pathlib.PurePosixPath(file_path).parent
            if spec.separator == ".":
                # Python: ".mod" is relative to the package, "..pkg.mod" goes up one level.
                dots = len(target) - len(target.lstrip("."))
                module = target.lstrip(".").replace(".", "/")
                if dots:
                    for _ in range(dots - 1):
                        base = base.parent
                    stems = [(base / module).as_posix()]
                else:
                    # Absolute: from the root of the importer's project, which in a monorepo may be any directory
                    # around it that isn't a package, nearest first, or a src layout's: `from app.models import User`
                    # in services/api. Not from its own package: `import subprocess` in asyncio isn't asyncio's.
                    stems = [
                        (directory / layout / module).as_posix()
                        for directory in (base, *base.parents) if (directory / "__init__.py").as_posix() not in files
                        for layout in ("", "src")
                    ]
                    # Then a distribution of the tree by its import name, as installed ones are imported from anywhere:
                    # `from text.clean import strip` in services/api for libs/text/src/text, nearest first.
                    head, _, rest = module.partition("/")
                    for directory in sorted(
                        distributions.get(head, []), key=lambda d: (-len(os.path.commonpath([d, file_path])), d),
                    ):
                        stems.append(f"{directory}/{rest}" if rest else directory)
            elif target.startswith("."):
                # JS/TS: "./x" and "../x" are relative.
                stems = [(base / target).as_posix()]
            else:
                # JS/TS: anything else may be an alias from the nearest tsconfig or jsconfig, `"@/*": ["./src/*"]`,
                # or under its base url, and is otherwise a package.
                settings = None
                for directory in (base, *base.parents):
                    if directory not in configs:
                        config = next(
                            (root / directory / name for name in ("tsconfig.json", "jsconfig.json") if has(directory, name)),
                            None,
                        )
                        configs[directory] = None if config is None else cls._path_aliases(config, root)
                    if configs[directory] is not None:
                        settings = configs[directory]
                        break
                base_url, aliases = settings or (None, {})
                # The aliases of vite and webpack configs in the nearest directory with any, though the tsconfig's win:
                # `alias: { "@": "/src" }`. Configs listed first win over those after them.
                for directory in (base, *base.parents):
                    if directory not in bundlers:
                        found = [
                            (root / directory / name, rule)
                            for name in reversed(cls._bundler_configs) if has(directory, name)
                            for suffixes, rule in languages if any(name.endswith(suffix) for suffix in suffixes)
                        ]
                        bundlers[directory] = {
                            pattern: substitutions for config, rule in found for pattern, substitutions in cls._bundler_aliases(config, root, rule).items()
                        } if found else None
                    if bundlers[directory] is not None:
                        aliases = {**bundlers[directory], **aliases}
                        break

                # An exact pattern wins over wildcards, then the longest prefix; its substitutions go in order, then the base url.
                stems = []
                for pattern, substitutions in sorted(aliases.items(), key=lambda alias: ("*" in alias[0], -len(alias[0].partition("*")[0]))):
                    prefix, star, suffix = pattern.partition("*")
                    if target == pattern if not star else (
                        target.startswith(prefix) and target.endswith(suffix) and len(target) >= len(prefix) + len(suffix)
                    ):
                        stems = [s.replace("*", target[len(prefix):len(target) - len(suffix)], 1) for s in substitutions]
                        break

                # Or a name in the imports map of the nearest package.json: `#utils/dates`.
                if target.startswith("#"):
                    directory = next((d for d in (base, *base.parents) if d in package_dirs), None)
                    if directory is not None:
                        stems += cls._package_stems(directory, package_dirs[directory], target)

                # Or a package of the tree by its package.json name, as workspaces import each other: `@acme/ui/button`.
                name = max((n for n in manifests if target == n or target.startswith(f"{n}/")), key=len, default=None)
                if name is not None:
                    stems += cls._package_stems(*manifests[name], f".{target[len(name):]}")
                if base_url is not None:
                    stems.append(f"{base_url}/{target}")

            # The importer's own language first, then others sharing its import syntax.
            family = sorted(
                (rule for rule in languages if rule[1].separator == spec.separator and not rule[1].packages),
                key=lambda rule: rule[1] is not spec,
            )
            paths = []
            for stem in map(os.path.normpath, stems):
                prefix = "" if stem == "." else f"{stem}/"
                paths.append(stem)
                for suffixes, rule in family:
                    paths += [f"{stem}{suffix}" for suffix in suffixes]
                for suffixes, rule in family:
                    paths += [f"{prefix}{index}{suffix}" for index in rule.index_names for suffix in suffixes]

            dst_id = next((path for path in paths if path in files), None)
            if dst_id:
                imported.setdefault(file_path, set()).add(dst_id)
                module_files.setdefault((file_path, target), set()).add(dst_id)
                import_edges.append((src_id, dst_id, "IMPORTS", file_path, line, "exact"))

        # Files in one package directory see each other's names without importing.
        package_of: dict[str, set[str]] = {}  # file -> the files of its package
        for siblings in packages.values():
            for path in siblings:
                imported.setdefault(path, set()).update(siblings - {path})
                package_of[path] = siblings

        # Index files (`index.ts`, `__init__.py`) usually re-export their package, so names imported
        # through them are visible from the files they import, following chains of index files.
        index_stems = {index for _, rule in languages for index in rule.index_names}
        for visible in imported.values():
            pending = [path for path in visible if pathlib.PurePosixPath(path).stem in index_stems]
            while pending:
                for path in imported.get(pending.pop(), set()) - visible:
                    visible.add(path)
                    if pathlib.PurePosixPath(path).stem in index_stems:
                        pending.append(path)

        def follow(start: str, names: tuple[str, ...]) -> tuple[list[tuple[str, str]], bool]:
            # Definitions a name in a file reaches, as (id, kind), from the file's own definitions through
            # its bindings, `*` imports, re-exports, and package siblings, and whether it names something
            # outside the tree. `m.Session` with `from . import models as m` -> models.py's Session; names
            # bound to modules outside the tree, e.g. `json.dumps` or go's `errors.New`, are outside.
            found: list[tuple[str, str]] = []
            external = False
            pending = [(start, names)]
            visited: set[tuple[str, tuple[str, ...]]] = set()
            while pending and len(visited) < 64:
                at, names = pending.pop()
                if (at, names) in visited:
                    continue
                visited.add((at, names))
                hit = by_qualified.get((at, ".".join(names)))
                if hit is not None:
                    found.append(hit)
                    continue
                if (at, names[0]) in exporting:
                    pending.append((at, (*exporting[(at, names[0])].split("."), *names[1:])))
                head, rest = names[0], names[1:]
                reached = False
                for module, name in bound.get((at, head), []):
                    if name is None:
                        # The module itself; python's following segments may name submodules, longest first.
                        for split in range(len(rest), -1, -1):
                            submodule = ".".join((module, *rest[:split])) if split else module
                            if (at, submodule) in module_files:
                                reached = True
                                following = rest[split:]
                                for g in module_files[(at, submodule)]:
                                    if following:
                                        pending.append((g, following))
                                    # A commonjs module is also what it assigns `module.exports`: `const Store = require("./store")`,
                                    # and its `default` is that: compiled typescript's `store_1.default`.
                                    if g in defaults:
                                        pending.append((g, (defaults[g], *(following[1:] if following[:1] == ("default",) else following))))
                                break
                    else:
                        # A name in the module, which in python may itself be a submodule: `from . import util`.
                        joined = f"{module}{'' if module.endswith('.') else '.'}{name}"
                        if (at, joined) in module_files:
                            reached = True
                            if rest:
                                pending += [(g, rest) for g in module_files[(at, joined)]]
                        for g in module_files.get((at, module), ()):
                            reached = True
                            pending.append((g, (defaults.get(g, name) if name == "default" else name, *rest)))
                            # Or a member of that: `const { load } = require("./store")` with `module.exports = { load() {} }`.
                            if name != "default" and g in defaults:
                                pending.append((g, (defaults[g], name, *rest)))
                if at == start and bound.get((at, head)) and not reached:
                    external = True
                for module, _ in bound.get((at, "*"), []):
                    pending += [(g, names) for g in module_files.get((at, module), ())]
                if at == start:
                    pending += [(g, names) for g in package_of.get(at, set()) - {at}]
            return found, external

        # Each symbol's file, qualified name, and kind; the types of class attributes, (class, attribute) ->
        # (file, type); what functions return, function -> (file, type); and the exact bases of classes,
        # filled in as bases resolve, before calls need them.
        where = {symbol_id: (file_path, qualified_name, kind) for (file_path, qualified_name), (symbol_id, kind) in by_qualified.items()}
        typed = {
            (src_id, target): (file_path, receiver)
            for src_id, target, file_path, receiver in db.execute("SELECT src_id, target, file_path, receiver FROM refs WHERE kind = 'TYPED'")
        }
        returns = {
            src_id: (file_path, receiver)
            for src_id, file_path, receiver in db.execute("SELECT src_id, file_path, receiver FROM refs WHERE kind = 'RETURNS'")
        }
        bases: dict[str, list[str]] = {}
        typeful = ("class", "interface", "variable")  # what a type can be; variables are object namespaces
        declared = {  # methods of interfaces, which declare them in the same file
            symbol_id for symbol_id, (file_path, qualified_name, kind) in where.items()
            if kind == "method" and by_qualified.get((file_path, qualified_name.rpartition(".")[0]), ("", ""))[1] == "interface"
        }

        def lineage(class_id: str) -> list[str]:
            # A class and its bases, nearest first.
            order = [class_id]
            for c in order:
                order += [b for b in bases.get(c, []) if b not in order]
            return order

        def member(type_id: str, name: str, inherited: bool = False) -> tuple[set[str], tuple[str, str] | None]:
            # A type's members of a name and its attribute's (file, type), if typed, from it or its nearest
            # base that has either, or only from its bases. Go declares methods in any file of their package.
            for c in lineage(type_id)[1 if inherited else 0:]:
                at, qualified, _ = where[c]
                method = f"{qualified}.{name}"
                members = {
                    by_qualified[(f, method)][0]
                    for f in {at} | package_of.get(at, set())
                    if (f, method) in by_qualified
                }
                if members or (c, name) in typed:
                    return members, typed.get((c, name))
            return set(), None

        def instances(symbol_id: str, kind: str, depth: int) -> tuple[set[str], bool]:
            # What a value named by a symbol can be: a class's instances, or what a function returns.
            if kind in typeful:
                return {symbol_id}, False
            if kind in ("function", "method") and symbol_id in returns and depth < 4:
                return types_of(*returns[symbol_id], depth + 1)
            return set(), False

        def types_of(start: str, expression: str, depth: int = 0) -> tuple[set[str], bool]:
            # The types of what an expression in a file names, and whether it is outside the tree. Its longest
            # prefix that resolves is a class for its instances or a function for what it returns, and each
            # name after it a member of the types before: "Store" -> Store, "make_store.copy" -> Store after
            # `def make_store() -> Store` and `def copy(self) -> Store`.
            names = tuple(expression.split("."))
            for split in range(len(names), 0, -1):
                found, external = follow(start, names[:split])
                if found or external:
                    break
            types: set[str] = set()
            for symbol_id, kind in found:
                reached, outside = instances(symbol_id, kind, depth)
                types, external = types | reached, external or outside
            for name in names[split:]:
                following: set[str] = set()
                for type_id in types:
                    members, attribute = member(type_id, name)
                    if attribute is not None and depth < 4:
                        reached, outside = types_of(*attribute, depth + 1)
                        following, external = following | reached, external or outside
                    else:
                        for member_id in members:
                            following |= instances(member_id, where[member_id][2], depth)[0]
                types = following
            return types, external

        # Bases must be classes or interfaces, type references any kind of type; calls and values anything.
        allowed = {"INHERITS": ("class", "interface"), "USES": ("class", "interface", "type", "enum")}
        named_edges: list[tuple] = []
        dispatched: list[tuple[str, set[str], set[str], str, int]] = []  # (caller, methods, receiver types, file, line)
        reachable: dict[str, set[str]] = {}  # file -> files its imports reach, directly or through others
        for index, (src_id, kind, target, file_path, line, qualifier, receiver) in enumerate(db.execute(
            "SELECT src_id, kind, target, file_path, line, qualifier, receiver FROM refs "
            "WHERE kind IN ('CALLS', 'INHERITS', 'USES', 'REFERENCES') ORDER BY kind IN ('CALLS', 'REFERENCES')"
        )):
            on_time(index)
            # Called on an expression, it can't be found by name: `rows.filter(f).map(g)`.
            found, external = follow(file_path, (*(qualifier.split(".") if qualifier else ()), target)) if qualifier != "" else ([], False)
            # A bare name defined in a function around it shadows those of the file, though not through
            # classes, whose names their methods don't see: `hydrate()` in `Provider` -> Provider.hydrate.
            if qualifier is None and src_id in where:
                at, scope_name, _ = where[src_id]
                parts = scope_name.split(".")
                for size in range(len(parts), 0, -1):
                    hit = by_qualified.get((at, ".".join((*parts[:size], target))))
                    if hit is not None and by_qualified.get((at, ".".join(parts[:size])), ("", ""))[1] in ("function", "method"):
                        found, external = [hit], False
                        break
            exact = {symbol_id for symbol_id, k in found if kind not in allowed or k in allowed[kind]}

            # Inferred: the type of what it's called on, through its attributes, then the method on that type
            # or its bases: `self.store.add()` -> Store.add after `self.store = Store()`. A class without the
            # method gets it from outside the tree, so it gets no edge.
            inferred: set[str] = set()
            if not exact and not external and receiver is not None:
                types, external = types_of(file_path, receiver)
                for attribute in qualifier.split(".")[1:]:
                    attribute_of: set[str] = set()
                    for type_id in types:
                        typed_attribute = member(type_id, attribute)[1]
                        if typed_attribute is not None:
                            reached, outside = types_of(*typed_attribute)
                            attribute_of, external = attribute_of | reached, external or outside
                    types = attribute_of
                for type_id in types:
                    inferred |= member(type_id, target, inherited=qualifier == "super")[0]
                if not inferred and any(where[type_id][2] == "class" for type_id in types):
                    external = True
                # Called on a value of these types, it may run their subtypes' overrides; `super()` runs its own.
                if kind == "CALLS" and inferred and qualifier != "super":
                    dispatched.append((src_id, inferred, types, file_path, line))

            # A value whose member isn't a symbol refers to what holds it: `Role.ADMIN` -> Role, for a ts enum.
            if kind == "REFERENCES" and not exact and not inferred and not external and qualifier:
                exact = {symbol_id for symbol_id, _ in follow(file_path, tuple(qualifier.split(".")))[0]}

            if exact:
                destinations, confidence = sorted(exact), "exact"
            elif inferred:
                destinations, confidence = sorted(inferred), "inferred"
            elif external or kind == "REFERENCES":
                # Values aren't guessed: most names in code are locals, not symbols.
                destinations, confidence = [], "exact"
            else:
                # Guess by name among what the file can reach: the same file, then files it imports, then a
                # unique match among files those import in turn. Only in its language family, only into tests
                # from tests, and calls only of what can be called: called on something, e.g. `items.map()`,
                # a method or a class; else a function or a class, as methods are only called on something.
                # Not for the language's own names, which calls of a name not defined, or of its types' methods
                # on unknown values, likely mean: `len(x)`, `row.get(k)`. Nor of an interface's methods, which
                # only declare what their types implement.
                spec = specs[file_path]
                callable_kinds = ("method", "class") if qualifier is not None else ("function", "class")
                testing = cls._test_paths.search(file_path) is not None
                candidates = [
                    (s, f) for s, f, k in by_name.get(target, [])
                    if (k in allowed[kind] if kind in allowed else k in callable_kinds)
                    and s not in declared
                    and (specs[f].separator, specs[f].packages) == (spec.separator, spec.packages)
                    and (testing or cls._test_paths.search(f) is None)
                ] if kind != "CALLS" or target not in (spec.builtins if qualifier is None else spec.builtin_methods) else []
                if candidates and file_path not in reachable:
                    reachable[file_path], pending = set(), [file_path]
                    while pending:
                        for g in imported.get(pending.pop(), set()) - reachable[file_path]:
                            reachable[file_path].add(g)
                            pending.append(g)
                visible = imported.get(file_path, set())
                within = [s for s, f in candidates if f in reachable.get(file_path, set())]
                dst_id = (
                    next((s for s, f in candidates if f == file_path), None)
                    or next((s for s, f in candidates if f in visible), None)
                    or (within[0] if len(within) == 1 else None)
                )
                destinations, confidence = ([dst_id] if dst_id else []), "guess"
            for dst_id in destinations:
                # A type naming itself, e.g. `type Pair = ...` or a method returning its own class, isn't a use;
                # nor is a function naming itself, e.g. to recurse.
                if not (kind in ("USES", "REFERENCES") and (dst_id == src_id or src_id.startswith(f"{dst_id}."))):
                    named_edges.append((src_id, dst_id, kind, file_path, line, confidence))
                if kind == "INHERITS" and confidence == "exact":
                    bases.setdefault(src_id, []).append(dst_id)

        # The methods of each class, interface, or object namespace by name; go declares methods in any file of
        # their type's package.
        methods_of: dict[str, dict[str, str]] = {}
        for symbol_id, (file_path, qualified_name, kind) in where.items():
            owner, _, name = qualified_name.rpartition(".")
            holder = next(
                (by_qualified[(f, owner)][0] for f in {file_path} | package_of.get(file_path, set()) if (f, owner) in by_qualified),
                None,
            ) if kind == "method" and owner else None
            if holder is not None:
                methods_of.setdefault(holder, {})[name] = symbol_id

        # A method overrides its namesake nearest in each of its class's exact bases, including interfaces it
        # implements, through that class. Embedding in go isn't overriding, as calls through what is embedded run
        # its own.
        lines = dict(db.execute("SELECT id, start_line FROM symbols"))
        overrides: dict[tuple[str, str], str] = {}  # (method, method it overrides) -> confidence
        overriders: dict[str, set[tuple[str, str]]] = {}  # method -> (method overriding it, type it does through)
        for class_id, methods in methods_of.items():
            if specs[where[class_id][0]].packages:
                continue
            for base_id in bases.get(class_id, []):
                for name, method_id in methods.items():
                    overridden = next((methods_of[c][name] for c in lineage(base_id) if name in methods_of.get(c, {})), None)
                    if overridden is not None:
                        overrides[(method_id, overridden)] = "exact"
                        overriders.setdefault(overridden, set()).add((method_id, class_id))

        # Structural types are implemented by having all their methods, so types are inferred to, through themselves:
        # go's interfaces by its types, ts's interfaces by classes not declaring them, and python's protocols, classes
        # with `Protocol` among their bases, by classes not subclassing them; each within its language family, and
        # where some file's imports reach both, so values can pass from one to the other. A type's methods are its
        # own or its bases', as go's promoted from what it embeds, the nearest of each name. Only names say they
        # match, so not types of only dunders, whose signatures are all they are: `__call__`.
        protocols = {
            src_id for src_id, file_path in db.execute("SELECT src_id, file_path FROM refs WHERE kind = 'INHERITS' AND target = 'Protocol'")
            if specs[file_path].separator == "." and src_id in where
        }
        method_sets: dict[str, dict[str, str]] = {}
        for type_id, (file_path, _, kind) in where.items():
            if kind in ("class", "interface") or (kind == "type" and specs[file_path].packages):
                method_sets[type_id] = {}
                for c in lineage(type_id):
                    for name, method_id in methods_of.get(c, {}).items():
                        method_sets[type_id].setdefault(name, method_id)
        structural = {i for i in method_sets if where[i][2] == "interface" or i in protocols}
        implemented: dict[str, set[str]] = {}  # type -> structural types it implements
        having: dict[tuple[str, bool, str], set[str]] = {}  # (family, method name) -> types with a method of that name
        for type_id, methods in method_sets.items():
            if type_id not in structural:
                spec = specs[where[type_id][0]]
                for name in methods:
                    having.setdefault((spec.separator, spec.packages, name), set()).add(type_id)
        importers: dict[str, set[str]] = {}  # file -> files importing it, directly or as their package
        for path, visible in imported.items():
            for g in visible:
                importers.setdefault(g, set()).add(path)
        reaching: dict[str, set[str]] = {}  # file -> files whose imports reach it, and itself

        def reached_by(path: str) -> set[str]:
            if path not in reaching:
                reaching[path], pending = {path}, [path]
                while pending:
                    for g in importers.get(pending.pop(), set()) - reaching[path]:
                        reaching[path].add(g)
                        pending.append(g)
            return reaching[path]

        for interface_id, methods in sorted(method_sets.items()):
            if interface_id not in structural or all(name.startswith("__") and name.endswith("__") for name in methods):
                continue
            spec = specs[where[interface_id][0]]
            for type_id in sorted(set.intersection(*(having.get((spec.separator, spec.packages, name), set()) for name in methods))):
                if interface_id in lineage(type_id) or not reached_by(where[type_id][0]) & reached_by(where[interface_id][0]):
                    continue  # declared, so overriding exactly, or out of reach
                implemented.setdefault(type_id, set()).add(interface_id)
                named_edges.append((type_id, interface_id, "INHERITS", where[type_id][0], lines[type_id], "inferred"))
                for name, method_id in methods.items():
                    overrides.setdefault((method_sets[type_id][name], method_id), "inferred")
                    overriders.setdefault(method_id, set()).add((method_sets[type_id][name], type_id))
        for (method_id, overridden), confidence in sorted(overrides.items()):
            named_edges.append((method_id, overridden, "OVERRIDES", where[method_id][0], lines[method_id], confidence))

        # A call through a type may run any override of what it calls, through any type that is one of the
        # receiver's: `store.save()` with `store: Base` runs Sub.save for a Sub, but not Other.save for
        # `store: Sub`. Each is inferred, as which runs depends on the value.
        subtyping: dict[str, set[str]] = {}  # type -> what it is: its lineage and what those implement
        called = {(src_id, dst_id, line) for src_id, dst_id, kind, _, line, _ in named_edges if kind == "CALLS"}
        for index, (src_id, methods, types, file_path, line) in enumerate(dispatched):
            on_time(index)
            reached: dict[str, set[str]] = {}  # override -> types it overrides through
            pending = list(methods)
            while pending:
                for method_id, through in overriders.get(pending.pop(), set()):
                    if method_id not in methods and through not in reached.setdefault(method_id, set()):
                        reached[method_id].add(through)
                        pending.append(method_id)
            for method_id, throughs in sorted(reached.items()):
                for through in throughs - subtyping.keys():
                    subtyping[through] = set(lineage(through))
                    for c in list(subtyping[through]):
                        for interface_id in implemented.get(c, set()):
                            subtyping[through].update(lineage(interface_id))
                if any(types & subtyping[through] for through in throughs) and (src_id, method_id, line) not in called:
                    called.add((src_id, method_id, line))
                    named_edges.append((src_id, method_id, "CALLS", file_path, line, "inferred"))

        db.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?)", import_edges + named_edges)

    @classmethod
    def _path_aliases(
        cls,
        config: pathlib.Path,
        root: pathlib.Path,
        seen: frozenset[pathlib.Path] = frozenset(),
    ) -> tuple[str | None, dict[str, list[str]]]:
        """
        Base url and path aliases of a tsconfig or jsconfig, following its extends and references.

        Args:
            config: Path to the config file
            root: Directory the returned paths are relative to
            seen: Configs already followed, to stop cycles

        Returns:
            The base url, or None, and each alias pattern's substitutions
        """

        if config in seen or not config.is_file():
            return None, {}
        seen = seen | {config}
        try:
            # Json with comments and trailing commas: drop both, leaving strings as they are.
            text = config.read_text(errors="replace")
            text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', lambda m: m[0] if m[0][0] == '"' else "", text, flags=re.S)
            text = re.sub(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])', lambda m: m[0] if m[0][0] == '"' else "", text)
            settings = json.loads(text)
        except (OSError, ValueError) as ex:
            log.warning(f"~ skipping {config}: {ex}")
            return None, {}
        if not isinstance(settings, dict):
            return None, {}

        # Later parents override earlier ones, and this config overrides them all; `paths` is replaced whole.
        base_url: str | None = None
        aliases: dict[str, list[str]] = {}
        extends = settings.get("extends")
        for parent in [extends] if isinstance(extends, str) else extends if isinstance(extends, list) else []:
            if not isinstance(parent, str):
                continue
            # Relative, or a package's, found as node finds packages: in node_modules of its directory or one above.
            path = config.parent / parent if parent.startswith(".") else next(
                (d / "node_modules" / parent for d in (config.parent, *config.parent.parents) if (d / "node_modules").is_dir()
                 and any(p.exists() for p in (d / "node_modules" / parent, d / "node_modules" / f"{parent}.json"))),
                None,
            )
            if path is not None:
                path = path if path.is_file() else path / "tsconfig.json" if path.is_dir() else path.with_name(f"{path.name}.json")
                parent_base_url, parent_aliases = cls._path_aliases(path, root, seen)
                base_url, aliases = parent_base_url or base_url, parent_aliases or aliases

        options = settings.get("compilerOptions")
        options = options if isinstance(options, dict) else {}
        if isinstance(options.get("baseUrl"), str):
            base_url = os.path.relpath(config.parent / options["baseUrl"], root)
        if isinstance(options.get("paths"), dict):
            # Substitutions are relative to the base url when there is one, else to the config defining them.
            base = root / base_url if base_url is not None else config.parent
            aliases = {
                pattern: [os.path.relpath(base / s, root) for s in substitutions if isinstance(s, str)]
                for pattern, substitutions in options["paths"].items() if isinstance(substitutions, list)
            }

        # Solution-style configs keep their options in the projects they reference.
        references = settings.get("references")
        for reference in references if isinstance(references, list) else []:
            if isinstance(reference, dict) and isinstance(reference.get("path"), str):
                path = config.parent / reference["path"]
                referenced_base_url, referenced_aliases = cls._path_aliases(path / "tsconfig.json" if path.is_dir() else path, root, seen)
                base_url, aliases = base_url or referenced_base_url, {**referenced_aliases, **aliases}
        return base_url, aliases

    # Top-level names of python packages and modules that projects conventionally don't ship, as setuptools' discovery.
    _unshipped: set[str] = {
        "tests", "test", "testing", "docs", "doc", "examples", "example", "scripts", "tools", "benchmarks", "build",
        "dist", "setup", "conftest", "noxfile",
    }

    @classmethod
    def _python_layout(
        cls,
        project: pathlib.Path,
    ) -> tuple[dict[str, str], list[str]]:
        """
        Where a python project's packaging config puts what it ships, from its pyproject.toml, setup.cfg, or setup.py.

        Args:
            project: Directory of the project's packaging config

        Returns:
            Paths relative to the project of the packages it names, by import name: setuptools' `package-dir`
            `{acme = "source/acme_impl"}`, poetry's `{include = "acme", from = "lib"}`, hatch's `["src/acme"]`; and
            of directories it says hold the others: setuptools' `{"" = "lib"}` and `find.where`, pdm's
            `package-dir`; empty if it doesn't say
        """

        named: dict[str, str] = {}
        roots: list[str] = []

        def get(table: object, *keys: str) -> object:
            # A value nested in toml tables, or None.
            for key in keys:
                table = table.get(key) if isinstance(table, dict) else None
            return table

        try:
            pyproject = project / "pyproject.toml"
            settings = tomllib.loads(pyproject.read_text(errors="replace")) if pyproject.is_file() else {}
        except (OSError, tomllib.TOMLDecodeError) as ex:
            log.warning(f"~ skipping {project / 'pyproject.toml'}: {ex}")
            settings = {}
        package_dir = get(settings, "tool", "setuptools", "package-dir")
        for name, path in package_dir.items() if isinstance(package_dir, dict) else []:
            if isinstance(path, str) and not name:
                roots.append(path)
            elif isinstance(path, str) and name.isidentifier():
                named[name] = path
        where = get(settings, "tool", "setuptools", "packages", "find", "where")
        roots += [path for path in where if isinstance(path, str)] if isinstance(where, list) else []
        poetry = get(settings, "tool", "poetry", "packages")
        for entry in poetry if isinstance(poetry, list) else []:
            include, origin = get(entry, "include"), get(entry, "from")
            if isinstance(include, str) and include.split("/")[0].isidentifier():
                named[include.split("/")[0]] = os.path.join(origin if isinstance(origin, str) else "", include.split("/")[0])
        for hatch in (get(settings, "tool", "hatch", "build", "packages"), get(settings, "tool", "hatch", "build", "targets", "wheel", "packages")):
            for path in hatch if isinstance(hatch, list) else []:
                if isinstance(path, str) and pathlib.PurePosixPath(path).name.isidentifier():
                    named[pathlib.PurePosixPath(path).name] = path
        pdm = get(settings, "tool", "pdm", "build", "package-dir")
        if isinstance(pdm, str):
            roots.append(pdm)

        # setup.cfg's `package_dir = =src` or `acme = source/acme_impl` lines, and `where = src` to find them.
        try:
            config = configparser.ConfigParser()
            config.read_string((project / "setup.cfg").read_text(errors="replace") if (project / "setup.cfg").is_file() else "")
            for line in config.get("options", "package_dir", fallback="").splitlines():
                name, _, path = (part.strip() for part in line.partition("="))
                if path and not name:
                    roots.append(path)
                elif path and name.isidentifier():
                    named[name] = path
            if config.get("options.packages.find", "where", fallback="").strip():
                roots.append(config.get("options.packages.find", "where").strip())
        except (OSError, configparser.Error) as ex:
            log.warning(f"~ skipping {project / 'setup.cfg'}: {ex}")

        # setup.py's `package_dir={"": "lib"}` and `find_packages(where="src")`, read as text, as it is code.
        try:
            text = (project / "setup.py").read_text(errors="replace") if (project / "setup.py").is_file() else ""
        except OSError as ex:
            log.warning(f"~ skipping {project / 'setup.py'}: {ex}")
            text = ""
        mapping = re.search(r"package_dir\s*=\s*\{([^}]*)\}", text)
        for name, path in re.findall(r"""["']([\w]*)["']\s*:\s*["']([^"']+)["']""", mapping[1] if mapping else ""):
            if name:
                named[name] = path
            else:
                roots.append(path)
        roots += re.findall(r"""find_(?:namespace_)?packages\(\s*(?:where\s*=\s*)?["']([^"']+)["']""", text)
        return named, list(dict.fromkeys(path.strip("/") or "." for path in roots))

    # Bundler configs that may alias imports, nearest first in each directory.
    _bundler_configs: list[str] = [
        f"{tool}.config.{suffix}" for tool in ("vite", "vitest", "webpack") for suffix in ("ts", "js", "mjs", "cjs")
    ]

    @classmethod
    def _bundler_aliases(
        cls,
        config: pathlib.Path,
        root: pathlib.Path,
        spec: LanguageSpec,
    ) -> dict[str, list[str]]:
        """
        Import aliases of a vite or webpack config, as tsconfig path patterns.

        Args:
            config: Path to the config file
            root: Directory the returned paths are relative to
            spec: Language of the config file

        Returns:
            Each alias pattern's substitutions: `alias: { "@": path.resolve(__dirname, "src") }` -> {"@": ["src"],
            "@/*": ["src/*"]}, and webpack's exact `"x$"` -> {"x": [...]}
        """

        try:
            tree = tree_sitter.Parser(spec.language).parse(config.read_bytes())
        except (OSError, ValueError) as ex:
            log.warning(f"~ skipping {config}: {ex}")
            return {}

        def text(node: tree_sitter.Node | None) -> str | None:
            # A key's or string's text: `"@"`, `components`.
            if node is None or node.type not in ("string", "property_identifier", "identifier"):
                return None
            return cls._import_target(node)

        # `alias: { "@": ... }`, or vite's `alias: [{ find: "@", replacement: ... }]`.
        found: list[tuple[str, tree_sitter.Node]] = []
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            pending.extend(node.named_children)
            value = node.child_by_field_name("value") if node.type == "pair" and text(node.child_by_field_name("key")) == "alias" else None
            for entry in value.named_children if value is not None and value.type in ("object", "array") else []:
                if entry.type == "pair" and text(entry.child_by_field_name("key")) is not None and entry.child_by_field_name("value") is not None:
                    found.append((text(entry.child_by_field_name("key")) or "", entry.child_by_field_name("value")))
                elif entry.type == "object":
                    fields = {
                        text(p.child_by_field_name("key")): p.child_by_field_name("value") for p in entry.named_children if p.type == "pair"
                    }
                    find, replacement = fields.get("find"), fields.get("replacement")
                    if find is not None and find.type == "string" and replacement is not None:
                        found.append((text(find) or "", replacement))

        # Replacements are paths from the config's directory, given in pieces: `path.resolve(__dirname, "src")`,
        # `"/src"`, `` `${__dirname}/src` ``, `new URL("./src", import.meta.url)`.
        aliases: dict[str, list[str]] = {}
        for find, replacement in found:
            pieces, nodes = [], [replacement]
            while nodes:
                node = nodes.pop(0)
                if node.type == "string_fragment" and node.text:
                    pieces.append(node.text.decode("utf-8", errors="replace").lstrip("/"))
                else:
                    nodes[:0] = node.named_children
            if not find or not pieces:
                continue
            target = os.path.relpath(os.path.normpath(os.path.join(config.parent, *pieces)), root)
            if find.endswith("$"):
                aliases[find[:-1]] = [target]
            else:
                aliases[find] = [target]
                aliases[f"{find.rstrip('/')}/*"] = [f"{target}/*"]
        return aliases

    @classmethod
    def _package_stems(
        cls,
        directory: pathlib.PurePosixPath,
        manifest: dict,
        subpath: str,
    ) -> list[str]:
        """
        Paths without extensions a package.json's package may have a subpath of in source, most likely first.

        Args:
            directory: Directory of the package.json, relative to the analyzed root
            manifest: The package.json's settings
            subpath: Path in the package as imported, "." for the package itself, "./button" for `@acme/ui/button`,
                or a name in its imports map, `#utils/dates`

        Returns:
            Paths relative to the analyzed root: what its exports or imports map, or entry fields for the package itself,
            name, each also in src for built output, then the subpath in its src and in the package
        """

        # Exports map subpaths, `"./*": "./dist/*.js"`, and imports map private names, `"#utils/*": "./src/utils/*.js"`,
        # to targets or conditions of them, `{"import": ..., "types": ...}`.
        exports = manifest.get("imports") if subpath.startswith("#") else manifest.get("exports")
        if subpath.startswith("#"):
            exports = exports if isinstance(exports, dict) else {}
        elif not (isinstance(exports, dict) and any(key.startswith(".") for key in exports)):
            exports = {".": exports}
        entries: list[str] = []
        for pattern, value in exports.items():
            prefix, star, suffix = pattern.partition("*")
            if subpath != pattern and not (star and subpath.startswith(prefix) and subpath.endswith(suffix)):
                continue
            pending = [value]
            while pending:
                value = pending.pop(0)
                if isinstance(value, str):
                    entries.append(value.replace("*", subpath[len(prefix):len(subpath) - len(suffix)]) if star else value)
                elif isinstance(value, dict):
                    pending += value.values()
                elif isinstance(value, list):
                    pending += value
        if subpath == ".":
            entries += [manifest[field] for field in ("source", "types", "typings", "module", "main") if isinstance(manifest.get(field), str)]

        stems: list[str] = []
        for entry in entries:
            parts = pathlib.PurePosixPath(os.path.normpath(re.sub(r"(\.d)?\.[cm]?[jt]sx?$", "", entry))).parts
            stems.append((directory / pathlib.PurePosixPath(*parts)).as_posix() if parts else directory.as_posix())
            # Built output, `dist/index.js` or `lib/esm/index.js`, is usually built from the same path in src.
            while parts and parts[0] in ("dist", "build", "lib", "out", "esm", "cjs", "types"):
                parts = parts[1:]
            stems.append((directory / "src" / pathlib.PurePosixPath(*parts)).as_posix() if parts else (directory / "src").as_posix())
        if subpath.startswith("#"):
            return stems
        return stems + [(directory / "src" / subpath).as_posix(), (directory / subpath).as_posix()]

    @classmethod
    def _set_meta(cls, db: sqlite3.Connection, **values):
        db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            [(key, json.dumps(value)) for key, value in values.items()],
        )
        db.commit()

client = GraphClient()

@mcp.tool()
def build_graph(
    path_to_analyze: str,
    path_to_storage: str,
    timeout_after_s: int = 300,
) -> dict[str, str | float | int | list[str]]:
    """
    Build or update a code graph of a source tree for subsequent analysis.

    Parses python, javascript, typescript, and go files and stores their
    symbols and the contains, calls (including rendering jsx components),
    inherits (including implemented interfaces), overrides, uses (type
    references), references (functions, classes, and constants used as
    values), and imports edges between them. Call before querying a source
    tree and again after it changes; only changed files are parsed again. If
    status is "partial", call again to continue: what was parsed is kept,
    and edges are resolved once every file is, until then being the last
    build's.

    The graph doesn't follow edits on its own: after changing files, call
    again before querying, or answers describe the code as it was. Rebuilds
    are cheap, so do it after each round of edits.

    Args:
        path_to_analyze: Directory of the source tree to analyze
        path_to_storage: Full path to the sqlite database file, one per
            source tree; its directory must exist
        timeout_after_s: Seconds to work before returning "partial"

    Returns:
        A dict whose "status" is "complete", "partial", "busy" (another
        build is running on path_to_storage), or "failed" (see "error"),
        with counts of files, symbols, and edges in the graph, the
        "kinds_of_symbols" accepted by search_symbols, and a "note" when
        calling again won't finish without a larger timeout_after_s
    """

    try:
        return client.build_database(path_to_analyze, path_to_storage, timeout_after_s)
    except Exception as ex:
        log.exception("! failed to build graph")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def graph_state(
    path_to_storage: str,
) -> dict[str, str | float | int | list[str]]:
    """
    Report the state of a code graph without changing it.

    Use to check whether a graph exists and is complete before querying it,
    or to follow a build running elsewhere.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph

    Returns:
        A dict whose "status" is "missing" (call build_graph), "building",
        "interrupted" or "partial" (call build_graph to finish), "complete",
        or "failed" (see "error"), with the analyzed "root", "started_at"
        and "finished_at" epoch seconds, counts of files, symbols, and
        edges as of the last finished build, and the "kinds_of_symbols"
        accepted by search_symbols
    """

    try:
        return client.load_status_of(path_to_storage)
    except Exception as ex:
        log.exception("! failed to load graph state")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def search_symbols(
    path_to_storage: str,
    kind: str,
    query: str = "",
    limit: int = 128,
) -> dict[str, str | list[dict]]:
    """
    Find symbols of a kind in a code graph by name, or where to start reading it.

    To start on an unfamiliar code base, search kind "file" with an empty
    query: it lists top-level files, which no other file imports, ordered
    by how many files their imports reach, so each project's entry points
    come first and test files last. An empty query with another kind
    lists that kind's most referenced symbols, the ones the code depends
    on most.

    A query matches names and qualified names ("Store.add"), and files by
    their path: exact matches first, those matching case before those
    ignoring it, then prefixes, then substrings, each most referenced first,
    then exported (usable from other files) before internal. For a name
    whose kind isn't known, search kind "any", which matches every kind
    and needs a query.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        kind: One of the "kinds_of_symbols" from build_graph or graph_state,
            or "any" with a query to search every kind
        query: Name, qualified name, or part of either or of a path; empty
            for top-level files or the most referenced symbols
        limit: Most results to return

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error"), and whose "results" have each symbol's "id", "kind",
        "qualified_name", "file_path", "start_line", "end_line",
        "signature", "doc", "references" count, and whether it is "exported"
        (usable from other files), or for top-level files their "id",
        "kind", "doc", "symbols" count, and "reach", each with whether it is
        "test" code
    """

    try:
        return client.find_symbols(path_to_storage, kind, query, limit)
    except Exception as ex:
        log.exception("! failed to search symbols")
        return {"status": "failed", "error": str(ex), "results": []}

@mcp.tool()
def get_file(
    path_to_storage: str,
    file_path: str,
    max_symbols: int = 256,
) -> dict:
    """
    Outline a file in a code graph: what it defines, imports, and is imported by.

    Use after search_symbols to see what a file holds before reading or
    editing it; signatures and docs often answer without the source.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        file_path: Path of the file relative to the analyzed root, as in a
            file symbol's "id"
        max_symbols: Most symbols in the outline, the most referenced
            first, counting references to their members

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar paths), with the file's
        "language", "doc", "lines", and whether it is "test" code, the
        files it "imports" and is
        "imported_by", "external_imports" of packages or files outside the
        graph, its "symbols" count, whether the outline is "truncated", and
        the "outline": symbols with "id", "kind", "name", "start_line",
        "end_line", "signature", "doc", "references" count, whether they
        are "exported" (usable from other files), and "members"
    """

    try:
        return client.describe_file(path_to_storage, file_path, max_symbols)
    except Exception as ex:
        log.exception("! failed to get file")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_symbols_at(
    path_to_storage: str,
    file_path: str,
    start_line: int,
    end_line: int | None = None,
    limit: int = 128,
) -> dict:
    """
    Find the symbols around lines of a file in a code graph, innermost first.

    Use to go from a location to the graph: the first result holds a stack
    trace's or an error's line, and every result spans some of a diff hunk's
    lines, given as start_line to end_line. Pass their ids to get_context or
    get_related. The file spans every line, so it comes last.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        file_path: Path of the file relative to the analyzed root, as in a
            file symbol's "id"
        start_line: First line, from 1
        end_line: Last line; start_line if not given
        limit: Most results to return

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar paths), with the
        "file_path", "start_line" and "end_line" looked up, whether the
        file is "test" code and "stale" (changed since the build, so lines
        may have moved), the "results": symbols with "id", "kind",
        "signature", "start_line", "end_line", and whether they are
        "exported", the "count" of all found, and whether results were
        "truncated"
    """

    try:
        return client.locate_symbols(path_to_storage, file_path, start_line, end_line, limit)
    except Exception as ex:
        log.exception("! failed to get symbols at lines")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_context(
    path_to_storage: str,
    symbol_id: str,
    include_source: bool = False,
    max_source_lines: int = 256,
    limit: int = 128,
) -> dict:
    """
    Describe a symbol in a code graph with everything directly related to it.

    Use before changing a symbol: its callers, subclasses, and the methods
    it's overridden_by are what a change can break, its callees and uses are
    what it depends on. Each related symbol's "confidence" is "exact"
    (resolved through definitions and imports), "inferred" (a method found
    through the type of what it's called on, e.g. `self.save()` or `s.add()`
    after `s = Store()`, an override such a call may run, or a go type
    having all of an interface's methods), or "guess" (matched by name only;
    check these).

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        symbol_id: A symbol's "id" from search_symbols or get_file, like
            "src/store.py::Store.add"
        include_source: Whether to include the symbol's source lines
        max_source_lines: Most source lines to include
        limit: Most entries in each list of related symbols

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar ids), with the symbol's
        "kind", "qualified_name", "file_path", "start_line", "end_line",
        "signature", and "doc", whether it is "exported" (usable from other
        files: python's names without a leading underscore or in `__all__`,
        ts's exports and members not private, go's capitalized names) and
        "test" code, whether its file is "stale" (changed since the build),
        its "parent" and "members", whether each is "exported",
        the "callers", "callees", "bases", "subclasses", "uses", "used_by",
        "references" (functions, classes, and constants it uses as values,
        e.g. a callback it passes), "referenced_by", "overrides" (the base or
        interface methods it overrides or implements), and "overridden_by"
        symbols with "id",
        "kind", "signature", "confidence",
        whether they are "test" code, and "lines" where they refer,
        "counts" of each list before the limit, and the "source" and
        whether it was "source_truncated" when included
    """

    try:
        return client.describe_symbol(path_to_storage, symbol_id, include_source, max_source_lines, limit)
    except Exception as ex:
        log.exception("! failed to get context")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_dependencies(
    path_to_storage: str,
    file_path: str,
    depth: int = 1,
    limit: int = 128,
) -> dict:
    """
    Find the files a file imports, directly or through other files, nearest first.

    Use to see what a file needs before moving, copying, or testing it:
    depth 1 is what it imports, depth 2 adds what those import, and so on.
    Imports of packages outside the graph are listed by get_file.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        file_path: Path of the file relative to the analyzed root, as in a
            file symbol's "id"
        depth: How many imports away to follow
        limit: Most results to return

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar ids), with the "results":
        files with "id", "kind", "signature", "depth" (steps away),
        "confidence" (the surest path's least sure edge), "via" (the files
        one step closer it connects through), and whether they are "test"
        code, the "count" of all reached, and whether results were
        "truncated"
    """

    try:
        return client.walk_edges(path_to_storage, file_path, ("IMPORTS",), True, depth, limit, False, False)
    except Exception as ex:
        log.exception("! failed to get dependencies")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_dependents(
    path_to_storage: str,
    file_path: str,
    depth: int = 1,
    limit: int = 128,
) -> dict:
    """
    Find the files that import a file, directly or through other files, nearest first.

    Use before changing a file's exports to see which files a change can
    reach: depth 1 is what imports it, depth 2 adds what imports those, and
    so on.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        file_path: Path of the file relative to the analyzed root, as in a
            file symbol's "id"
        depth: How many imports away to follow
        limit: Most results to return

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar ids), with the "results":
        files with "id", "kind", "signature", "depth" (steps away),
        "confidence" (the surest path's least sure edge), "via" (the files
        one step closer it connects through), and whether they are "test"
        code, the "count" of all reached, and whether results were
        "truncated"
    """

    try:
        return client.walk_edges(path_to_storage, file_path, ("IMPORTS",), False, depth, limit, False, False)
    except Exception as ex:
        log.exception("! failed to get dependents")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_related(
    path_to_storage: str,
    symbol_id: str,
    relationship: str,
    depth: int = 1,
    limit: int = 128,
    skip_guesses: bool = False,
) -> dict:
    """
    Follow one relationship from a symbol, directly or through others, nearest first.

    Use to go further than get_context's direct relationships:
    - "callers" shows what a change can break: depth 1 is what calls the
      symbol, depth 2 adds their callers, and so on. Calls of a class are
      its constructions, and of a jsx component its renders. Calls of a
      method through a base or interface count for its overrides too. A
      function passed as a value, e.g. a callback, is in "referenced_by"
      instead.
    - "callees" shows what a symbol depends on before reusing, moving, or
      testing it: depth 1 is what it calls, depth 2 adds what those call.
    - "subclasses" at depth 3 is a class's hierarchy below it, "bases" the
      one above it; an interface's subclasses include what implements it.
    - "overridden_by" shows the methods overriding or implementing a
      method, which change with its signature; "overrides" the methods it
      overrides or implements.
    - "used_by" shows what refers to a type, "uses" the types it refers to.
    - "referenced_by" shows what uses a function, class, or constant as a
      value, e.g. `items.map(format)` or `onClick={save}`; "references" the
      values a symbol uses.
    - "members" at depth 2 includes members of members.
    - "tests" shows the tests that reach a symbol: its callers and those
      referencing it, at any depth up to the given one, that are test code,
      e.g. test functions, or test files whose test callbacks call it.
    Each result's "confidence" is its surest path's least sure edge:
    "exact" (resolved through definitions and imports), "inferred" (a
    method found through the type of what it's called on, e.g. `self.save()`
    or `s.add()` after `s = Store()`, an override such a call may run, or a
    go type having all of an interface's methods), or "guess" (matched by
    name only).
    Guesses multiply with depth; use skip_guesses to follow only the others.

    Args:
        path_to_storage: Full path to the sqlite database file given to
            build_graph
        symbol_id: A symbol's "id" from search_symbols, get_file, or
            get_context, like "src/store.py::Store"
        relationship: One of "callers", "callees", "bases", "subclasses",
            "uses", "used_by", "references", "referenced_by", "overrides",
            "overridden_by", "members", or "tests"
        depth: How many steps away to follow
        limit: Most results to return
        skip_guesses: Whether to skip edges matched by name only

    Returns:
        A dict whose "status" is the graph's from graph_state, or "failed"
        (see "error", with "matches" of similar ids), with the "results":
        symbols with "id", "kind", "signature", "depth" (steps away),
        "confidence", "via" (the symbols one step closer it connects
        through), and whether they are "test" code, the "count" of all
        reached, and whether results were "truncated"
    """

    # Each relationship as (edge kinds, whether it follows edges from the symbol).
    relationships = {
        "callers": (("CALLS",), False),
        "callees": (("CALLS",), True),
        "bases": (("INHERITS",), True),
        "subclasses": (("INHERITS",), False),
        "uses": (("USES",), True),
        "used_by": (("USES",), False),
        "references": (("REFERENCES",), True),
        "referenced_by": (("REFERENCES",), False),
        "overrides": (("OVERRIDES",), True),
        "overridden_by": (("OVERRIDES",), False),
        "members": (("CONTAINS",), True),
        "tests": (("CALLS", "REFERENCES"), False),
    }
    if relationship not in relationships:
        return {"status": "failed", "error": f"unknown relationship {relationship!r}, expected one of {list(relationships)}"}
    try:
        return client.walk_edges(path_to_storage, symbol_id, *relationships[relationship], depth, limit, skip_guesses, relationship == "tests")
    except Exception as ex:
        log.exception("! failed to get related")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def get_source(
    path_to_analyze: str,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_lines: int = 256,
) -> dict:
    """
    Read lines of a file in an analyzed source tree as it is now.

    Use for lines around a symbol or anything outside one; get_context
    includes a symbol's own source.

    Args:
        path_to_analyze: Directory of the source tree given to build_graph
        file_path: Path of the file relative to that directory
        start_line: First line to read, from 1; the start of the file if
            not given
        end_line: Last line to read; max_lines from start_line if not given
        max_lines: Most lines to read

    Returns:
        A dict whose "status" is "ok" or "failed" (see "error"), with the
        "file_path", the "start_line" and "end_line" read, the file's
        "lines" count, the "source", and whether it was "truncated" by
        max_lines
    """

    try:
        return client.read_source(path_to_analyze, file_path, start_line, end_line, max_lines)
    except Exception as ex:
        log.exception("! failed to get source")
        return {"status": "failed", "error": str(ex)}

@mcp.tool()
def ping() -> str:
    """
    Health check. Returns "ok" when the service is up; "down" when down.
    """

    return "ok"

def add_to_registry(name: str, role: str, endpoint: str, url: str, port: int):
    deadline = time.monotonic() + 30

    while time.monotonic() < deadline:
        try:
            requests.get(url=f"http://localhost:{port}", timeout=2)
            break
        except requests.exceptions.RequestException:
            time.sleep(1)
    else:
        log.warning("~ gave up waiting, continuing with registering")

    time.sleep(1)

    try:
        requests.post(
            url=url,
            json={
                "name": name,
                "role": role,
                "endpoint": endpoint,
            },
            timeout=15,
        )
    except IOError:
        log.exception("! failed to add to registry")

if __name__ == "__main__":
    print(textwrap.dedent(r"""
    .                                           /                _                                      
    .           _/_                      o     /                //                                    / 
    .  __,  , , /  __ _ _ _   __,  _ _  ,  _, /__,  _ _   __,  // __  , __, _     _,  _   __,   ,_   /_ 
    . (_/(_(_/_(__(_)/ / / /_(_/(_/ / /_(_(__/(_/(_/ / /_(_/(_(/_/ (_/_/_/_(/_---(_)_/ (_(_/(__/|_)_/ /_
    .                                                               /  (/         /|           /|       
    .                                                              '             (/           (/
    . 
    . powered by automanic 🍣
    . """))

    if not registry_url:
        log.error("! registry url not configured, exiting")
        sys.exit(1)

    if not endpoint_url:
        log.error("! endpoint url not configured, exiting")
        sys.exit(1)

    log.info(f". supporting semantic analysis as {service_name}")

    log.info(". available tools:")
    for tool in mcp._tool_manager.list_tools():
        log.info(f". {tool.name}")
    tooling = threading.Thread(
        target=lambda: add_to_registry(
            name=service_name,
            role=service_role,
            endpoint=endpoint_url,
            url=registry_url,
            port=service_port,
        ),
        name="service_tooling",
        daemon=True
    )
    tooling.start()
    try:
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=service_port,
            stateless_http=True,
            json_response=True,
        )
    finally:
        log.info(". shutting down 👋")
        tooling.join(timeout=10)
