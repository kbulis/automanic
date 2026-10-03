import sqlite3
import pathlib
import textwrap

import service


def build(tmp_path: pathlib.Path, files: dict[str, str]) -> sqlite3.Connection:
    root = tmp_path / "src"
    for rel_path, text in files.items():
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())
    storage = tmp_path / "graph.sqlite"
    result = service.client.build_database(str(root), str(storage), 30)
    assert result["status"] == "complete", result
    return sqlite3.connect(storage)


def edges(db: sqlite3.Connection, kind: str) -> set[tuple[str, str]]:
    return set(db.execute("SELECT src_id, dst_id FROM edges WHERE kind = ?", (kind,)))


def symbols(db: sqlite3.Connection) -> dict[str, tuple[str, str | None, str | None]]:
    return {i: (k, s, d) for i, k, s, d in db.execute("SELECT id, kind, signature, doc FROM symbols")}


# 1. Type references.

def test_python_annotations_use_types(tmp_path):
    db = build(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/models.py": """
            from enum import Enum
            from dataclasses import dataclass
            class User: ...
            class Role(Enum):
                ADMIN = 1
            type UserId = int
            @dataclass
            class Session:
                user: User
                roles: list[Role]
                def renew(self) -> Session: ...
        """,
        "pkg/service.py": """
            from . import models as m
            from .models import User, UserId
            def load(uid: UserId, cache: dict[str, m.Session] | None = None) -> User:
                return User()
            DEFAULT: m.Role = m.Role.ADMIN
        """,
    })
    uses = edges(db, "USES")
    assert ("pkg/models.py::Session.user", "pkg/models.py::User") in uses
    assert ("pkg/models.py::Session.roles", "pkg/models.py::Role") in uses
    assert ("pkg/service.py::load", "pkg/models.py::UserId") in uses
    assert ("pkg/service.py::load", "pkg/models.py::Session") in uses
    assert ("pkg/service.py::load", "pkg/models.py::User") in uses
    assert ("pkg/service.py::DEFAULT", "pkg/models.py::Role") in uses
    # A method using its own class isn't a dependency.
    assert ("pkg/models.py::Session.renew", "pkg/models.py::Session") not in uses


def test_typescript_annotations_use_types_through_index(tmp_path):
    db = build(tmp_path, {
        "models/user.ts": """
            export interface User { id: number }
            export type Role = "admin" | "user";
            export enum Level { Low, High }
        """,
        "models/index.ts": 'export * from "./user";\n',
        "dup.py": "class User: ...\n",  # same name elsewhere, so resolving needs the index
        "api.ts": """
            import { User } from "./models";
            import * as models from "./models";
            export interface Props { user: User; level: models.Level }
            export function load(role: models.Role): Promise<User | null> { return null; }
        """,
    })
    uses = edges(db, "USES")
    assert ("api.ts::Props", "models/user.ts::User") in uses
    assert ("api.ts::Props", "models/user.ts::Level") in uses
    assert ("api.ts::load", "models/user.ts::Role") in uses
    assert ("api.ts::load", "models/user.ts::User") in uses
    assert not any(dst == "dup.py::User" for _, dst in uses)


# 2. Module graph.

def test_reexports_and_dynamic_imports(tmp_path):
    db = build(tmp_path, {
        "models/user.ts": "export interface User {}\n",
        "models/role.ts": "export type Role = string;\n",
        "models/index.ts": """
            export { User } from "./user";
            export * from "./role";
        """,
        "lazy.ts": "export const x = 1;\n",
        "app.ts": """
            import { User } from "./models";
            async function load() { return await import("./lazy"); }
        """,
        "legacy.js": """
            const lazy = require("./lazy");
            const pkg = require("lodash");
        """,
        "pkg/__init__.py": "",
        "pkg/plugin.py": "",
        "pkg/loader.py": """
            import importlib
            def load(name):
                importlib.import_module("pkg.plugin")
                __import__(f"pkg.{name}")
        """,
    })
    imports = edges(db, "IMPORTS")
    assert ("models/index.ts", "models/user.ts") in imports
    assert ("models/index.ts", "models/role.ts") in imports
    assert ("app.ts", "models/index.ts") in imports
    assert ("app.ts", "lazy.ts") in imports
    assert ("legacy.js", "lazy.ts") in imports
    assert ("pkg/loader.py", "pkg/plugin.py") in imports
    # Packages outside the tree and f-strings have no edge.
    assert {dst for src, dst in imports if src in ("legacy.js", "pkg/loader.py")} == {"lazy.ts", "pkg/plugin.py"}


def test_commonjs_exports_and_requires_resolve(tmp_path):
    db = build(tmp_path, {
        "store.js": """
            class Store { add() {} }
            module.exports = Store;
        """,
        "api.js": """
            function load() {}
            function _hidden() {}
            module.exports = { load, save() {}, };
        """,
        "util.js": """
            exports.slug = function () {};
            module.exports.trim = (s) => s;
            function pad() {}
            exports.pad = pad;
            function inner() {}
        """,
        "factory.js": "module.exports = function () {};\n",
        ".eslintrc.js": "module.exports = { root: true, env: { node: true } };\n",
        "index.js": "module.exports = require('./util');\n",
        "app.js": """
            const Store = require("./store");
            const api = require("./api");
            const { slug, trim: clean } = require("./util");
            const pad = require("./util").pad;
            const { save } = require("./api");
            const make = require("./factory");
            const lib = require(".");
            function main() {
              const s = new Store();
              api.load(); api.save(); save();
              slug(); clean(); pad(); make();
              lib.trim();
            }
        """,
    })
    assert edges_from(db, "app.js::main", "CALLS") == {
        ("store.js::Store", "exact"),
        ("api.js::load", "exact"),
        ("api.js::api.save", "exact"),
        ("util.js::slug", "exact"),
        ("util.js::trim", "exact"),
        ("util.js::pad", "exact"),
        ("factory.js::factory", "exact"),
    }
    # What a file assigns `module.exports`, or as one of its names, is exported.
    exported = {i for (i,) in db.execute("SELECT id FROM symbols WHERE exported")}
    assert {"store.js::Store", "api.js::load", "api.js::api", "api.js::api.save", "util.js::slug", "util.js::trim",
            "util.js::pad", "factory.js::factory"} <= exported
    assert not {"api.js::_hidden", "util.js::inner"} & exported
    # Hidden files' exports are named without their dot, which would read as qualifying them.
    assert {".eslintrc.js::eslintrc", ".eslintrc.js::eslintrc.env"} <= exported


def test_commonjs_renamed_and_compiled_exports(tmp_path):
    db = build(tmp_path, {
        # As typescript compiles to commonjs.
        "lib/store.js": """
            "use strict";
            Object.defineProperty(exports, "__esModule", { value: true });
            exports.make = exports.Store = void 0;
            class Store {
                add() { }
            }
            exports.Store = Store;
            function make() { return new Store(); }
            exports.make = make;
            exports.default = Store;
        """,
        "lib/index.js": """
            "use strict";
            Object.defineProperty(exports, "__esModule", { value: true });
            exports.build = void 0;
            const store_1 = require("./store");
            Object.defineProperty(exports, "build", { enumerable: true, get: function () { return store_1.make; } });
            __exportStar(require("./extra"), exports);
        """,
        "lib/extra.js": "function extra() { }\nexports.extra = extra;\n",
        # Names exported for others.
        "renamed.js": "function inner() {}\nexports.publicName = inner;\n",
        "object.js": "function run() {}\nfunction hidden() {}\nmodule.exports = { start: run };\n",
        "app.js": """
            const store_1 = __importDefault(require("./lib/store"));
            const lib_1 = require("./lib");
            const { publicName } = require("./renamed");
            const { start } = require("./object");
            function main() {
                new store_1.default();
                (0, lib_1.build)();
                (0, lib_1.extra)();
                publicName();
                start();
            }
        """,
    })
    assert edges_from(db, "app.js::main", "CALLS") == {
        ("lib/store.js::Store", "exact"),   # the default
        ("lib/store.js::make", "exact"),    # exported by a getter, as `build`
        ("lib/extra.js::extra", "exact"),   # exported with all of its module's
        ("renamed.js::inner", "exact"),
        ("object.js::run", "exact"),
    }
    found = symbols(db)
    assert "app.js::store_1" not in found and "app.js::lib_1" not in found
    exported = {i for (i,) in db.execute("SELECT id FROM symbols WHERE exported")}
    assert {"lib/store.js::Store", "lib/store.js::make", "renamed.js::inner", "object.js::run"} <= exported
    assert "object.js::hidden" not in exported


def test_bundler_aliases_imports_maps_and_package_extends(tmp_path):
    db = build(tmp_path, {
        # Vite's object form, from the config's directory in pieces.
        "web/vite.config.ts": """
            import path from "path";
            export default defineConfig({
              resolve: { alias: { "@": path.resolve(__dirname, "src"), "~lib": "/lib" } },
            });
        """,
        "web/src/util.ts": "export function util() {}\n",
        "web/lib/index.ts": "export function lib() {}\n",
        "web/src/app.ts": """
            import { util } from "@/util";
            import { lib } from "~lib";
            export function main() { util(); lib(); }
        """,
        # Vite's array form and webpack's exact `$`.
        "admin/webpack.config.js": """
            module.exports = {
              resolve: { alias: { shared$: path.resolve(__dirname, "shared/index.js") } },
            };
        """,
        "admin/vitest.config.mjs": """
            export default { resolve: { alias: [{ find: "#root", replacement: new URL("./src", import.meta.url).pathname }] } };
        """,
        "admin/shared/index.js": "export function share() {}\n",
        "admin/src/x.js": "export function x() {}\n",
        "admin/src/page.js": """
            import { share } from "shared";
            import { x } from "#root/x";
            export function page() { share(); x(); }
        """,
        # An imports map, and a tsconfig extending one from a package.
        "api/package.json": '{"name": "api", "imports": {"#db": "./src/db.js", "#utils/*": "./dist/utils/*.js"}}',
        "api/tsconfig.json": '{"extends": "@acme/tsconfig/base.json"}',
        "api/node_modules/@acme/tsconfig/base.json": '{"compilerOptions": {"baseUrl": "../../../src"}}',
        "api/src/db.ts": "export function connect() {}\n",
        "api/src/utils/dates.ts": "export function today() {}\n",
        "api/src/models.ts": "export function model() {}\n",
        "api/src/server.ts": """
            import { connect } from "#db";
            import { today } from "#utils/dates";
            import { model } from "models";
            export function start() { connect(); today(); model(); }
        """,
    })
    assert edges_from(db, "web/src/app.ts::main", "CALLS") == {("web/src/util.ts::util", "exact"), ("web/lib/index.ts::lib", "exact")}
    # Every config in the nearest directory with any.
    assert edges_from(db, "admin/src/page.js::page", "CALLS") == {("admin/shared/index.js::share", "exact"), ("admin/src/x.js::x", "exact")}
    assert edges_from(db, "api/src/server.ts::start", "CALLS") == {
        ("api/src/db.ts::connect", "exact"), ("api/src/utils/dates.ts::today", "exact"), ("api/src/models.ts::model", "exact"),
    }


def test_ts_workspace_packages_resolve(tmp_path):
    db = build(tmp_path, {
        "package.json": '{"name": "acme", "workspaces": ["packages/*", "apps/*"]}',
        "packages/ui/package.json": '{"name": "@acme/ui", "main": "./dist/index.js", "types": "./dist/index.d.ts"}',
        "packages/ui/src/index.ts": "export function render() {}\n",
        "packages/ui/src/button.ts": "export function Button() {}\n",
        "packages/core/package.json": """
            {"name": "@acme/core", "exports": {
                ".": {"types": "./lib/main.d.ts", "import": "./lib/main.js"},
                "./features/*": "./src/features/*.ts"
            }}
        """,
        "packages/core/src/main.ts": "export function start() {}\n",
        "packages/core/src/features/auth.ts": "export function login() {}\n",
        "apps/web/package.json": '{"name": "web", "dependencies": {"@acme/ui": "workspace:*", "react": "18"}}',
        "apps/web/src/app.ts": """
            import { render } from "@acme/ui";
            import { Button } from "@acme/ui/button";
            import { start } from "@acme/core";
            import { login } from "@acme/core/features/auth";
            import React from "react";
            export function main() {
              render(); Button(); start(); login();
            }
        """,
    })
    assert {dst for src, dst in edges(db, "IMPORTS") if src == "apps/web/src/app.ts"} == {
        "packages/ui/src/index.ts", "packages/ui/src/button.ts", "packages/core/src/main.ts", "packages/core/src/features/auth.ts",
    }
    assert edges_from(db, "apps/web/src/app.ts::main", "CALLS") == {
        ("packages/ui/src/index.ts::render", "exact"),
        ("packages/ui/src/button.ts::Button", "exact"),
        ("packages/core/src/main.ts::start", "exact"),
        ("packages/core/src/features/auth.ts::login", "exact"),
    }


def test_python_project_roots_resolve(tmp_path):
    db = build(tmp_path, {
        "services/api/app/__init__.py": "",
        "services/api/app/models.py": "class User: ...\n",
        "services/api/app/subprocess.py": "",
        "services/api/app/main.py": """
            import subprocess
            from app.models import User
            def handler(): User()
        """,
        "services/api/tests/test_models.py": """
            from app import models
            def test_user(): models.User()
        """,
        "libs/text/pyproject.toml": "[project]\nname = 'text'\n",
        "libs/text/src/text/__init__.py": "",
        "libs/text/src/text/clean.py": "def strip(s): ...\n",
        "libs/text/tests/test_clean.py": """
            from text.clean import strip
            def test_strip(): strip("x")
        """,
    })
    assert edges_from(db, "services/api/app/main.py::handler", "CALLS") == {("services/api/app/models.py::User", "exact")}
    # A package's own directory isn't searched: `import subprocess` in it is python's.
    assert {dst for src, dst in edges(db, "IMPORTS") if src == "services/api/app/main.py"} == {"services/api/app/models.py"}
    assert edges_from(db, "services/api/tests/test_models.py::test_user", "CALLS") == {
        ("services/api/app/models.py::User", "exact"),
    }
    assert edges_from(db, "libs/text/tests/test_clean.py::test_strip", "CALLS") == {("libs/text/src/text/clean.py::strip", "exact")}


def test_python_distributions_resolve_from_anywhere(tmp_path):
    db = build(tmp_path, {
        # src layout, found by default; its tests aren't shipped.
        "libs/text/pyproject.toml": "[project]\nname = 'text'\n",
        "libs/text/src/text/__init__.py": "",
        "libs/text/src/text/clean.py": "def strip(s): ...\n",
        "libs/text/tests/__init__.py": "",
        "libs/text/tests/helpers.py": "def fixture(): ...\n",
        # Renamed by setuptools' package-dir.
        "libs/acme/pyproject.toml": "[tool.setuptools.package-dir]\nacme = 'source/acme_impl'\n",
        "libs/acme/source/acme_impl/__init__.py": "",
        "libs/acme/source/acme_impl/core.py": "def run(): ...\n",
        # Poetry's include from a directory.
        "libs/poet/pyproject.toml": "[tool.poetry]\nname = 'poet'\npackages = [{ include = 'poet', from = 'lib' }]\n",
        "libs/poet/lib/poet/__init__.py": "def verse(): ...\n",
        # setup.cfg's package_dir, and setup.py's find_packages, with modules too.
        "libs/cfg/setup.cfg": "[options]\npackage_dir =\n    =code\n",
        "libs/cfg/code/cfgpkg/__init__.py": "def load(): ...\n",
        "libs/old/setup.py": "from setuptools import setup, find_packages\nsetup(packages=find_packages('lib'))\n",
        "libs/old/lib/legacy.py": "def ancient(): ...\n",
        # A setup.py in a package is a module of it, not a project's packaging.
        "tools/runner/__init__.py": "",
        "tools/runner/setup.py": "def configure(): ...\n",
        "tools/runner/jobs/__init__.py": "def queue(): ...\n",
        "services/api/app/__init__.py": "",
        "services/api/app/main.py": """
            from text.clean import strip
            from acme.core import run
            from poet import verse
            from cfgpkg import load
            from legacy import ancient
            from tests.helpers import fixture
            from jobs import queue
            mod = __import__("os")
            def handler():
                strip(""); run(); verse(); load(); ancient(); fixture(); queue()
        """,
    })
    assert edges_from(db, "services/api/app/main.py::handler", "CALLS") == {
        ("libs/text/src/text/clean.py::strip", "exact"),
        ("libs/acme/source/acme_impl/core.py::run", "exact"),
        ("libs/poet/lib/poet/__init__.py::verse", "exact"),
        ("libs/cfg/code/cfgpkg/__init__.py::load", "exact"),
        ("libs/old/lib/legacy.py::ancient", "exact"),
    }
    # `mod = __import__("os")` binds a module, so isn't a definition.
    assert "services/api/app/main.py::mod" not in symbols(db)


# 3. Signatures and docs.

def test_python_signatures_and_docs(tmp_path):
    db = build(tmp_path, {
        "mod.py": '''
            """Session handling for the API."""
            import functools

            # Retries before giving up.
            MAX_RETRIES: int = 3

            # Comment loses to the docstring.
            @functools.cache
            def refresh(token: str,
                        *, force: bool = False) -> "Session":
                """Refresh the token.

                More detail."""

            class Session(Base, metaclass=Meta):
                r\'\'\'Raw docstring here.\'\'\'
                user: User  # trailing comment
                handler = lambda self, x: x
        ''',
    })
    found = symbols(db)
    assert found["mod.py"] == ("file", None, "Session handling for the API.")
    assert found["mod.py::MAX_RETRIES"] == ("variable", "MAX_RETRIES: int = 3", "Retries before giving up.")
    assert found["mod.py::refresh"] == (
        "function", 'def refresh(token: str, *, force: bool = False) -> "Session"', "Refresh the token.",
    )
    assert found["mod.py::Session"] == ("class", "class Session(Base, metaclass=Meta)", "Raw docstring here.")
    # A comment trailing the line above isn't documentation.
    assert found["mod.py::Session.handler"] == ("method", "handler = lambda self, x", None)


def test_typescript_signatures_and_docs(tmp_path):
    db = build(tmp_path, {
        "ui.tsx": """
            /** Base URL for the API. */
            export const API_URL = "https://example";

            /**
             * Load a user.
             * @param id user id
             */
            export async function loadUser(id: number): Promise<User | null> {
              return null;
            }

            // Renders a button.
            export const Button = ({ label }: Props): JSX.Element => {
              return null;
            };

            /** Store of items. */
            @Injectable()
            export class Store<T> extends Base implements Repo {
              /** Add an item. */
              @Track() add(item: T): void {}
            }

            export type Id = string | number;
        """,
    })
    found = symbols(db)
    assert found["ui.tsx::API_URL"] == ("variable", 'API_URL = "https://example"', "Base URL for the API.")
    assert found["ui.tsx::loadUser"] == (
        "function", "async function loadUser(id: number): Promise<User | null>", "Load a user.",
    )
    assert found["ui.tsx::Button"] == ("function", "Button = ({ label }: Props): JSX.Element", "Renders a button.")
    assert found["ui.tsx::Store"] == ("class", "class Store<T> extends Base implements Repo", "Store of items.")
    assert found["ui.tsx::Store.add"] == ("method", "add(item: T): void", "Add an item.")
    assert found["ui.tsx::Id"] == ("type", "type Id = string | number", None)


def test_file_docs_of_ts_js_and_go(tmp_path):
    db = build(tmp_path, {
        "serve.js": """
            #!/usr/bin/env node
            'use strict';
            // Copyright 2024 Acme. All rights reserved.

            /* eslint-disable no-console */

            /**
             * Serves files from a directory.
             */

            /** Starts serving. */
            function serve() {}
        """,
        "env.d.ts": '/// <reference types="vite/client" />\n',
        "attached.ts": """
            /** Loads the config. */
            export function load() {}
        """,
        "overview.ts": """
            /**
             * @fileoverview Shared helpers for dates.
             */
            export function today() {}
        """,
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            // Copyright 2024 Acme.

            // Package store holds items.
            package store
        """,
        "store/other.go": "package store\n",
    })
    found = symbols(db)
    assert found["serve.js"][2] == "Serves files from a directory."
    assert found["serve.js::serve"][2] == "Starts serving."
    # A comment right above the first code documents that code, unless it says it's the file's.
    assert found["attached.ts"][2] is None and found["attached.ts::load"][2] == "Loads the config."
    assert found["overview.ts"][2] == "Shared helpers for dates."
    assert found["env.d.ts"][2] is None
    assert found["store/store.go"][2] == "Package store holds items."
    assert found["store/other.go"][2] is None


