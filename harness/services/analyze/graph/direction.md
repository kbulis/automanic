# Analyze Graph: Agent Policy

How to understand and navigate a code base with the analyze graph tools,
instead of listing directories and reading whole files through a shell.

Tools are registered as `<service name>-<tool>`, e.g.
`analyze-graph-search_symbols`; below they're named by tool alone.

## Why

Listing a tree (`ls -la`, `find`, `tree`) and reading files (`cat`, `head`,
`grep -rn`) spends tokens on everything to find the little that matters.
The graph has already parsed the tree: it ranks files and symbols by how
much the code depends on them, and answers with signatures, docs, and line
ranges. Ask it narrow questions, and read source only for the lines an
answer points to.

## Rules

1. For python, javascript, typescript (jsx, tsx), and go, use the graph to
   find, outline, and relate code. Don't list, `cat`, or `grep` those
   files through the shell.
2. Read signatures and docs before source. Read source by symbol
   (`get_context` with `include_source`) or by line range (`get_source`),
   never a whole file to find something in it.
3. Start with small limits (10 to 30) and raise them only if what's needed
   was cut off ("truncated" is true).
4. Rebuild the graph after editing files, before querying again. Rebuilds
   only parse what changed.
5. Treat "guess" confidence as unverified: confirm it with `get_context`
   or a short `get_source` before relying on it. Prefer `skip_guesses`
   when following relationships more than one step.
6. Use the shell for what the graph doesn't cover: files of other languages
   and formats (configs, yaml, markdown, Dockerfiles, sql), running code and
   tests, git, and creating directories.

## Setup: build or check the graph

Keep one graph per source tree, stored outside it in the shared working
folder, e.g. `/working/.graphs/<tree>.db`, for a tree at `/working/<tree>`.
Paths are as the analyze service sees them.

1. `graph_state(path_to_storage)`.
2. If "missing", "partial", or "interrupted", or the tree has changed since
   "finished_at", call `build_graph(path_to_analyze, path_to_storage)`. If
   its directory doesn't exist, create it with the shell first.
3. While `build_graph` returns "partial", call it again; if it returns a
   "note", call again with a larger `timeout_after_s`. If "busy", wait and
   check `graph_state`.
4. Keep the "kinds_of_symbols" it returns; they're the kinds
   `search_symbols` accepts.

## Use case: understand an unfamiliar code base

Go from the top down: entry points, then the core symbols, then how they
connect.

1. **Find entry points.**
   `search_symbols(kind="file", query="", limit=20)` lists top-level files,
   which nothing imports, ordered by how much of the code their imports
   reach. The first non-test results are each project's entry points (a
   monorepo has several). Skip results where "test" is true.
2. **Find the core abstractions.**
   `search_symbols(kind=<a kind such as "class">, query="", limit=20)` lists
   that kind's most referenced symbols, the ones the code depends on most.
   Repeat for the few kinds that matter, e.g. classes, interfaces, then
   functions.
3. **Outline the entry point.**
   `get_file(file_path)` on the top entry file shows what it defines,
   imports, and is imported by, with signatures and docs. This usually
   tells what the program does without reading it.
4. **Trace the flow from the entry point.**
   `get_related(symbol_id, "callees", depth=2, skip_guesses=true)` on the
   entry function (e.g. `main`, an app factory, a request handler) shows
   what runs from it. Outline the files those land in with `get_file`.
5. **Interrogate the key symbols.**
   `get_context(symbol_id)` on each core symbol shows its callers, callees,
   bases, subclasses, and the types it uses. Add `include_source=true` only
   when the signature and doc don't answer the question.
6. **Summarize** the architecture from what was found: entry points, core
   abstractions, and how a request or command flows between them, citing
   `file_path:start_line`.

## Use case: find where something is

- By name: `search_symbols(kind="any", query="<name>")`. Exact matches come
  first, then prefixes, then substrings; qualified names like
  `Store.add` work too.
- By file name or path fragment: `search_symbols(kind="file",
  query="<fragment>")`.
- Then `get_file` or `get_context` on the result, not `get_source` on the
  whole file.

## Use case: from an error or a diff to the code

- Stack trace or error line: `get_symbols_at(file_path, start_line)`; the
  first result is the innermost symbol holding the line. Then
  `get_context(symbol_id, include_source=true)`.
- Diff hunk: `get_symbols_at(file_path, start_line, end_line)` lists every
  symbol the hunk touches.
- If "stale" is true the file changed since the build: rebuild first.

## Use case: before changing code

1. `get_context(symbol_id)`: its "callers", "subclasses", and
   "overridden_by" are what a change can break.
2. For further reach, `get_related(symbol_id, "callers", depth=2)`, and
   `"referenced_by"` for functions passed as values (callbacks, handlers).
3. For a changed signature of a method, `get_related(symbol_id,
   "overridden_by")`.
4. For a file's exports, `get_dependents(file_path, depth=2)`.
5. Find the tests to run: `get_related(symbol_id, "tests", depth=3)`.
6. After editing, `build_graph` again, then run those tests.

## Use case: before moving, reusing, or testing code

- `get_related(symbol_id, "callees", depth=2)` shows what it depends on.
- `get_dependencies(file_path, depth=2)` shows the files it needs;
  `get_file` lists its "external_imports" of packages.

## Shell habits and what to use instead

- `ls -la`, `find . -name`, `tree` to get oriented:
  `search_symbols(kind="file", query="")`.
- `find` or `ls` for a file by name: `search_symbols(kind="file",
  query="<fragment>")`.
- `cat file` to see what it holds: `get_file(file_path)`.
- `cat file` or `sed -n` to read a function:
  `get_context(symbol_id, include_source=true)`.
- `sed -n 'a,bp'` for other lines: `get_source(file_path, start_line,
  end_line)`.
- `grep -rn "def name"` or `grep -rn "class Name"`:
  `search_symbols(kind="any", query="Name")`.
- `grep -rn "name("` for callers: `get_context` or
  `get_related(..., "callers")`.
- `grep -rn "import x"` for importers: `get_dependents(file_path)`.
- `grep` for tests of a function: `get_related(..., "tests")`.

## When results fail or look wrong

- "failed" with "matches": the path or id was close; retry with a match.
- An empty or thin graph: check `path_to_analyze` points at the tree's root
  as the analyze service sees it, and that the language is supported.
- A symbol the graph doesn't know (generated code, dynamic dispatch,
  unsupported language): fall back to `get_source` on a narrow range, or a
  scoped shell `grep` limited to a directory and file type, as a last
  resort.
