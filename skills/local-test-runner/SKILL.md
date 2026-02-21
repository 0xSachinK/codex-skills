---
name: local-test-runner
description: >
  Local verification runner for any repository. Use when running tests, lint,
  type checks, coverage, build verification, or targeted test commands. Detect
  package manager/toolchain/framework at runtime, execute the right commands,
  and return structured results for other skills such as `ship-*` workflows.
---

# Local Test Runner

A universal, zero-configuration skill for detecting and executing local quality
checks across any repository. All detection happens at runtime; never hardcode
repository names, organization names, or project-specific paths.

---

## 1. Auto-Detect Repo Profile

Before running any commands, build a complete profile of the repository.
Every value must be discovered dynamically.

### 1.1 Repo Root

Detect the repository root directory:

```bash
git rev-parse --show-toplevel
```

All subsequent paths are relative to this root. If this command fails, abort
with an error indicating the directory is not inside a git repository.

### 1.2 Package Manager

Detect the package manager by lockfile at the repo root, in priority order:

| Lockfile             | Package Manager |
|----------------------|-----------------|
| `bun.lockb`          | bun             |
| `pnpm-lock.yaml`     | pnpm            |
| `yarn.lock`          | yarn            |
| `package-lock.json`  | npm             |

If no lockfile exists but `package.json` is present, default to `npm`. If no
`package.json` exists, skip Node detection and check other ecosystems (Rust,
Go, Python, Solidity).

### 1.3 Test Framework

Detect the test framework by config files at the repo root:

| Config File(s)                          | Framework  |
|-----------------------------------------|------------|
| `vitest.config.*` (ts, js, mts, mjs)   | Vitest     |
| `jest.config.*` (ts, js, json, cjs)     | Jest       |
| `foundry.toml`                          | Foundry    |
| `hardhat.config.*` (ts, js)             | Hardhat    |
| `pytest.ini`, `pyproject.toml`, `setup.cfg` (with `[tool:pytest]`) | pytest |
| `Cargo.toml`                           | cargo test |
| `go.mod`                               | go test    |

If multiple frameworks are detected, record all of them. Prefer the framework
matching the file under test; if ambiguous, prefer the one configured at the
repo root.

### 1.4 Available Scripts

Read `package.json` at the repo root (if present) and extract the `scripts`
object. Look for:

- `test` -- primary test command
- `test:coverage` or `coverage` -- tests with coverage collection
- `lint` or `lint:check` -- linter execution
- `typecheck` or `type-check` or `tsc` -- type checking
- `build` -- build step

When a script exists, prefer running it via the detected package manager
(e.g., `pnpm run test`) rather than invoking the framework binary directly.
Scripts may include project-specific flags or environment setup.

### 1.5 AGENTS.md Hints

If an `AGENTS.md` file exists at the repo root, read it and extract:

- Custom test commands or overrides
- Required environment variables
- Required services (databases, caches, message brokers)
- Workspace or monorepo instructions
- Any explicit instructions for running tests

Hints from `AGENTS.md` take precedence over auto-detected defaults (e.g., if
it says "run tests with `make test`", use that instead of the detected script).

---

## 2. Run Commands by Mode

Accept one of five modes defining which steps to execute.

### 2.1 Mode Definitions

| Mode       | Steps Executed                        | Use Case                                      |
|------------|---------------------------------------|-----------------------------------------------|
| `quick`    | typecheck, lint                       | Fast feedback during development              |
| `test`     | typecheck, lint, test                 | Standard pre-commit or pre-push check         |
| `coverage` | typecheck, lint, test (with coverage) | Measuring test coverage                       |
| `full`     | typecheck, lint, test, build          | Complete verification before shipping         |
| `targeted` | Run specific test file(s) or pattern  | Debugging a specific failing test             |

### 2.2 Step Execution

Execute each step sequentially. If a step fails, record the failure and continue
to the next step (do not abort early). Construct the command based on the
detected profile:

**Typecheck:**
- If `typecheck` script exists: `<pm> run typecheck`
- Else if `tsconfig.json` exists: `npx tsc --noEmit`
- Else: skip (record as `"skipped": true`)

**Lint:**
- If `lint` script exists: `<pm> run lint`
- Else if `.eslintrc.*` or `eslint.config.*` exists: `npx eslint .`
- Else if `ruff.toml` or `pyproject.toml` with `[tool.ruff]` exists: `ruff check .`
- Else if `Cargo.toml` exists: `cargo clippy`
- Else: skip