# 4. Decorators.

def test_python_decorators_call_from_decorated(tmp_path):
    db = build(tmp_path, {
        "lib.py": """
            def cache(f): return f
            def route(path): return lambda f: f
        """,
        "views.py": """
            import functools
            from .lib import cache, route
            @cache
            def a(): ...
            @route("/b")
            @functools.lru_cache(maxsize=1)
            def b(): ...
            class Point:
                @cache
                def c(self): ...
        """,
    })
    calls = edges(db, "CALLS")
    assert ("views.py::a", "lib.py::cache") in calls
    assert ("views.py::b", "lib.py::route") in calls
    assert ("views.py::Point.c", "lib.py::cache") in calls
    # Not credited to the file or class around them.
    assert not any(src in ("views.py", "views.py::Point") for src, _ in calls)


def test_typescript_decorators_stay_on_their_member(tmp_path):
    db = build(tmp_path, {
        "ng.ts": """
            export function Component(o: object) { return (c: any) => c; }
            export function Input() { return (t: any, k: string) => {}; }
            export function HostListener(e: string) { return (t: any, k: string) => {}; }
            @Component({ selector: "x" })
            export class Widget {
              @Input() name: string;
              @HostListener("click") onClick() {}
              plain() {}
              @Input() @HostListener("x") both() {}
            }
        """,
    })
    calls = {(src, dst) for src, dst in edges(db, "CALLS") if src.startswith("ng.ts::Widget")}
    assert calls == {
        ("ng.ts::Widget", "ng.ts::Component"),
        ("ng.ts::Widget.name", "ng.ts::Input"),
        ("ng.ts::Widget.onClick", "ng.ts::HostListener"),
        ("ng.ts::Widget.both", "ng.ts::Input"),
        ("ng.ts::Widget.both", "ng.ts::HostListener"),
    }


def test_decorator_arguments_are_the_decorated_symbols(tmp_path):
    db = build(tmp_path, {
        "cases.py": """
            import pytest
            CASES = [1, 2]
            def check(value): ...
            def ids(value): ...
            @pytest.mark.parametrize("case", CASES, ids=ids)
            def test_cases(case): ...
            class Model:
                @validator(check)
                def name(self, check): ...
        """,
        "users.module.ts": """
            import { Module } from "@nestjs/common";
            export class UsersController {}
            export class UsersService {}
            export class Dto {}
            export function make() { return 1; }
            @Module({ controllers: [UsersController], providers: [UsersService], imports: [make()] })
            export class UsersModule {
              @Type(() => Dto)
              item: Dto;
            }
        """,
    })
    assert edges_from(db, "cases.py::test_cases", "REFERENCES") == {("cases.py::CASES", "exact"), ("cases.py::ids", "exact")}
    # In the scope around the definition, not its own, whose parameter `check` is another.
    assert edges_from(db, "cases.py::Model.name", "REFERENCES") == {("cases.py::check", "exact")}
    assert edges_from(db, "users.module.ts::UsersModule", "REFERENCES") == {
        ("users.module.ts::UsersController", "exact"), ("users.module.ts::UsersService", "exact"),
    }
    assert ("users.module.ts::make", "exact") in edges_from(db, "users.module.ts::UsersModule", "CALLS")
    assert edges_from(db, "users.module.ts::UsersModule.item", "REFERENCES") == {("users.module.ts::Dto", "exact")}


# 5. Default exports and object namespaces.

def test_default_exports_named_after_file(tmp_path):
    db = build(tmp_path, {
        "components/Base.ts": "export default class Base {}\n",
        "components/Button.tsx": """
            import Base from "./Base";
            /** A button. */
            export default class extends Base {
              click() {}
            }
        """,
        "components/Card/index.tsx": "export default (props: Props) => null;\n",
        "routes/home.js": "export default function () {}\n",
        "routes/value.js": "const x = 1;\nexport default x;\n",
    })
    found = symbols(db)
    assert found["components/Button.tsx::Button"] == ("class", "class extends Base", "A button.")
    assert found["components/Button.tsx::Button.click"][0] == "method"
    assert found["components/Card/index.tsx::Card"][0] == "function"
    assert found["routes/home.js::home"][0] == "function"
    # Exporting an existing name adds nothing.
    assert "routes/value.js::value" not in found
    assert ("components/Button.tsx::Button", "components/Base.ts::Base") in edges(db, "INHERITS")


def test_object_namespaces_name_their_methods(tmp_path):
    db = build(tmp_path, {
        "api.ts": """
            export const api = {
              timeout: 30,
              get(path: string) { return fetch(path); },
              post: (path: string) => fetch(path),
              users: {
                list() { return api.get("/users"); },
                limit: 10,
              },
              "quoted-key": () => 1,
            };
            function setup() {
              const local = { inner() {} };
            }
            app.use({ handler() {} });
        """,
    })
    found = {i for i in symbols(db) if i.startswith("api.ts::")}
    assert found == {
        "api.ts::api",
        "api.ts::api.get",
        "api.ts::api.post",
        "api.ts::api.users",
        "api.ts::api.users.list",
        "api.ts::setup",
    }
    assert ("api.ts::api.users.list", "api.ts::api.get") in edges(db, "CALLS")


# Import bindings and edge confidence.

def edges_from(db: sqlite3.Connection, src_id: str, kind: str) -> set[tuple[str, str]]:
    return set(db.execute("SELECT dst_id, confidence FROM edges WHERE src_id = ? AND kind = ?", (src_id, kind)))


def test_python_bindings_resolve_exactly(tmp_path):
    db = build(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/models/__init__.py": "from .user import User\nfrom .role import *\n",
        "pkg/models/user.py": "class User:\n    def save(self): ...\n",
        "pkg/models/role.py": "class Role: ...\n",
        "pkg/util.py": "def helper(): ...\ndef dumps(): ...\n",
        "pkg/app.py": """
            import json
            import pkg.util
            from . import util as u
            from .util import helper as h
            from .models import User, Role
            from pkg import models
            def run(user: User) -> Role:
                h()
                u.helper()
                pkg.util.helper()
                models.User()
                User.save(user)
                json.dumps({})
        """,
    })
    assert edges_from(db, "pkg/app.py::run", "CALLS") == {
        ("pkg/util.py::helper", "exact"),
        ("pkg/models/user.py::User", "exact"),
        ("pkg/models/user.py::User.save", "exact"),
    }
    # Through the package's `from .user import User` and `from .role import *`.
    assert edges_from(db, "pkg/app.py::run", "USES") == {
        ("pkg/models/user.py::User", "exact"),
        ("pkg/models/role.py::Role", "exact"),
    }
    # `json.dumps` is bound to a module outside the tree, so it doesn't guess the local dumps.
    assert ("pkg/util.py::dumps", "guess") not in edges_from(db, "pkg/app.py::run", "CALLS")


def test_typescript_bindings_resolve_exactly(tmp_path):
    db = build(tmp_path, {
        "models/user.ts": """
            export interface User { id: number }
            export default class Store {}
            export function make() {}
        """,
        "models/index.ts": """
            export { User, make as build } from "./user";
        """,
        "legacy.js": """
            const user = require("./models/user");
            function go() { user.make(); }
        """,
        "app.ts": """
            import Store from "./models/user";
            import { build, User as U } from "./models";
            import * as models from "./models";
            export function main(u: U): models.User {
              new Store();
              build();
              models.build();
              return u;
            }
        """,
    })
    assert edges_from(db, "app.ts::main", "CALLS") == {
        ("models/user.ts::Store", "exact"),
        ("models/user.ts::make", "exact"),
    }
    assert edges_from(db, "app.ts::main", "USES") == {("models/user.ts::User", "exact")}
    assert edges_from(db, "legacy.js::go", "CALLS") == {("models/user.ts::make", "exact")}