**Test:**
- If `test` script exists: `<pm> run test`
- Else by framework:
  - Vitest: `npx vitest run`
  - Jest: `npx jest`
  - Foundry: `forge test -vv`
  - Hardhat: `npx hardhat test`
  - pytest: `pytest -v`
  - cargo: `cargo test`
  - go: `go test ./...`

**Coverage:**
- If `test:coverage` or `coverage` script exists: `<pm> run test:coverage` or `<pm> run coverage`
- Else by framework:
  - Vitest: `npx vitest run --coverage`
  - Jest: `npx jest --coverage`
  - Foundry: `forge coverage`
  - pytest: `pytest --cov --cov-report=term`
  - cargo: `cargo tarpaulin` (if installed) or `cargo llvm-cov`
  - go: `go test -coverprofile=coverage.out ./... && go tool cover -func=coverage.out`

**Build:**
- If `build` script exists: `<pm> run build`
- Else if `Cargo.toml` exists: `cargo build`
- Else if `go.mod` exists: `go build ./...`
- Else if `foundry.toml` exists: `forge build`
- Else: skip

### 2.3 Timeout Handling

Set a per-step timeout of 5 minutes (300,000 ms). If exceeded, kill the
process, record the step as failed with `"timeout": true`, and continue.

---

## 3. Output Structured Results

After all steps complete, return a JSON object with this schema:

```json
{
  "repo": "<repo-directory-name>",
  "mode": "<mode>",
  "package_manager": "<pm>",
  "test_framework": "<framework>",
  "results": {
    "typecheck": { "passed": true, "skipped": false, "output": "..." },
    "lint": { "passed": true, "skipped": false, "output": "..." },
    "test": { "passed": false, "skipped": false, "failures": 3, "output": "..." },
    "coverage": { "line_pct": 85.2, "branch_pct": 72.1 },
    "build": { "passed": true, "skipped": false, "output": "..." }
  },
  "overall": "PASS",
  "failed_steps": ["test"]
}
```

Field definitions:

- `repo`: Basename of the repo root directory.
- `mode`: The mode that was executed.
- `package_manager`: Detected package manager, or `"none"` for non-Node repos.
- `test_framework`: Primary detected framework (comma-separated if multiple).
- `results`: Object keyed by step name. Each step contains:
  - `passed`: `true` if exit code 0.
  - `skipped`: `true` if the step was not applicable.
  - `output`: Last 200 lines of stdout+stderr (truncate earlier output).
  - `failures`: (test only) Count of individual test failures parsed from output.
  - `timeout`: Present and `true` only if killed due to timeout.
  - `line_pct`, `branch_pct`: (coverage only) Parsed from output; `null` if unparseable.
- `overall`: `"PASS"` if zero steps failed (excluding skipped). `"FAIL"` if any step failed.
- `failed_steps`: An array of step names that failed. Empty array if all passed.

Only include steps that were part of the requested mode.

### 3.1 Integration with verify-changes

The `verify-changes` skill uses this structured output to compute a readiness
score. When called by `verify-changes`:

- Return the full JSON object as described above.
- The `verify-changes` skill maps each step result to its scoring rubric.
- A `"PASS"` overall result contributes positively to the readiness score; any
  `"FAIL"` result reduces it according to the severity of the failed step.

---

## 4. Targeted Test Execution

When mode is `targeted`, accept one or both of:

- **File paths**: One or more specific test file paths.
- **Name patterns**: A string pattern to match against test names.

Construct the command by framework:

| Framework | File Target                                | Name Pattern                                    |
|-----------|--------------------------------------------|-------------------------------------------------|
| Vitest    | `npx vitest run src/foo.test.ts`           | `npx vitest run -t "should handle edge case"`   |
| Jest      | `npx jest src/foo.test.ts`                 | `npx jest -t "should handle edge case"`         |
| Foundry   | `forge test --match-path test/Foo.t.sol -vvv` | `forge test --match-test "testSpecificThing" -vvv` |
| Hardhat   | `npx hardhat test test/Foo.ts`             | `npx hardhat test --grep "should handle"`       |
| pytest    | `pytest tests/test_foo.py -v`              | `pytest tests/test_foo.py::test_specific -v`    |
| cargo     | `cargo test test_specific_thing`           | `cargo test test_specific_thing -- --exact`     |
| go        | `go test ./pkg/... -run TestSpecific`      | `go test ./... -run "TestSpecific" -v`          |

When both file paths and name patterns are provided, combine them (e.g.,
`npx vitest run src/foo.test.ts -t "should handle edge case"`).

For targeted mode, skip typecheck, lint, and build. Run only the specified
tests. Set skipped steps to `"skipped": true` in the output JSON.

### 4.1 Verbose Output for Targeted Runs

Increase verbosity for targeted runs so the caller can diagnose failures
without re-running:

- Foundry: `-vvv` (traces on failure)
- pytest: `-v` (verbose test names)
- Vitest/Jest: `--reporter=verbose`
- go: `-v`

---

## 5. Handling Prerequisites

### 5.1 Service Dependency Detection

Before running tests, check for required external services by scanning:

1. **AGENTS.md** for mentions of Redis, PostgreSQL, MySQL, MongoDB, Docker,
   Elasticsearch, RabbitMQ, Kafka, etc.
2. **docker-compose.yml / compose.yml** for defined services.
3. **`.env.example` / `.env.test`** for service connection strings.

### 5.2 Service Health Checks

For each detected dependency, verify it is reachable:

| Service      | Health Check Command                                      |
|--------------|-----------------------------------------------------------|
| PostgreSQL   | `pg_isready -h localhost` or check port 5432              |
| Redis        | `redis-cli ping` (expect `PONG`)                         |
| MySQL        | `mysqladmin ping -h localhost` or check port 3306         |
| MongoDB      | check port 27017                                          |
| Docker       | `docker info` (exit code 0)                               |
| Elasticsearch| `curl -s http://localhost:9200/_cluster/health`           |

Port checks: `lsof -i :<port>` or `nc -z localhost <port>`.

### 5.3 Failure Behavior

If a required service is not running:

1. **Warn clearly**: Print the service name, expected port, and status.
2. **Suggest a start command**: For Docker Compose services, suggest
   `docker compose up -d`. For standalone services, suggest the appropriate
   start command (e.g., `brew services start redis`, `pg_ctl start`).
3. **Never silently skip tests.** Run them anyway and let them fail with real
   error messages rather than masking the problem.
4. **Include the warning in the output JSON**: Add a top-level `"warnings"`
   array with objects like:
   ```json
   { "service": "redis", "status": "not_running", "suggestion": "docker compose up -d" }
   ```

---

## 6. Monorepo Handling

### 6.1 Detection

Detect monorepo configurations by scanning for:

| Indicator                              | Type             |
|----------------------------------------|------------------|
| `pnpm-workspace.yaml`                  | pnpm workspaces  |
| `package.json` with `"workspaces"` key | yarn/npm workspaces |
| `lerna.json`                           | Lerna             |
| `nx.json`                              | Nx                |
| `turbo.json`                           | Turborepo        |

### 6.2 Workspace-Aware Execution

When operating inside a monorepo:

1. **Determine scope.** If a package name is specified or the working directory
   is inside a specific package, scope commands to that package.

2. **Use filter flags:**
   - pnpm: `pnpm --filter <package-name> run test`
   - yarn (v1): `yarn workspace <package-name> test`
   - yarn (berry): `yarn workspace <package-name> run test`
   - npm (v7+): `npm -w <package-name> run test`
   - nx: `npx nx run <package-name>:test`
   - turbo: `npx turbo run test --filter=<package-name>`

3. **Package-level profile.** Re-run detection (sections 1.2--1.4) at the
   package level, since each package may have its own scripts and config.

4. **Root-level fallback.** If no package is specified, run from the repo root
   (e.g., `pnpm -r run test`, `npx turbo run test`).

### 6.3 Cross-Package Dependencies

If a targeted test imports from another package in the monorepo, ensure that
package is built first. Turborepo and Nx handle this automatically. For
pnpm/yarn/npm workspaces without a task runner, identify the source package
and run its build step before testing.

---

## 7. Error Recovery

Apply these recovery strategies automatically, retrying each failed step at most
once before recording the final result:

- **Missing dependencies** (`Cannot find module`, `MODULE_NOT_FOUND`): Run
  `<pm> install`, then retry.
- **Stale build artifacts** (references to deleted files, unexpected type
  mismatches): Run the `clean` script if available (`forge clean` for Foundry,
  `cargo clean` for Rust), then retry.
- **Missing environment variables**: Check for `.env.example`, `.env.test`, or
  `.env.local`. Warn the caller via the `"warnings"` array but never create or
  modify `.env` files automatically.

---

## 8. Invocation Summary

Callers invoke this skill with the following parameters:

| Parameter      | Required | Description                                           |
|----------------|----------|-------------------------------------------------------|
| `mode`         | Yes      | One of: `quick`, `test`, `coverage`, `full`, `targeted` |
| `files`        | No       | File path(s) for targeted mode                        |
| `pattern`      | No       | Test name pattern for targeted mode                   |
| `package`      | No       | Package name for monorepo scoping                     |
| `cwd`          | No       | Working directory (defaults to current directory)     |

Return the structured JSON result object as defined in section 3.