def test_go_bindings_skip_external_packages(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store
            func New() int { return 1 }
        """,
        "store/cache.go": """
            package store
            import "errors"
            func Reset() { errors.New("x"); New() }
        """,
        "cmd/main.go": """
            package main
            import st "example.com/app/store"
            func main() { st.New() }
        """,
    })
    # `errors.New` is the standard library's, not the package's own New.
    assert edges_from(db, "store/cache.go::Reset", "CALLS") == {("store/store.go::New", "exact")}
    assert edges_from(db, "cmd/main.go::main", "CALLS") == {("store/store.go::New", "exact")}


def test_typescript_path_aliases_from_nearest_config(tmp_path):
    db = build(tmp_path, {
        "shared/src/types.ts": "export interface Plot { id: number }\n",
        "web/tsconfig.base.json": """
            {
              // Comments and trailing commas are allowed.
              "compilerOptions": {
                "paths": {
                  "@/*": ["./missing/*", "./src/*"], /* tried in order */
                  "@shared/*": ["../shared/src/*"],
                  "@config": ["./src/settings.ts"],
                },
              },
            }
        """,
        "web/tsconfig.json": '{ "extends": "./tsconfig.base" }\n',
        "web/src/settings.ts": "export const debug = true;\n",
        "web/src/util/format.ts": "export function format() {}\n",
        "web/src/app.tsx": """
            import { format } from "@/util/format";
            import type { Plot } from "@shared/types";
            import { debug } from "@config";
            import React from "react";
            export function App(plot: Plot) { format(); }
        """,
        "api/jsconfig.json": '{ "compilerOptions": { "baseUrl": "./" } }\n',
        "api/lib/db.js": "export function query() {}\n",
        "api/routes/users.js": """
            import { query } from "lib/db";
            import { format } from "@/util/format";
            export function list() { query(); }
        """,
    })
    imports = edges(db, "IMPORTS")
    assert {dst for src, dst in imports if src == "web/src/app.tsx"} == {
        "web/src/util/format.ts", "shared/src/types.ts", "web/src/settings.ts",
    }
    # Aliases belong to the nearest config, so api's "@/" isn't web's.
    assert {dst for src, dst in imports if src == "api/routes/users.js"} == {"api/lib/db.js"}
    assert edges_from(db, "web/src/app.tsx::App", "CALLS") == {("web/src/util/format.ts::format", "exact")}
    assert edges_from(db, "web/src/app.tsx::App", "USES") == {("shared/src/types.ts::Plot", "exact")}
    assert edges_from(db, "api/routes/users.js::list", "CALLS") == {("api/lib/db.js::query", "exact")}


# Builds.

def test_resolving_out_of_time_keeps_parsing_and_last_edges(tmp_path, monkeypatch):
    db = build(tmp_path, {
        "lib.py": "def helper(): ...\n",
        "app.py": "from lib import helper\ndef main(): helper()\n",
    })
    root, storage = tmp_path / "src", str(tmp_path / "graph.sqlite")
    (root / "app.py").write_text("from lib import helper\ndef main(): helper()\ndef added(): helper()\n")

    # Past the deadline, resolving stops, leaving its changes to roll back.
    connection = sqlite3.connect(storage)
    try:
        service.GraphClient._resolve(connection, service.GraphClient._languages, root, 0)
        raise AssertionError("expected TimeoutError")
    except TimeoutError:
        connection.rollback()
    connection.close()

    def out_of_time(*args):
        raise TimeoutError("resolving edges timed out")

    monkeypatch.setattr(service.GraphClient, "_resolve", out_of_time)
    result = service.client.build_database(str(root), storage, 30)
    assert (result["status"], result["parsed"], "note" in result) == ("partial", 1, False)
    db = sqlite3.connect(storage)
    assert "app.py::added" in symbols(db)
    assert edges(db, "CALLS") == {("app.py::main", "lib.py::helper")}
    assert service.graph_state(storage)["status"] == "partial"

    # With nothing left to parse, more time is what it needs.
    result = service.client.build_database(str(root), storage, 30)
    assert (result["status"], result["parsed"], "note" in result) == ("partial", 0, True)

    monkeypatch.undo()
    result = service.client.build_database(str(root), storage, 30)
    assert result["status"] == "complete" and "note" not in result
    assert edges(sqlite3.connect(storage), "CALLS") == {("app.py::main", "lib.py::helper"), ("app.py::added", "lib.py::helper")}


def test_extractor_version_parses_files_again(tmp_path, monkeypatch):
    build(tmp_path, {"lib.py": "def helper(): ...\n", "app.py": "def main(): ...\n"})
    root, storage = str(tmp_path / "src"), str(tmp_path / "graph.sqlite")
    assert service.client.build_database(root, storage, 30)["parsed"] == 0
    assert not service.get_context(storage, "lib.py::helper")["stale"]

    # A new version of extraction parses every file again, though none changed.
    monkeypatch.setattr(service.GraphClient, "_extractor_version", "next")
    assert service.get_context(storage, "lib.py::helper")["stale"]
    result = service.client.build_database(root, storage, 30)
    assert (result["parsed"], result["unchanged"]) == (2, 0)
    assert not service.get_context(storage, "lib.py::helper")["stale"]


# Search.

def search(tmp_path: pathlib.Path, kind: str, query: str = "") -> list[dict]:
    result = service.search_symbols(str(tmp_path / "graph.sqlite"), kind, query)
    assert result["status"] == "complete", result
    return result["results"]


def test_search_empty_query_starts_at_entry_points(tmp_path):
    build(tmp_path, {
        "web/src/util.ts": "export function format() {}\n",
        "web/src/store.ts": 'import { format } from "./util";\nexport class Store { save() { format(); } }\n',
        "web/src/main.ts": 'import { Store } from "./store";\nnew Store();\n',
        "web/src/index.ts": 'export * from "./util";\n',  # no symbols, but imports
        "tools/script.py": "def run(): ...\n",
        "tools/empty.py": "",
    })
    assert [(r["id"], r["symbols"], r["reach"]) for r in search(tmp_path, "file")] == [
        ("web/src/main.ts", 0, 2),
        ("web/src/index.ts", 0, 1),
        ("tools/script.py", 1, 0),
    ]
    # Other kinds list their most referenced symbols.
    assert [r["id"] for r in search(tmp_path, "function")] == ["web/src/util.ts::format", "tools/script.py::run"]
    assert [r["id"] for r in search(tmp_path, "class")] == ["web/src/store.ts::Store"]


def test_search_ranks_exact_then_prefix_then_substring(tmp_path):
    build(tmp_path, {
        "store.py": """
            class Store:
                def add(self): ...
            class StoreCache: ...
            class KeyStore: ...
            def fetch(): ...
            def Fetch(): ...
            def use():
                fetch()
                fetch()
                Store()
                Store()
                KeyStore()
        """,
        "other/store.py": "class Store: ...\n",
    })
    assert [r["id"] for r in search(tmp_path, "class", "Store")] == [
        "store.py::Store",          # exact and most referenced
        "other/store.py::Store",
        "store.py::StoreCache",     # prefix
        "store.py::KeyStore",       # substring
    ]
    # Matching case ranks first, however referenced the other.
    assert search(tmp_path, "function", "Fetch")[0]["id"] == "store.py::Fetch"
    assert search(tmp_path, "function", "fetch")[0]["id"] == "store.py::fetch"
    found = search(tmp_path, "method", "Store.add")
    assert [r["id"] for r in found] == ["store.py::Store.add"]
    assert found[0]["signature"] == "def add(self)"
    # Files are named by their path, and wildcards are literal.
    assert {r["id"] for r in search(tmp_path, "file", "other/")} == {"other/store.py"}
    assert search(tmp_path, "class", "%") == []
    assert service.search_symbols(str(tmp_path / "graph.sqlite"), "widget", "x")["status"] == "failed"
    # "any" searches every kind, but only by name.
    assert [(r["id"], r["kind"]) for r in search(tmp_path, "any", "store")][:5] == [
        ("store.py::Store", "class"),        # exact ignoring case, most referenced
        ("other/store.py::Store", "class"),
        ("store.py::Store.add", "method"),   # prefixes, shortest exported first
        ("store.py::StoreCache", "class"),
        ("store.py", "file"),
    ]
    assert service.search_symbols(str(tmp_path / "graph.sqlite"), "any")["status"] == "failed"
    assert service.search_symbols(str(tmp_path / "none.sqlite"), "class", "x")["status"] == "missing"


# Files and context.

def test_get_file_outlines_symbols_and_imports(tmp_path):
    build(tmp_path, {
        "src/util.ts": "export function format() {}\n",
        "src/store.ts": """
            import React from "react";
            import "./store.css";
            import { format } from "./util";
            /** Holds items. */
            export class Store {
              items: string[];
              add(item: string): void { format(); }
            }
            export function make(): Store { return new Store(); }
        """,
        "src/main.ts": 'import { make } from "./store";\nmake();\n',
        "tools/run.py": "import os.path\nfrom typing import List, Dict\nfrom collections import abc\n",
        "src/rank.py": """
            def first(): ...
            class Unused:
                def idle(self): ...
            class Used:
                def run(self): ...
                def other(self): ...
            def go(u: Used, x: Unused):
                Used.run(u)
                Used.run(u)
        """,
    })
    storage = str(tmp_path / "graph.sqlite")
    found = service.get_file(storage, "src/store.ts")
    assert found["status"] == "complete"
    assert (found["language"], found["doc"], found["lines"]) == ("typescript", None, 10)
    assert found["imports"] == ["src/util.ts"]
    assert found["imported_by"] == ["src/main.ts"]
    assert found["external_imports"] == ["./store.css", "react"]
    assert (found["symbols"], found["truncated"]) == (4, False)
    outline = found["outline"]
    assert [(s["id"], s["signature"], s["references"]) for s in outline] == [
        ("src/store.ts::Store", "class Store", 2),          # constructed and returned by make
        ("src/store.ts::make", "function make(): Store", 1),
    ]
    assert outline[0]["doc"] == "Holds items."
    assert [s["name"] for s in outline[0]["members"]] == ["items", "add"]

    # Cut short, the most referenced stay, counting their members' references, in the file's order.
    found = service.get_file(storage, "src/store.ts", max_symbols=2)
    assert found["truncated"] and [s["name"] for s in found["outline"]] == ["Store", "make"]
    assert "members" not in found["outline"][0]

    # Submodule guesses fold into their package.
    assert service.get_file(storage, "./tools/run.py")["external_imports"] == ["collections", "os.path", "typing"]

    found = service.get_file(storage, "src/rank.py", max_symbols=3)
    assert [(s["name"], [m["name"] for m in s.get("members", [])]) for s in found["outline"]] == [
        ("Unused", []),
        ("Used", ["run"]),
    ]

    missing = service.get_file(storage, "store.ts")
    assert (missing["status"], missing["matches"]) == ("failed", ["src/store.ts"])


def test_get_context_relates_symbols(tmp_path):
    build(tmp_path, {
        "base.py": "class Base:\n    def save(self): ...\n",
        "store.py": """
            from base import Base
            def helper(): ...
            class Store(Base):
                \"\"\"Holds items.\"\"\"
                limit: int = 3
                def add(self, item: Item) -> None:
                    helper()
                    helper()
                    self.save()
            class Item: ...
        """,
        "cache.py": """
            from store import Store
            class Cache(Store): ...
            def warm():
                Store()
                anything.add(1)
        """,
    })
    storage = str(tmp_path / "graph.sqlite")
    store = service.get_context(storage, "store.py::Store")
    assert store["status"] == "complete" and not store["stale"]
    assert (store["kind"], store["signature"], store["doc"]) == ("class", "class Store(Base)", "Holds items.")
    assert store["parent"] == {"id": "store.py", "kind": "file", "signature": None}
    assert [m["id"] for m in store["members"]] == ["store.py::Store.limit", "store.py::Store.add"]
    assert [(b["id"], b["confidence"]) for b in store["bases"]] == [("base.py::Base", "exact")]
    assert [s["id"] for s in store["subclasses"]] == ["cache.py::Cache"]
    assert [(c["id"], c["confidence"], c["lines"]) for c in store["callers"]] == [("cache.py::warm", "exact", [4])]

    add = service.get_context(storage, "store.py::Store.add", include_source=True, max_source_lines=2)
    assert [(c["id"], c["lines"]) for c in add["callees"] if c["id"] == "store.py::helper"] == [("store.py::helper", [7, 8])]
    assert [u["id"] for u in add["uses"]] == ["store.py::Item"]
    # `anything.add(1)` is on an unknown object, and `add` is a method of python's own sets, so not guessed.
    assert add["callers"] == []
    assert add["source"] == "    def add(self, item: Item) -> None:\n        helper()"
    assert add["source_truncated"] and add["counts"]["callees"] == len(add["callees"])

    assert store["counts"] == {
        "members": 2, "callers": 1, "callees": 0, "bases": 1, "subclasses": 1, "uses": 0, "used_by": 0,
        "references": 0, "referenced_by": 0, "overrides": 0, "overridden_by": 0,
    }
    assert service.get_context(storage, "store.py::Item", include_source=True)["source"] == "class Item: ..."

    (tmp_path / "src" / "store.py").write_text("# changed\n")
    assert service.get_context(storage, "store.py::Store")["stale"]
    missing = service.get_context(storage, "store.py::Stor.add")
    assert missing["status"] == "failed" and missing["matches"] == ["store.py::Store.add"]


def test_related_walks_callers_and_callees(tmp_path):
    build(tmp_path, {
        "lib.py": """
            def c(): ...
            def b(): c()
            def a():
                b()
                b()
            class Box:
                def open(self): c()
        """,
        "app.py": """
            from lib import a
            def main(): a()
            def poke(thing): thing.open()
            def loop(): loop()
        """,
    })
    storage = str(tmp_path / "graph.sqlite")

    def walk(symbol_id, relationship, **options):
        result = service.get_related(storage, symbol_id, relationship, **options)
        assert result["status"] == "complete", result
        return [(r["id"], r["depth"], r["confidence"], r["via"]) for r in result["results"]]

    assert walk("lib.py::c", "callers") == [
        ("lib.py::Box.open", 1, "exact", ["lib.py::c"]),
        ("lib.py::b", 1, "exact", ["lib.py::c"]),
    ]
    assert walk("lib.py::c", "callers", depth=3) == [
        ("lib.py::Box.open", 1, "exact", ["lib.py::c"]),
        ("lib.py::b", 1, "exact", ["lib.py::c"]),
        ("lib.py::a", 2, "exact", ["lib.py::b"]),
        ("app.py::poke", 2, "guess", ["lib.py::Box.open"]),  # `thing.open()` matched by name
        ("app.py::main", 3, "exact", ["lib.py::a"]),
    ]
    assert [i for i, *_ in walk("lib.py::c", "callers", depth=3, skip_guesses=True)] == [
        "lib.py::Box.open", "lib.py::b", "lib.py::a", "app.py::main",
    ]
    assert walk("app.py::main", "callees", depth=5) == [
        ("lib.py::a", 1, "exact", ["app.py::main"]),
        ("lib.py::b", 2, "exact", ["lib.py::a"]),
        ("lib.py::c", 3, "exact", ["lib.py::b"]),
    ]
    # Calling itself isn't a caller.
    assert walk("app.py::loop", "callers") == []

    cut = service.get_related(storage, "lib.py::c", "callers", depth=3, limit=2)
    assert (len(cut["results"]), cut["count"], cut["truncated"]) == (2, 5, True)
    missing = service.get_related(storage, "lib.py::mian", "callees")
    assert missing["status"] == "failed" and missing["matches"] == []
    assert service.get_related(storage, "app.py::Main", "callees")["matches"] == ["app.py::main"]


def test_dependencies_and_dependents_walk_files(tmp_path):
    build(tmp_path, {
        "util.ts": "export const x = 1;\n",
        "store.ts": 'import { x } from "./util";\nexport class Store {}\n',
        "lazy.ts": "export const y = 2;\n",
        "app.ts": """
            import { Store } from "./store";
            export async function load() { return import("./lazy"); }
        """,
        "main.ts": 'import "./app";\n',
    })
    storage = str(tmp_path / "graph.sqlite")

    def walk(tool, file_path, **options):
        result = tool(storage, file_path, **options)
        assert result["status"] == "complete", result
        return [(r["id"], r["depth"], r["via"]) for r in result["results"]]

    # A dynamic import inside a function is still the file's.
    assert walk(service.get_dependencies, "app.ts") == [("lazy.ts", 1, ["app.ts"]), ("store.ts", 1, ["app.ts"])]
    assert walk(service.get_dependencies, "./main.ts", depth=3) == [
        ("app.ts", 1, ["main.ts"]),
        ("lazy.ts", 2, ["app.ts"]),
        ("store.ts", 2, ["app.ts"]),
        ("util.ts", 3, ["store.ts"]),
    ]
    assert walk(service.get_dependents, "util.ts", depth=9) == [
        ("store.ts", 1, ["util.ts"]),
        ("app.ts", 2, ["store.ts"]),
        ("main.ts", 3, ["app.ts"]),
    ]
    assert service.get_dependents(storage, "utils.ts")["status"] == "failed"


def test_related_follows_one_relationship(tmp_path):
    build(tmp_path, {
        "shapes.py": """
            class Shape: ...
            class Polygon(Shape):
                sides: int = 0
                class Meta:
                    def describe(self): ...
            class Square(Polygon): ...
            class Circle(Shape): ...
            def area(s: Square) -> float: ...
            def report(): area(Square())
        """,
    })
    storage = str(tmp_path / "graph.sqlite")

    def related(symbol_id, relationship, **options):
        result = service.get_related(storage, symbol_id, relationship, **options)
        assert result["status"] == "complete", result
        return [(r["id"].removeprefix("shapes.py::"), r["depth"]) for r in result["results"]]

    assert related("shapes.py::Shape", "subclasses") == [("Circle", 1), ("Polygon", 1)]
    assert related("shapes.py::Shape", "subclasses", depth=5) == [("Circle", 1), ("Polygon", 1), ("Square", 2)]
    assert related("shapes.py::Square", "bases", depth=5) == [("Polygon", 1), ("Shape", 2)]
    assert related("shapes.py::Polygon", "members", depth=2) == [
        ("Polygon.Meta", 1), ("Polygon.sides", 1), ("Polygon.Meta.describe", 2),
    ]
    assert related("shapes.py::Square", "used_by") == [("area", 1)]
    assert related("shapes.py::area", "callers") == [("report", 1)]
    failed = service.get_related(storage, "shapes.py::Shape", "children")
    assert failed["status"] == "failed" and "subclasses" in failed["error"]


def test_get_source_reads_lines(tmp_path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("".join(f"line {n}\n" for n in range(1, 11)))
    (tmp_path / "secret.txt").write_text("no\n")

    found = service.get_source(str(root), "pkg/mod.py", 3, 5)
    assert (found["status"], found["start_line"], found["end_line"], found["lines"]) == ("ok", 3, 5, 10)
    assert found["source"] == "line 3\nline 4\nline 5" and not found["truncated"]
    found = service.get_source(str(root), "pkg/mod.py", 8)
    assert (found["source"], found["end_line"], found["truncated"]) == ("line 8\nline 9\nline 10", 10, False)
    found = service.get_source(str(root), "pkg/mod.py", 2, 9, max_lines=3)
    assert (found["source"], found["end_line"], found["truncated"]) == ("line 2\nline 3\nline 4", 4, True)

    for args in (("pkg/mod.py", 11), ("pkg/mod.py", 5, 4), ("../secret.txt",), ("pkg/none.py",), ("pkg",)):
        assert service.get_source(str(root), *args)["status"] == "failed", args


def test_symbols_at_lines(tmp_path):
    build(tmp_path, {
        "store.py": """
            import os
            class Store:
                def add(self, item):
                    return item

                def drop(self, item):
                    pass
            LIMIT = 3
        """,
    })
    storage = str(tmp_path / "graph.sqlite")

    def at(*lines, **options):
        result = service.get_symbols_at(storage, "store.py", *lines, **options)
        assert result["status"] == "complete" and not result["stale"], result
        return [r["id"] for r in result["results"]]

    # A line's innermost symbol first, out to the file.
    assert at(4) == ["store.py::Store.add", "store.py::Store", "store.py"]
    # Lines between symbols and outside any are in their owners only.
    assert at(5) == ["store.py::Store", "store.py"]
    assert at(1) == ["store.py"]
    # A range spans every symbol it touches.
    assert at(4, 8) == ["store.py::LIMIT", "store.py::Store.add", "store.py::Store.drop", "store.py::Store", "store.py"]
    assert at(3, 6, limit=1) == ["store.py::Store.add"]

    missing = service.get_symbols_at(storage, "stor.py", 1)
    assert missing["status"] == "failed"
    assert service.get_symbols_at(storage, "./store.py", 8)["results"][0]["id"] == "store.py::LIMIT"


# Receiver inference.

def test_python_infers_receivers(tmp_path):
    db = build(tmp_path, {
        "models.py": """
            class Base:
                def save(self): ...
            class Store(Base):
                def add(self): ...
            class Cache:
                store: Store
                def __init__(self):
                    self.backup = Store()
                def fill(self, s: Store, t: "Optional[Store]" = None, u: Store | None = None):
                    self.flush()
                    self.store.add()
                    self.backup.save()
                    s.add()
                    u.add()
                    local = Store()
                    local.add()
                    self.store.missing()
                def flush(self): ...
            class Fast(Cache):
                def flush(self):
                    super().flush()
        """,
        "app.py": """
            import requests
            from models import Cache
            def run(c: Cache, session: requests.Session, items):
                c.store.add()
                session.get()
                items.map()
                items.copy().map()
            def map(): ...
        """,
    })
    assert edges_from(db, "models.py::Cache.fill", "CALLS") == {
        ("models.py::Cache.flush", "inferred"),   # self
        ("models.py::Fast.flush", "inferred"),    # self may be a Fast, which overrides it
        ("models.py::Store.add", "inferred"),     # class attribute, parameter, `X | None`, local
        ("models.py::Base.save", "inferred"),     # attribute from __init__, method of a base
        ("models.py::Store", "exact"),            # constructed
    }
    assert edges_from(db, "models.py::Fast.flush", "CALLS") == {("models.py::Cache.flush", "inferred")}
    # Through an attribute of a class in another file; a type from outside the tree has no edge,
    # and calls on unknown objects don't guess plain functions.
    assert edges_from(db, "app.py::run", "CALLS") == {("models.py::Store.add", "inferred")}


def test_typescript_infers_receivers(tmp_path):
    db = build(tmp_path, {
        "service.ts": """
            export class Repo { find(): void {} }
            export class Service {
              constructor(private readonly repo: Repo, plain: Repo) {}
              list(): void { this.repo.find(); this.helper(); }
              helper(): void {}
            }
        """,
        "controller.ts": """
            import { Service } from "./service";
            import * as svc from "./service";
            export class Base { log(): void {} }
            export class Controller extends Base {
              private other: svc.Service | null;
              constructor(private service: Service) { super(); }
              get(): void {
                this.service.list();
                this.other.helper();
                const s = new Service(null, null);
                s.helper();
                super.log();
              }
            }
            export const api = {
              load() { return this.save(); },
              save() {},
            };
        """,
    })
    assert edges_from(db, "service.ts::Service.list", "CALLS") == {
        ("service.ts::Repo.find", "inferred"), ("service.ts::Service.helper", "inferred"),
    }
    assert edges_from(db, "controller.ts::Controller.get", "CALLS") == {
        ("service.ts::Service.list", "inferred"),
        ("service.ts::Service.helper", "inferred"),
        ("service.ts::Service", "exact"),
        ("controller.ts::Base.log", "inferred"),
    }
    # `this` in an object namespace is the namespace.
    assert edges_from(db, "controller.ts::api.load", "CALLS") == {("controller.ts::api.save", "inferred")}


def test_go_infers_receivers(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store
            type Store struct{ cache *Cache }
            type Cache struct{}
            func (s *Store) Add() { s.save(); s.cache.Put() }
        """,
        "store/save.go": """
            package store
            func (s *Store) save() {}
            func (c *Cache) Put() {}
            func Use(st *Store) {
                st.Add()
                c := &Cache{}
                c.Put()
            }
        """,
    })
    assert edges_from(db, "store/store.go::Store.Add", "CALLS") == {
        ("store/save.go::Store.save", "inferred"), ("store/save.go::Cache.Put", "inferred"),
    }
    assert edges_from(db, "store/save.go::Use", "CALLS") == {
        ("store/store.go::Store.Add", "inferred"), ("store/save.go::Cache.Put", "inferred"),
    }


def test_python_infers_through_return_types(tmp_path):
    db = build(tmp_path, {
        "models.py": """
            from typing import Optional, Self
            class Store:
                def add(self): ...
                def copy(self) -> "Store": ...
                @classmethod
                def open(cls) -> Self: ...
            def make_store() -> Optional[Store]: ...
        """,
        "app.py": """
            import requests
            from models import make_store, Store
            def get_session() -> requests.Session: ...
            async def fetch() -> Store: ...
            class Cache:
                def __init__(self):
                    self.store = make_store()
                def make(self) -> Store: ...
                async def run(self):
                    s = make_store()
                    s.add()
                    make_store().add()
                    self.make().add()
                    Store.open().add()
                    s.copy().add()
                    self.store.add()
                    t = await fetch()
                    t.add()
                    x = self.make()
                    x.add()
                    get_session().get()
        """,
    })
    assert edges_from(db, "app.py::Cache.run", "CALLS") == {
        ("models.py::make_store", "exact"),
        ("models.py::Store.open", "exact"),
        ("app.py::fetch", "exact"),
        ("app.py::get_session", "exact"),
        ("app.py::Cache.make", "inferred"),
        ("models.py::Store.copy", "inferred"),
        ("models.py::Store.add", "inferred"),  # through every return type above
    }


def test_typescript_and_go_infer_through_return_types(tmp_path):
    db = build(tmp_path, {
        "store.ts": """
            export class Store { add(): void {} }
            export function createStore(): Store { return new Store(); }
            export async function loadStore(): Promise<Store> { return new Store(); }
        """,
        "app.ts": """
            import { Store, createStore, loadStore } from "./store";
            export const make = (): Store => createStore();
            export async function run() {
              const s = createStore();
              s.add();
              const t = await loadStore();
              t.add();
              make().add();
            }
        """,
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store
            type Store struct{}
            func (s *Store) Add() {}
            func New() (*Store, error) { return &Store{}, nil }
        """,
        "cmd/main.go": """
            package main
            import "example.com/app/store"
            func main() {
                s, err := store.New()
                _ = err
                s.Add()
            }
        """,
    })
    assert edges_from(db, "app.ts::run", "CALLS") == {
        ("store.ts::createStore", "exact"),
        ("store.ts::loadStore", "exact"),
        ("app.ts::make", "exact"),
        ("store.ts::Store.add", "inferred"),
    }
    assert edges_from(db, "cmd/main.go::main", "CALLS") == {
        ("store/store.go::New", "exact"), ("store/store.go::Store.Add", "inferred"),
    }


def test_guesses_stay_within_reach(tmp_path):
    db = build(tmp_path, {
        "web/helpers.ts": "export const len = 4;\nexport function getLayer() {}\n",
        "web/store.ts": "export class Store { holds(): boolean { return true; } has(): boolean { return true; } }\n",
        "web/lib.ts": 'import { Store } from "./store";\nexport function make(): any {}\n',
        "web/app.ts": """
            import { make } from "./lib";
            export function run(map: any, seen: any) {
              map.getLayer();
              seen.holds();
              make().holds();
              seen.has();
              helper();
            }
        """,
        "web/app.test.ts": "export function helper() {}\n",
        "web/health.ts": """
            import { HealthCheckService } from "@nestjs/terminus";
            export class HealthController {
              constructor(private health: HealthCheckService) {}
              check() { return this.health.check(); }
            }
        """,
        "tools/count.py": """
            def total(items):
                return len(items) + items.has()
        """,
        "web/provider.tsx": """
            export function Provider() {
              async function hydrate() {}
              function load() { return hydrate(); }
              return load();
            }
            function hydrate() {}
        """,
    })
    # A method reachable through imports, `Store.holds` via lib.ts, is still guessed; a unique name in a
    # file nothing imports, a test helper, another language's names, or `has` of js's own maps aren't.
    assert edges_from(db, "web/app.ts::run", "CALLS") == {("web/lib.ts::make", "exact"), ("web/store.ts::Store.holds", "guess")}
    assert edges_from(db, "tools/count.py::total", "CALLS") == set()
    # An attribute typed from outside the tree isn't guessed against the file's own methods.
    assert edges_from(db, "web/health.ts::HealthController.check", "CALLS") == set()
    # Nested functions resolve exactly, ahead of the file's own of the same name.
    assert edges_from(db, "web/provider.tsx::Provider.load", "CALLS") == {("web/provider.tsx::Provider.hydrate", "exact")}
    assert edges_from(db, "web/provider.tsx::Provider", "CALLS") == {("web/provider.tsx::Provider.load", "exact")}


def test_walks_rank_confidence(tmp_path):
    build(tmp_path, {
        "lib.py": """
            class Store:
                def add(self): self.save()
                def save(self): ...
            def use(s: Store): s.add()
            def poke(thing): thing.use_it()
            class Other:
                def use_it(self): use(None)
        """,
    })
    result = service.get_related(str(tmp_path / "graph.sqlite"), "lib.py::Store.save", "callers", depth=4)
    assert [(r["id"], r["depth"], r["confidence"]) for r in result["results"]] == [
        ("lib.py::Store.add", 1, "inferred"),
        ("lib.py::use", 2, "inferred"),
        ("lib.py::Other.use_it", 3, "inferred"),
        ("lib.py::poke", 4, "guess"),
    ]
    kept = service.get_related(str(tmp_path / "graph.sqlite"), "lib.py::Store.save", "callers", depth=4, skip_guesses=True)
    assert [r["id"] for r in kept["results"]] == ["lib.py::Store.add", "lib.py::use", "lib.py::Other.use_it"]


# Overrides.

def test_python_overrides_and_dispatch(tmp_path):
    db = build(tmp_path, {
        "shapes.py": """
            class Shape:
                def area(self): ...
                def describe(self):
                    return self.area()
            class Square(Shape):
                def area(self): ...
            class Cube(Square):
                def area(self):
                    return super().area()
            class Circle(Shape):
                def area(self): ...
        """,
        "app.py": """
            from shapes import Shape, Square
            def total(shape: Shape, square: Square):
                shape.area()
                square.area()
        """,
    })
    overrides = edges(db, "OVERRIDES")
    assert overrides == {
        ("shapes.py::Square.area", "shapes.py::Shape.area"),
        ("shapes.py::Cube.area", "shapes.py::Square.area"),
        ("shapes.py::Circle.area", "shapes.py::Shape.area"),
    }
    # A call through a type may run its subtypes' overrides, but not its siblings'.
    assert edges_from(db, "app.py::total", "CALLS") == {
        ("shapes.py::Shape.area", "inferred"),
        ("shapes.py::Square.area", "inferred"),
        ("shapes.py::Cube.area", "inferred"),
        ("shapes.py::Circle.area", "inferred"),
    }
    assert edges_from(db, "shapes.py::Shape.describe", "CALLS") == {
        ("shapes.py::Shape.area", "inferred"),
        ("shapes.py::Square.area", "inferred"),
        ("shapes.py::Cube.area", "inferred"),
        ("shapes.py::Circle.area", "inferred"),
    }
    # `super()` runs its own.
    assert edges_from(db, "shapes.py::Cube.area", "CALLS") == {("shapes.py::Square.area", "inferred")}

    storage = str(tmp_path / "graph.sqlite")
    area = service.get_context(storage, "shapes.py::Square.area")
    assert [(o["id"], o["confidence"]) for o in area["overrides"]] == [("shapes.py::Shape.area", "exact")]
    assert [o["id"] for o in area["overridden_by"]] == ["shapes.py::Cube.area"]
    assert [c["id"] for c in area["callers"]] == ["app.py::total", "shapes.py::Cube.area", "shapes.py::Shape.describe"]
    walked = service.get_related(storage, "shapes.py::Shape.area", "overridden_by", depth=2)
    assert [(r["id"], r["depth"]) for r in walked["results"]] == [
        ("shapes.py::Circle.area", 1), ("shapes.py::Square.area", 1), ("shapes.py::Cube.area", 2),
    ]


def test_ts_implements_interfaces(tmp_path):
    db = build(tmp_path, {
        "store.ts": """
            export interface Saver {
              save(item: string): void;
              name: string;
            }
            export abstract class Base implements Saver {
              name = "base";
              abstract save(item: string): void;
            }
            export class Disk extends Base {
              save(item: string) {}
            }
            export function persist(saver: Saver) {
              saver.save("x");
            }
            export function blind(thing) {
              thing.save("x");
            }
            export function bare() {
              save("x");
            }
            export function shadowed() {
              const persist = (x: string) => x;
              persist("x");
            }
        """,
    })
    found = symbols(db)
    assert found["store.ts::Saver.save"][:2] == ("method", "save(item: string): void")
    assert found["store.ts::Base.save"][0] == "method"
    assert "store.ts::Saver.name" not in found
    assert edges(db, "OVERRIDES") == {
        ("store.ts::Base.save", "store.ts::Saver.save"),
        ("store.ts::Disk.save", "store.ts::Base.save"),
    }
    assert edges_from(db, "store.ts::persist", "CALLS") == {
        ("store.ts::Saver.save", "inferred"),
        ("store.ts::Base.save", "inferred"),
        ("store.ts::Disk.save", "inferred"),
    }
    # Guesses by name skip interface methods, which only declare.
    assert ("store.ts::Saver.save", "guess") not in edges_from(db, "store.ts::blind", "CALLS")
    # Nor methods for bare names, as methods are only called on something.
    assert edges_from(db, "store.ts::bare", "CALLS") == set()
    # Nor the file's functions for bare calls of local ones.
    assert edges_from(db, "store.ts::shadowed", "CALLS") == set()
    exported = dict(db.execute("SELECT id, exported FROM symbols WHERE id = 'store.ts::Saver.save'"))
    assert exported == {"store.ts::Saver.save": 1}


def test_python_protocols_are_implemented_structurally(tmp_path):
    db = build(tmp_path, {
        "saving.py": """
            from typing import Protocol
            class Saver(Protocol):
                def save(self): ...
                def close(self): ...
            class Base:
                def close(self): ...
            class Disk(Base):
                def save(self): ...
            class Partial:
                def save(self): ...
            class Declared(Saver):
                def save(self): ...
            def persist(saver: Saver):
                saver.save()
            class Callback(Protocol):
                def __call__(self): ...
            class Handler:
                def __call__(self): ...
        """,
        "elsewhere.py": """
            class Unrelated:
                def save(self): ...
                def close(self): ...
        """,
    })
    inherits = set(db.execute("SELECT src_id, dst_id, confidence FROM edges WHERE kind = 'INHERITS'"))
    # Having all its methods, its own or its bases', implements it; subclassing it is declaring it.
    assert ("saving.py::Disk", "saving.py::Saver", "inferred") in inherits
    assert ("saving.py::Declared", "saving.py::Saver", "exact") in inherits
    assert not {(src, dst) for src, dst, _ in inherits if src == "saving.py::Partial"}
    # Not by dunders alone, which say nothing without signatures, nor where nothing reaches both.
    assert not {(src, dst) for src, dst, _ in inherits if src in ("saving.py::Handler", "elsewhere.py::Unrelated")}
    assert set(db.execute("SELECT src_id, dst_id, confidence FROM edges WHERE kind = 'OVERRIDES'")) == {
        ("saving.py::Disk.save", "saving.py::Saver.save", "inferred"),
        ("saving.py::Base.close", "saving.py::Saver.close", "inferred"),
        ("saving.py::Declared.save", "saving.py::Saver.save", "exact"),
    }
    assert edges_from(db, "saving.py::persist", "CALLS") == {
        ("saving.py::Saver.save", "inferred"), ("saving.py::Disk.save", "inferred"), ("saving.py::Declared.save", "inferred"),
    }


def test_ts_interfaces_are_implemented_structurally(tmp_path):
    db = build(tmp_path, {
        "saving.ts": """
            export interface Saver {
              save(): void;
            }
            export class Disk {
              save() {}
            }
            export const memory: Saver = {
              save() {},
            };
            export function persist(saver: Saver) {
              saver.save();
            }
        """,
    })
    inherits = set(db.execute("SELECT src_id, dst_id, confidence FROM edges WHERE kind = 'INHERITS'"))
    assert inherits == {
        ("saving.ts::Disk", "saving.ts::Saver", "inferred"),    # having its methods
        ("saving.ts::memory", "saving.ts::Saver", "exact"),     # typed as it
    }
    assert edges_from(db, "saving.ts::persist", "CALLS") == {
        ("saving.ts::Saver.save", "inferred"), ("saving.ts::Disk.save", "inferred"), ("saving.ts::memory.save", "inferred"),
    }


def test_go_types_implement_interfaces(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store

            // Saver saves items.
            type Saver interface {
            	// Save saves one.
            	Save(item string) error
            	Close() error
            }

            type Disk struct{}

            func (d *Disk) Close() error { return nil }
        """,
        "store/disk.go": """
            package store

            func (d *Disk) Save(item string) error { return nil }

            type Partial struct{}

            func (p Partial) Save(item string) error { return nil }

            func Persist(s Saver) error {
            	return s.Save("x")
            }
        """,
    })
    found = symbols(db)
    assert found["store/store.go::Saver.Save"] == ("method", "Save(item string) error", "Save saves one.")
    # Implementing is having all of an interface's methods, wherever in the package they're declared.
    assert set(db.execute("SELECT src_id, dst_id, confidence FROM edges WHERE kind = 'INHERITS'")) == {
        ("store/store.go::Disk", "store/store.go::Saver", "inferred"),
    }
    assert edges(db, "OVERRIDES") == {
        ("store/disk.go::Disk.Save", "store/store.go::Saver.Save"),
        ("store/store.go::Disk.Close", "store/store.go::Saver.Close"),
    }
    assert edges_from(db, "store/disk.go::Persist", "CALLS") == {
        ("store/store.go::Saver.Save", "inferred"),
        ("store/disk.go::Disk.Save", "inferred"),
    }


def test_local_names_shadow_bare_calls(tmp_path):
    db = build(tmp_path, {
        "copy.py": """
            def deepcopy(x): ...
            def log(x): ...
            def rule(x): ...
            def walk(items, deepcopy=deepcopy):
                for rule in items:
                    rule(1)
                deepcopy(items)
            def init():
                global log
                log = print
                log(1)
            def local(log):
                log(1)
            def nested(onexc=None):
                if onexc is None:
                    def onexc(x): ...
                onexc(1)
        """,
    })
    # Aliases of the file's names and `global` names are the file's; parameters and loop variables aren't.
    assert edges_from(db, "copy.py::walk", "CALLS") == {("copy.py::deepcopy", "exact")}
    assert edges_from(db, "copy.py::init", "CALLS") == {("copy.py::log", "exact")}
    assert edges_from(db, "copy.py::local", "CALLS") == set()
    # A function defined in one is its symbol, though a parameter has its name too.
    assert edges_from(db, "copy.py::nested", "CALLS") == {("copy.py::nested.onexc", "exact")}


def test_go_embedding_promotes_and_implements(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store

            import "sync"

            type Reader interface{ Read() string }
            type Closer interface{ Close() error }
            type ReadCloser interface {
            	Reader
            	Closer
            }
            type Numbers interface{ ~int | ~float64 }

            type Base struct{}

            func (b *Base) Read() string { return "" }
            func (b *Base) Close() error { return nil }

            type Store struct {
            	*Base
            	sync.Mutex
            	Name string
            }

            func (s *Store) Close() error { return nil }

            func Use(s *Store, rc ReadCloser, c Closer) {
            	s.Read()
            	s.Lock()
            	s.Base.Close()
            	rc.Read()
            	c.Close()
            }
        """,
    })
    inherits = set(db.execute("SELECT src_id, dst_id, confidence FROM edges WHERE kind = 'INHERITS'"))
    # Embedded types are bases; type sets aren't.
    assert {(src, dst) for src, dst, c in inherits if c == "exact"} == {
        ("store/store.go::Store", "store/store.go::Base"),
        ("store/store.go::ReadCloser", "store/store.go::Reader"),
        ("store/store.go::ReadCloser", "store/store.go::Closer"),
    }
    # Types implement interfaces with promoted methods, and interfaces with embedded ones.
    implements = {(src, dst) for src, dst, c in inherits if c == "inferred"}
    assert ("store/store.go::Store", "store/store.go::ReadCloser") in implements
    assert ("store/store.go::Base", "store/store.go::ReadCloser") in implements
    # Shadowing an embedded type's method isn't overriding it; implementing an interface's is.
    assert ("store/store.go::Store.Close", "store/store.go::Base.Close") not in edges(db, "OVERRIDES")
    assert ("store/store.go::Store.Close", "store/store.go::Closer.Close") in edges(db, "OVERRIDES")
    assert edges_from(db, "store/store.go::Use", "CALLS") == {
        ("store/store.go::Base.Read", "inferred"),    # promoted, and through ReadCloser
        ("store/store.go::Base.Close", "inferred"),   # through the embedded field, and through Closer
        ("store/store.go::Reader.Read", "inferred"),
        ("store/store.go::Closer.Close", "inferred"),
        ("store/store.go::Store.Close", "inferred"),  # through Closer
    }


def test_jsdoc_types_javascript(tmp_path):
    db = build(tmp_path, {
        "store.js": """
            export class Store {
              add() {}
            }
            /** @returns {Store} */
            export function makeStore() { return new Store(); }
        """,
        "app.js": """
            import { Store, makeStore } from "./store.js";

            /**
             * Fills a store.
             * @param {?Store} store - where to put them
             * @param {Array<Store>} many
             * @param {import("./cache.js").Cache} [cache]
             * @returns {Promise<Store | null>}
             */
            export async function fill(store, many, cache) {
              store.add();
              cache.flush();
              makeStore().add();
              return store;
            }

            /** @type {Store} */
            export const shared = makeStore();

            export class Holder {
              constructor() {
                /** @type {Store} */
                this.store = makeStore();
              }
              run() {
                this.store.add();
                shared.add();
              }
            }
        """,
        "cache.js": "export class Cache { flush() {} }\n",
    })
    assert edges_from(db, "app.js::fill", "CALLS") == {
        ("store.js::Store.add", "inferred"),   # a parameter, and what a function returns
        ("cache.js::Cache.flush", "inferred"), # imported in the type
        ("store.js::makeStore", "exact"),
    }
    assert edges_from(db, "app.js::Holder.run", "CALLS") == {("store.js::Store.add", "inferred")}
    assert edges_from(db, "app.js::fill", "USES") == {("store.js::Store", "exact"), ("cache.js::Cache", "exact")}
    assert edges_from(db, "app.js::shared", "USES") == {("store.js::Store", "exact")}
    assert ("app.js", "cache.js") in edges(db, "IMPORTS")
    assert symbols(db)["app.js::fill"][2] == "Fills a store."


# Values and components.

def test_python_references_values(tmp_path):
    db = build(tmp_path, {
        "lib.py": """
            from enum import Enum
            class StoreError(Exception): ...
            class Role(Enum):
                ADMIN = 1
            DEFAULT_LIMIT = 10
            def format_item(x): ...
            def handler(): ...
        """,
        "app.py": """
            import lib
            from lib import StoreError, Role, DEFAULT_LIMIT, format_item, handler
            data = []
            def run(items, data, limit=DEFAULT_LIMIT):
                register(handler)
                out = list(map(format_item, items))
                if role == Role.ADMIN or isinstance(out, StoreError):
                    pass
                try:
                    callbacks = [handler, lib.handler]
                except (StoreError, KeyError) as e:
                    raise StoreError
                return data
            def run_again():
                return run
            class Service:
                def handle(self): ...
                def start(self):
                    register(self.handle)
        """,
    })
    # Locals, like the `data` parameter over the file's `data`, and builtins aren't references.
    assert edges_from(db, "app.py::run", "REFERENCES") == {
        ("lib.py::DEFAULT_LIMIT", "exact"),
        ("lib.py::handler", "exact"),
        ("lib.py::format_item", "exact"),
        ("lib.py::Role.ADMIN", "exact"),
        ("lib.py::StoreError", "exact"),
    }
    assert edges_from(db, "app.py::run_again", "REFERENCES") == {("app.py::run", "exact")}
    assert edges_from(db, "app.py::Service.start", "REFERENCES") == {("app.py::Service.handle", "inferred")}
    # Bases inherit; they aren't values too.
    assert edges_from(db, "lib.py::StoreError", "REFERENCES") == set()


def test_tsx_renders_components_and_references_values(tmp_path):
    db = build(tmp_path, {
        "components/Button.tsx": "export function Button() { return <div />; }\n",
        "components/Layout.tsx": "export const Layout = { Main: () => null };\n",
        "api.ts": "export function save() {}\nexport enum Role { Admin }\n",
        "App.tsx": """
            import { Button } from "./components/Button";
            import { Layout } from "./components/Layout";
            import { save, Role } from "./api";
            export function App({ items }) {
              const onSave = save;
              return (
                <Layout.Main>
                  <Button onClick={save} role={Role.Admin} />
                  {items.map(renderItem)}
                  <div />
                </Layout.Main>
              );
            }
            function renderItem(x) { return x; }
        """,
        "App.test.tsx": """
            import { App } from "./App";
            it("renders", () => { render(<App />); });
        """,
    })
    assert edges_from(db, "App.tsx::App", "CALLS") == {
        ("components/Layout.tsx::Layout.Main", "exact"), ("components/Button.tsx::Button", "exact"),
    }
    # An enum member that isn't a symbol refers to its enum.
    assert edges_from(db, "App.tsx::App", "REFERENCES") == {
        ("api.ts::save", "exact"), ("api.ts::Role", "exact"), ("App.tsx::renderItem", "exact"),
    }
    tests = service.get_related(str(tmp_path / "graph.sqlite"), "components/Button.tsx::Button", "tests", depth=2)
    assert [(r["id"], r["depth"]) for r in tests["results"]] == [("App.test.tsx", 2)]


def test_callback_locals_stay_local(tmp_path):
    db = build(tmp_path, {
        "store.test.ts": """
            const a = 1;
            const crop = "x";
            setup({ crop: ["none"], limit: 3 });
            describe("store", () => {
              const each = 60000;
              it("sorts", () => {
                rows.sort(function (a, b) { return keep(a, b, each, crop); });
              });
            });
            items.map((a) => a);
            items.map(a => a);
            registerHooks({ resolve(specifier) { const parent = specifier; return parent; } });
        """,
    })
    # Callback locals and object keys outside a namespace aren't symbols; parameters shadow the file's names.
    assert set(symbols(db)) == {"store.test.ts", "store.test.ts::a", "store.test.ts::crop"}
    assert edges_from(db, "store.test.ts", "REFERENCES") == {("store.test.ts::crop", "exact")}


def test_go_references_values(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store
            var ErrMissing error
            func Handle() {}
            func Register(f func()) {}
        """,
        "cmd/main.go": """
            package main
            import "example.com/app/store"
            func main() {
                store.Register(store.Handle)
                err := run()
                if err == store.ErrMissing {
                    return
                }
            }
            func run() error { return nil }
        """,
    })
    assert edges_from(db, "cmd/main.go::main", "REFERENCES") == {
        ("store/store.go::Handle", "exact"), ("store/store.go::ErrMissing", "exact"),
    }


# Exported symbols.

def test_exported_symbols(tmp_path):
    db = build(tmp_path, {
        "api.py": """
            def public(): ...
            def _private(): ...
            class Store:
                def __init__(self): ...
                def add(self): ...
                def _check(self): ...
            class _Hidden:
                def add(self): ...
            def outer():
                def inner(): ...
        """,
        "listed.py": """
            __all__ = ["shown"]
            def shown(): ...
            def unlisted(): ...
        """,
        "api.ts": """
            export function load() {}
            function helper() {}
            const later = 1;
            export { later };
            export class Repo {
              find() {}
              private cache() {}
              #secret() {}
              protected guard() {}
            }
            class Local { run() {} }
            export const ns = { get() {} };
            (function () { function wrapped() {} })();
        """,
        "Button.tsx": "export default function () { return null; }\n",
        "go.mod": "module example.com/app\n",
        "store/store.go": """
            package store
            type Store struct{ Items []int; count int }
            type cache struct{}
            func New() *Store { return nil }
            func helper() {}
            func (s *Store) Add() {}
            func (s *Store) reset() {}
            func (c *cache) Put() {}
        """,
    })
    exported = {i for (i,) in db.execute("SELECT id FROM symbols WHERE exported")}
    assert exported == {
        "api.py::public", "api.py::Store", "api.py::Store.__init__", "api.py::Store.add", "api.py::outer",
        "listed.py::shown",
        "api.ts::load", "api.ts::later", "api.ts::Repo", "api.ts::Repo.find", "api.ts::ns", "api.ts::ns.get",
        "Button.tsx::Button",
        "store/store.go::Store", "store/store.go::Store.Items", "store/store.go::New", "store/store.go::Store.Add",
    }
    storage = str(tmp_path / "graph.sqlite")
    assert service.get_context(storage, "api.py::Store")["exported"]
    assert [(m["id"], m["exported"]) for m in service.get_context(storage, "api.py::Store")["members"]] == [
        ("api.py::Store.__init__", True), ("api.py::Store.add", True), ("api.py::Store._check", False),
    ]
    assert [(s["name"], s["exported"]) for s in service.get_file(storage, "listed.py")["outline"]] == [
        ("__all__", False), ("shown", True), ("unlisted", False),
    ]
    # Exported first among equally referenced matches.
    assert [r["id"] for r in service.search_symbols(storage, "method", "add")["results"]] == [
        "api.py::Store.add", "api.py::_Hidden.add", "store/store.go::Store.Add",
    ]


# Test code.

def test_test_paths():
    tests = [
        "tests/helpers.py", "pkg/test/x.ts", "src/__tests__/a.js", "app/e2e/flow.ts", "spec/a.rb",
        "test_store.py", "pkg/conftest.py", "store_test.py", "store_test.go",
        "a.test.ts", "a.test.tsx", "a.spec.js", "a.spec.mjs", "app.e2e-spec.ts",
    ]
    code = ["store.py", "testing.py", "contest.py", "latest/x.py", "attest.ts", "spectrum.ts", "a.tests.ts", "pkg/testdata.go"]
    assert [path for path in tests if not service.GraphClient._test_paths.search(path)] == []
    assert [path for path in code if service.GraphClient._test_paths.search(path)] == []


def test_tests_rank_last_and_reach_what_they_test(tmp_path):
    build(tmp_path, {
        "src/store.ts": "export class Store { add(): void {} }\n",
        "src/main.ts": 'import { Store } from "./store";\nnew Store().add();\n',
        "src/store.spec.ts": """
            import { Store } from "./store";
            describe("Store", () => {
              it("adds", () => { const s = new Store(); s.add(); });
            });
        """,
        "lib.py": "def helper(): ...\ndef run(): helper()\n",
        "tests/test_lib.py": """
            from lib import run
            def test_run():
                run()
            def make(): ...
        """,
    })
    storage = str(tmp_path / "graph.sqlite")
    files = service.search_symbols(storage, "file")["results"]
    assert [(r["id"], r["test"]) for r in files] == [
        ("src/main.ts", False), ("tests/test_lib.py", True), ("src/store.spec.ts", True),
    ]

    def tests_of(symbol_id, depth=1):
        result = service.get_related(storage, symbol_id, "tests", depth=depth)
        assert result["status"] == "complete", result
        assert all(r["test"] for r in result["results"])
        return [(r["id"], r["depth"]) for r in result["results"]]

    # Test callbacks' calls belong to their file.
    assert tests_of("src/store.ts::Store.add") == [("src/store.spec.ts", 1)]
    assert tests_of("lib.py::helper") == []
    assert tests_of("lib.py::helper", depth=2) == [("tests/test_lib.py::test_run", 2)]

    context = service.get_context(storage, "lib.py::run")
    assert not context["test"] and [(c["id"], c["test"]) for c in context["callers"]] == [("tests/test_lib.py::test_run", True)]
    assert service.get_file(storage, "tests/test_lib.py")["test"]
    assert [r["test"] for r in service.search_symbols(storage, "function", "test_run")["results"]] == [True]


# Go.

def test_go_packages_receivers_and_types(tmp_path):
    db = build(tmp_path, {
        "go.mod": "module example.com/app\n\ngo 1.22\n",
        "internal/store/store.go": """
            package store

            import "fmt"

            // MaxItems caps the store.
            const MaxItems = 10

            // Store holds items.
            type Store struct {
            	items []*Item
            	log   Logger
            }

            type Item struct{ ID string }

            // Logger writes messages.
            type Logger interface {
            	Log(msg string)
            }

            type ID = string

            // New makes a store.
            func New() *Store { return &Store{} }

            // Add appends an item.
            func (s *Store) Add(item *Item) error {
            	fmt.Println(item)
            	s.save(item)
            	return nil
            }
        """,
        "internal/store/save.go": """
            package store

            func (s *Store) save(item *Item) {}
        """,
        "cmd/app/main.go": """
            package main

            import (
            	st "example.com/app/internal/store"
            	"github.com/other/store"
            )

            func main() {
            	st.New()
            	run := func() { helper() }
            	run()
            }

            func helper() {}
        """,
    })
    found = symbols(db)
    assert found["internal/store/store.go::MaxItems"] == ("variable", "MaxItems = 10", "MaxItems caps the store.")
    assert found["internal/store/store.go::Store"] == ("class", "Store struct", "Store holds items.")
    assert found["internal/store/store.go::Store.items"][0] == "variable"
    assert found["internal/store/store.go::Logger"][0] == "interface"
    assert found["internal/store/store.go::ID"][0] == "type"
    # Methods are qualified by their receiver type, wherever they are declared.
    assert found["internal/store/store.go::Store.Add"] == (
        "method", "func (s *Store) Add(item *Item) error", "Add appends an item.",
    )
    assert found["internal/store/save.go::Store.save"][0] == "method"

    # Imports resolve through go.mod to every file of the package; external modules have no edge.
    assert {dst for src, dst in edges(db, "IMPORTS") if src == "cmd/app/main.go"} == {
        "internal/store/store.go", "internal/store/save.go",
    }
    calls = edges(db, "CALLS")
    assert ("cmd/app/main.go::main", "internal/store/store.go::New") in calls
    assert ("cmd/app/main.go::main", "cmd/app/main.go::helper") in calls
    # Files of one package see each other without importing.
    assert ("internal/store/store.go::Store.Add", "internal/store/save.go::Store.save") in calls

    uses = edges(db, "USES")
    assert ("internal/store/store.go::Store.log", "internal/store/store.go::Logger") in uses
    assert ("internal/store/store.go::Store.Add", "internal/store/store.go::Item") in uses
