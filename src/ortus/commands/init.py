"""ortus init <repo> — bootstrap bd plus Claude or Codex project config."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import typer

from ortus.core import output
from ortus.core.agent import BACKEND_BINARIES, BACKENDS
from ortus.core.agent_files import (
    MANAGED_FILES,
    AgentFileError,
    BlockOutcome,
    apply_block,
    duplicate_headings_message,
    duplicated_headings,
    gitignore_match,
    render_block,
)
from ortus.core.codegraph import CodeGraphMode
from ortus.core.config import (
    DEFAULT_CODEGRAPH_MODE,
    INIT_ONLY_BACKEND_MESSAGE,
    read_recorded_facts,
    read_recorded_local,
)
from ortus.core.init_render import (
    BACKEND_TEMPLATES,
    FRAMEWORK_CHOICES,
    FRAMEWORK_DEFAULTS,
    LINTER_CHOICES,
    LINTER_DEFAULTS,
    PACKAGE_MANAGER_CHOICES,
    PACKAGE_MANAGER_DEFAULTS,
    PROJECT_TYPES,
    RenderContext,
    merge_gitignore,
    merge_opencode_config,
    read_opencode_config,
    render_all,
)
from ortus.core.local_backend import (
    DEFAULT_LOCAL_BASE_URL,
    LOCAL_TABLE_BACKENDS,
    OPENCODE_CONFIG_FILE,
    OPENCODE_MCP_SERVER,
    OPENCODE_PROVIDER_ID,
    LocalConfig,
    LocalServerError,
    OpenCodeBinaryError,
    parse_local_table,
    probe_models,
    resolve_opencode_binary,
    serving_hint,
)
from ortus.core.profiles import ProfileError
from ortus.core.readiness import (
    READINESS_MEMORY_KEY,
    readiness_memory_command,
    readiness_memory_text,
)


def _bd_init(repo: Path, prefix: str | None) -> None:
    """Run `bd init --non-interactive --prefix <prefix>` inside `repo`.

    Streams bd's output straight to the operator's stdout/stderr instead of
    capturing it. Capturing can deadlock on a pipe-buffer boundary if bd writes
    more than ~64 KB before exiting, and bd's non-TTY code path can be much
    slower than its TTY one — both manifested as a multi-minute hang on a
    fresh dir. Streaming sidesteps both, and the operator gets to see bd's
    own progress lines during the init.

    `--non-interactive` is required because bd's prompt-detection only checks
    if stdin is a TTY. When ortus is invoked from a terminal, stdin is inherited
    and IS a TTY even though the operator isn't watching for bd's prompts —
    bd would otherwise block forever on hidden "Contributing to someone else's
    repo? [y/N]" prompts. Ortus verbs default to non-interactive everywhere.
    """
    args = ["bd", "init", "--non-interactive"]
    if prefix:
        args.extend(["--prefix", prefix])
    subprocess.run(args, cwd=str(repo), check=True)


def _bd_remember(repo: Path) -> None:
    """Store the readiness-contract pointer as a keyed bd memory in `repo`.

    bd injects memories at prime time, so one write here surfaces the contract
    in every later session of the repo and after every compaction. `--key`
    makes the write idempotent: bd updates a memory carrying that key in place,
    so re-running init (including `--force`) never accumulates duplicates.
    """
    subprocess.run(
        ["bd", "remember", readiness_memory_text(), "--key", READINESS_MEMORY_KEY],
        cwd=str(repo),
        check=True,
    )


def _remove_bd_claude_scaffold(repo: Path) -> None:
    """Remove the Claude *config dir* ``bd init`` creates in a non-Claude repo.

    `CLAUDE.md` is pointedly left alone. It is repo instructions, not backend
    configuration: Ortus writes its pointer block into it for every backend,
    and a repo that has taught Claude something already keeps that prose even
    when it grinds with Codex or Grok.
    """
    settings = repo / ".claude" / "settings.json"
    if settings.is_file():
        settings.unlink()
    claude_dir = repo / ".claude"
    if claude_dir.is_dir() and not any(claude_dir.iterdir()):
        claude_dir.rmdir()


def _require_tracked_agent_files(repo: Path) -> None:
    """Refuse to manage an agent file the repo has told git to forget.

    A gitignored `AGENTS.md` is a repo that treats agent instructions as
    scratch. Ortus would then write a block every collaborator's clone lacks,
    so the honest move is to fail while nothing has been written and let the
    operator decide which of the two conventions wins.
    """
    for managed in MANAGED_FILES:
        pattern = gitignore_match(repo, managed.filename)
        if pattern is None:
            continue
        output.error(
            f"{managed.filename} is gitignored by the pattern {pattern!r}",
            hint=(
                f"ortus manages {managed.filename} as tracked source; remove the "
                "pattern (or negate it with "
                f"'!{managed.filename}') and re-run"
            ),
        )
        raise typer.Exit(code=1)


def _write_agent_files(repo: Path, codegraph: str) -> None:
    """Apply the managed block to `AGENTS.md` and `CLAUDE.md`.

    Every backend gets both files: the block is about how work is tracked and
    closed in this repo, which does not change because the agent driving it
    does.
    """
    for managed in MANAGED_FILES:
        path = repo / managed.filename
        try:
            outcome = apply_block(
                path, managed.block, render_block(managed.block, codegraph=codegraph)
            )
        except AgentFileError as exc:
            output.error(
                f"{managed.filename} has a malformed ortus block: {exc}",
                hint="repair the BEGIN/END markers by hand, then re-run ortus init",
            )
            raise typer.Exit(code=1)
        if outcome is BlockOutcome.AHEAD:
            output.warn(
                f"{managed.filename} carries a block from a newer ortus; left untouched"
            )
        elif outcome is BlockOutcome.UNCHANGED:
            output.success(f"{managed.filename} ortus block already current")
        else:
            output.success(f"{outcome.value} {managed.filename} ortus block")
        # apply_block just parsed this file, so the re-read cannot fail; the
        # stale copies live outside the markers and are never cleaned up
        # automatically, so the operator hears about them while present.
        duplicates = duplicated_headings(
            path.read_text(encoding="utf-8"), path=path
        )
        if duplicates:
            output.warn(duplicate_headings_message(managed.filename, duplicates))


def _normalize_initial_branch(repo: Path, branch: str = "main") -> None:
    """Put a newly initialized repo on grind's default integration branch."""
    if not (repo / ".git").exists():
        return
    current = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if current.returncode == 0 and current.stdout.strip() != branch:
        subprocess.run(
            ["git", "branch", "-m", branch],
            cwd=repo,
            check=True,
            capture_output=True,
        )


CODEGRAPH_MODES: tuple[str, ...] = tuple(m.value for m in CodeGraphMode)
# The policy does not vary by language, but keying it like the stack flags lets
# `--codegraph` reuse `_resolve_choice`'s validation and error shape.
CODEGRAPH_CHOICES: dict[str, tuple[str, ...]] = {pt: CODEGRAPH_MODES for pt in PROJECT_TYPES}
CODEGRAPH_DEFAULTS: dict[str, str] = {pt: DEFAULT_CODEGRAPH_MODE for pt in PROJECT_TYPES}
CODEGRAPH_INIT_TIMEOUT = 900
CODEGRAPH_INSTALL_HINT = (
    "install CodeGraph (https://github.com/colbymchenry/codegraph) and re-run, "
    "or bootstrap without it: ortus init --codegraph off"
)


def _codegraph_cli() -> str | None:
    """Locate the CodeGraph CLI; the seam init tests replace."""
    return shutil.which("codegraph")


#: The backends `ortus init` can provision: those with a project config file.
#: `opencode`'s file (shared by `local`, its older name) needs the served
#: model, so `--backend all` leaves it to a pinned init and adds only the
#: commented `[local]` reference block to `.ortusrc`.
PROVISIONABLE_BACKENDS: tuple[str, ...] = tuple(
    b for b in BACKENDS if b in BACKEND_TEMPLATES
)
#: Seconds the post-render reachability probe waits for a pinned local server.
#: Short because it runs on every local init and is informational only;
#: `ortus check --backend local` owns the full probe set.
LOCAL_PROBE_TIMEOUT = 3.0
#: `%s` is the operator-served backend named on the command line.
LOCAL_MODEL_REQUIRED_MESSAGE = (
    "--backend %s needs --local-model <id as GET {base_url}/models reports it>"
)


def _backend_cli(name: str) -> str | None:
    """Locate a backend's executable; the seam init tests replace.

    Resolved through `BACKEND_BINARIES`, so `local` looks for `opencode`: it
    is that backend under its older name, not a binary of its own. For that
    pair the installer's `~/.opencode/bin` counts as well as PATH, the way
    the runner and `ortus check` resolve it, so a standard install pins as
    the run backend instead of being refused as "not on PATH".
    """
    if name in LOCAL_TABLE_BACKENDS:
        try:
            return str(resolve_opencode_binary())
        except OpenCodeBinaryError:
            return None
    return shutil.which(BACKEND_BINARIES[name])


def _codegraph_index(repo: Path, *, timeout: int = CODEGRAPH_INIT_TIMEOUT) -> None:
    """Run `codegraph init` inside `repo`; the seam init tests replace.

    Streams the CLI's own progress to the operator for the same reason
    `_bd_init` does: indexing a large repo is slow, and a silent multi-minute
    wait is indistinguishable from a hang. `check=True` turns a non-zero exit
    into CalledProcessError for the caller to translate.
    """
    subprocess.run(
        ["codegraph", "init", str(repo)], cwd=str(repo), check=True, timeout=timeout
    )


def _require_codegraph_cli(mode: str) -> None:
    """Fail `init` before it writes anything when `required` cannot be honored.

    Finishing init in a state where every subsequent grind aborts at the probe
    is worse than failing now, while the operator is present and can install
    the CLI. `auto` keeps its best-effort posture and only warns.
    """
    if mode == CodeGraphMode.OFF.value or _codegraph_cli() is not None:
        return
    if mode == CodeGraphMode.REQUIRED.value:
        output.error(
            "the codegraph CLI is not on PATH, and --codegraph=required needs it",
            hint=CODEGRAPH_INSTALL_HINT,
        )
        raise typer.Exit(code=1)
    output.warn("codegraph CLI not on PATH; --codegraph=auto will fall back to grep/Read")


def _bootstrap_codegraph(repo: Path, mode: str) -> None:
    """Build the project index so a finished `init` can run `grind` at once."""
    if mode == CodeGraphMode.OFF.value:
        output.progress("init", "CodeGraph disabled by policy (--codegraph off)")
        return
    if _codegraph_cli() is None:
        return  # auto-only: _require_codegraph_cli already warned
    if (repo / ".codegraph").is_dir():
        output.progress("init", "CodeGraph index already present; skipping codegraph init")
        return
    output.progress(
        "init", f"indexing with CodeGraph (timeout {CODEGRAPH_INIT_TIMEOUT}s)"
    )
    required = mode == CodeGraphMode.REQUIRED.value
    try:
        _codegraph_index(repo)
    except subprocess.TimeoutExpired:
        problem = f"codegraph init timed out after {CODEGRAPH_INIT_TIMEOUT}s"
    except (subprocess.CalledProcessError, OSError) as exc:
        returncode = getattr(exc, "returncode", None)
        problem = (
            f"codegraph init failed (exit {returncode})"
            if returncode is not None
            else f"codegraph init failed to run: {exc}"
        )
    else:
        if (repo / ".codegraph").is_dir():
            output.success("CodeGraph index built (.codegraph/)")
            return
        problem = "codegraph init exited 0 but left no .codegraph/ index"
    if required:
        output.error(problem, hint=CODEGRAPH_INSTALL_HINT)
        raise typer.Exit(code=1)
    output.warn(f"{problem}; continuing under --codegraph=auto")


def _summarize_backends(run_backend: str) -> None:
    """Per-backend summary for `--backend all`; nonzero if the run backend can't run.

    Static provision (the config files) has already succeeded by the time this
    runs, so all that is left to judge is the CLI-dependent tier. A missing
    sibling CLI is a recorded skip — its config sits on disk waiting for the
    install, and `ortus check` reports it — but the pinned run backend without
    its CLI means every grind would abort at launch, which is a failed init.
    """
    for b in PROVISIONABLE_BACKENDS:
        config_path = BACKEND_TEMPLATES[b]
        if b in LOCAL_TABLE_BACKENDS and b != run_backend:
            # Provisioned but unpinned: `.ortusrc` carries only the commented
            # reference block, so no CLI state can make this a failed init.
            # opencode's own file cannot be written without the model.
            state = f"{config_path} not written (it needs the served model)"
            output.warn(
                f"{b}: {state}; [local] left commented in "
                f".ortusrc — pin it with ortus init --backend {b} "
                f"--local-model <id>, then ortus check --backend {b}"
            )
            continue
        cli = _backend_cli(b)
        if cli is not None:
            output.success(f"{b}: {config_path} written; CLI at {cli}")
        elif b == run_backend:
            output.error(
                f"{b}: {config_path} written, but the {b} CLI is not on PATH "
                f"and {b} is the pinned run backend",
                hint=f"install the {b} CLI, then verify with `ortus check`",
            )
            raise typer.Exit(code=1)
        else:
            output.warn(
                f"{b}: {config_path} written; {b} CLI not on PATH — provisioned "
                "but not runnable; skipped CLI-dependent setup (verify with "
                "`ortus check` after installing)"
            )
    output.success(f'pinned run backend "{run_backend}" in .ortusrc')


def _recorded_facts(target: Path) -> dict[str, str]:
    """Facts recorded in an existing project `.ortusrc`, or {} on a first init.

    Omitted flags resolve to these instead of first-init detection, so a
    forced re-init cannot silently flip a fact the repo already pinned (the
    bd prefix most damagingly: every existing issue id carries it).
    """
    try:
        facts = read_recorded_facts(target)
    except OSError as exc:
        output.error(f"could not read {target / '.ortusrc'}: {exc}")
        raise typer.Exit(code=1)
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
        output.error(
            f"{target / '.ortusrc'} is not valid TOML: {exc}",
            hint="repair the file (or delete it and pass explicit flags), then re-run ortus init",
        )
        raise typer.Exit(code=1)
    for key, value in facts.items():
        if not isinstance(value, str):
            output.error(
                f"{target / '.ortusrc'} records {key} = {value!r}, which is not a string",
                hint=f"fix .ortusrc or pass an explicit --{key.replace('_', '-')}",
            )
            raise typer.Exit(code=1)
    return facts


def _recorded_local(target: Path) -> dict[str, Any]:
    """The `[local]` table an existing project `.ortusrc` records, or {}.

    Read apart from the init facts because the served model is not a top-level
    key: `RECORDED_INIT_KEYS` stays as it is, and a forced re-init preserves
    the table through this read instead.
    """
    try:
        return read_recorded_local(target)
    except OSError as exc:
        output.error(f"could not read {target / '.ortusrc'}: {exc}")
        raise typer.Exit(code=1)
    except ValueError as exc:  # tomllib.TOMLDecodeError subclasses ValueError
        output.error(
            f"{target / '.ortusrc'} is not valid TOML: {exc}",
            hint="repair the file (or delete it and pass explicit flags), then re-run ortus init",
        )
        raise typer.Exit(code=1)


def _resolve_local_table(
    target: Path,
    run_backend: str,
    recorded: dict[str, Any],
    model_flag: Optional[str],
    base_url_flag: Optional[str],
) -> tuple[dict[str, Any], LocalConfig | None]:
    """The `[local]` values to render, plus the validated table when it is pinned.

    Precedence per key: explicit flag, then the recorded table, then (for
    `base_url` only) the default. Under `--backend local` or `--backend
    opencode` a missing model fails here, before anything is written, and a
    table that breaks the config rules
    fails with the config's own message rather than being re-rendered from
    defaults. Under any other backend the flags are noted and ignored, but a
    recorded table is still validated: a re-init must never carry a broken
    table forward in silence.
    """
    table = dict(recorded)
    if model_flag is not None:
        table["model"] = model_flag
    if base_url_flag is not None:
        table["base_url"] = base_url_flag
    repair_hint = (
        f"fix the [local] table in {target / '.ortusrc'}, or pass "
        "--local-model / --local-base-url"
    )
    if run_backend not in LOCAL_TABLE_BACKENDS:
        given = [
            flag
            for flag, value in (
                ("--local-model", model_flag),
                ("--local-base-url", base_url_flag),
            )
            if value is not None
        ]
        if given:
            output.progress(
                "init",
                f"{' and '.join(given)} applies only to --backend local or opencode",
            )
        if recorded:
            try:
                parse_local_table(recorded)
            except ProfileError as exc:
                output.error(str(exc), hint=repair_hint)
                raise typer.Exit(code=1)
        return table, None
    if "model" not in table:
        base_url = table.get("base_url", DEFAULT_LOCAL_BASE_URL)
        output.error(
            LOCAL_MODEL_REQUIRED_MESSAGE % run_backend,
            hint=f"list the served ids with: curl {base_url}/models",
        )
        raise typer.Exit(code=1)
    table.setdefault("base_url", DEFAULT_LOCAL_BASE_URL)
    try:
        local = parse_local_table(table)
    except ProfileError as exc:
        output.error(str(exc), hint=repair_hint)
        raise typer.Exit(code=1)
    # Render what validation normalised (a trailing slash on base_url is
    # dropped) so the recorded file and the loaded config agree byte for byte.
    rendered = {
        "base_url": local.base_url,
        "model": local.model,
        "api_key_env": local.api_key_env,
    }
    return rendered, local


def _probe_local_server(local: LocalConfig) -> None:
    """Say whether the pinned server answers; never fail init over it.

    The server may legitimately be down at bootstrap, so the verdict is a
    line, not an exit code.
    """
    output.progress(
        "init",
        f"probing the local server at {local.base_url} "
        f"(timeout {LOCAL_PROBE_TIMEOUT:g}s)",
    )
    try:
        probe_models(local, timeout=LOCAL_PROBE_TIMEOUT)
    except LocalServerError as exc:
        if exc.kind == "unreachable":
            first_line = serving_hint(local).splitlines()[0]
            output.warn(
                f"local server not reachable at {local.base_url}: {exc}; "
                f"start it with: {first_line}"
            )
        else:
            output.warn(
                f"local server at {local.base_url} answered, but {exc}; "
                f"{exc.remediation}"
            )
        return
    output.success(f"local server reachable: {local.display}")


OPENCODE_REPAIR_HINT = (
    f"repair {OPENCODE_CONFIG_FILE} by hand (or delete it), then re-run ortus init"
)


def _require_mergeable_opencode_config(target: Path) -> None:
    """Refuse, before anything is written, an `opencode.json` the merge cannot take."""
    try:
        read_opencode_config(target)
    except ValueError as exc:
        output.error(str(exc), hint=OPENCODE_REPAIR_HINT)
        raise typer.Exit(code=1)


def _report_opencode_key(label: str, outcome: BlockOutcome) -> None:
    """One line per Ortus-owned `opencode.json` key: what the merge did to it."""
    if outcome is BlockOutcome.UNCHANGED:
        output.success(f"{OPENCODE_CONFIG_FILE} {label} already current")
    else:
        output.success(f"{outcome.value} {OPENCODE_CONFIG_FILE} {label}")


def _write_opencode_config(
    target: Path, local: LocalConfig, *, register_codegraph: bool
) -> bool:
    """Merge the Ortus entries into `opencode.json`; True when the file changed.

    A keyed merge rather than a render: Ortus owns the served-model provider
    and, unless CodeGraph is off for the repo, the `codegraph` MCP server;
    the operator's own providers and MCP servers in that file are theirs to
    keep across a re-init. Each owned key is reported on its own line, so an
    operator whose hand-written `codegraph` entry was rewritten reads that.
    """
    try:
        merge = merge_opencode_config(
            target, local, register_codegraph=register_codegraph
        )
    except ValueError as exc:
        output.error(str(exc), hint=OPENCODE_REPAIR_HINT)
        raise typer.Exit(code=1)
    _report_opencode_key(f"provider {OPENCODE_PROVIDER_ID}", merge.provider)
    if merge.mcp is not None:
        _report_opencode_key(f"mcp {OPENCODE_MCP_SERVER}", merge.mcp)
    return merge.changed


def _resolve_choice(
    flag_name: str,
    cli_value: Optional[str],
    project_type: str,
    choices: dict[str, tuple[str, ...]],
    defaults: dict[str, str],
) -> str:
    """Resolve one of --package-manager / --framework / --linter.

    Order: explicit CLI value (validated) → per-language default. Raises
    typer.Exit(1) with a helpful message on an invalid combination.
    """
    valid = choices[project_type]
    if cli_value is None:
        return defaults[project_type]
    if cli_value not in valid:
        output.error(
            f"{flag_name}={cli_value!r} is not valid for --project-type={project_type}",
            hint=f"choices for {project_type}: {', '.join(valid)}",
        )
        raise typer.Exit(code=1)
    return cli_value


def init(
    repo: Optional[Path] = typer.Argument(
        None,
        help="Target repo directory. Defaults to $PWD. Created if missing.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-render ortus-owned files even if .beads/ already exists."
    ),
    prefix: Optional[str] = typer.Option(
        None,
        "--prefix",
        help="bd issue-id prefix (default: target directory basename).",
    ),
    project_type: Optional[str] = typer.Option(
        None,
        "--project-type",
        help=(
            "Project type for templating (python|typescript|go|rust|polyglot). "
            "Defaults to the value recorded in .ortusrc, else polyglot."
        ),
    ),
    package_manager: Optional[str] = typer.Option(
        None,
        "--package-manager",
        help="Package manager (choices depend on --project-type; per-language default applies if omitted).",
    ),
    framework: Optional[str] = typer.Option(
        None,
        "--framework",
        help="Web/app framework (choices depend on --project-type; defaults to 'none').",
    ),
    linter: Optional[str] = typer.Option(
        None,
        "--linter",
        help="Linter (choices depend on --project-type; per-language default applies if omitted).",
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=(
            "Agent backend to configure (all|claude|codex|grok|local|opencode). Defaults "
            "to the run backend recorded in .ortusrc, else 'all', which "
            "provisions every backend and pins claude as the run backend."
        ),
    ),
    codegraph: Optional[str] = typer.Option(
        None,
        "--codegraph",
        help="CodeGraph policy to bootstrap and pin (off|auto|required); defaults to required.",
    ),
    local_model: Optional[str] = typer.Option(
        None,
        "--local-model",
        help=(
            "Model id for --backend local or opencode, as GET {base_url}/models "
            "reports it. "
            "Defaults to the local table recorded in .ortusrc; required on a "
            "first local init."
        ),
    ),
    local_base_url: Optional[str] = typer.Option(
        None,
        "--local-base-url",
        help=(
            "OpenAI-compatible base URL for --backend local or opencode. Defaults to the "
            f"recorded local value, else {DEFAULT_LOCAL_BASE_URL}."
        ),
    ),
) -> None:
    """Bootstrap a new repo with bd, backend config, .ortusrc, and AGENTS.md."""
    if project_type is not None and project_type not in PROJECT_TYPES:
        output.error(
            f"--project-type={project_type!r} is not recognized",
            hint=f"choices: {', '.join(PROJECT_TYPES)}",
        )
        raise typer.Exit(code=1)
    provision_all = backend is None or backend == "all"
    if (
        backend is not None
        and backend != "all"
        and backend not in PROVISIONABLE_BACKENDS
    ):
        output.error(
            f"--backend={backend!r} is not recognized",
            hint=f"choices: all, {', '.join(PROVISIONABLE_BACKENDS)}",
        )
        raise typer.Exit(code=1)

    target = (repo if repo is not None else Path.cwd()).resolve()
    # Precedence per recorded key: explicit flag > recorded `.ortusrc` value >
    # first-init detection default. A recorded value that fails validation is
    # an error, never a silent fall-through to detection.
    recorded = _recorded_facts(target)

    resolved_project_type = project_type or recorded.get("project_type") or "polyglot"
    if resolved_project_type not in PROJECT_TYPES:
        output.error(
            f".ortusrc records project_type = {resolved_project_type!r}, "
            "which is not recognized",
            hint=(
                f"fix {target / '.ortusrc'} or pass --project-type; "
                f"choices: {', '.join(PROJECT_TYPES)}"
            ),
        )
        raise typer.Exit(code=1)

    if backend is None and recorded.get("backend") is not None:
        recorded_backend = recorded["backend"]
        if recorded_backend == "all":
            output.error(
                f'.ortusrc records backend = "all" — {INIT_ONLY_BACKEND_MESSAGE}',
                hint=f"fix {target / '.ortusrc'} or pass --backend",
            )
            raise typer.Exit(code=1)
        if recorded_backend not in PROVISIONABLE_BACKENDS:
            output.error(
                f".ortusrc records backend = {recorded_backend!r}, "
                "which is not recognized",
                hint=(
                    f"fix {target / '.ortusrc'} or pass --backend; "
                    f"choices: {', '.join(PROVISIONABLE_BACKENDS)}"
                ),
            )
            raise typer.Exit(code=1)
        run_backend = recorded_backend
    else:
        # `all` is a provisioning breadth, never a run backend: `.ortusrc`
        # always pins a concrete value, and resolve_backend() rejects the
        # token outright.
        run_backend = "claude" if provision_all else backend

    resolved_pm = _resolve_choice(
        "--package-manager", package_manager, resolved_project_type,
        PACKAGE_MANAGER_CHOICES, PACKAGE_MANAGER_DEFAULTS,
    )
    resolved_fw = _resolve_choice(
        "--framework", framework, resolved_project_type,
        FRAMEWORK_CHOICES, FRAMEWORK_DEFAULTS,
    )
    resolved_lint = _resolve_choice(
        "--linter", linter, resolved_project_type,
        LINTER_CHOICES, LINTER_DEFAULTS,
    )
    if codegraph is None and recorded.get("codegraph") is not None:
        resolved_codegraph = recorded["codegraph"]
        if resolved_codegraph not in CODEGRAPH_MODES:
            output.error(
                f".ortusrc records codegraph = {resolved_codegraph!r}, "
                "which is not recognized",
                hint=(
                    f"fix {target / '.ortusrc'} or pass --codegraph; "
                    f"choices: {', '.join(CODEGRAPH_MODES)}"
                ),
            )
            raise typer.Exit(code=1)
    else:
        resolved_codegraph = _resolve_choice(
            "--codegraph", codegraph, resolved_project_type,
            CODEGRAPH_CHOICES, CODEGRAPH_DEFAULTS,
        )
    resolved_prefix = prefix or recorded.get("prefix") or target.name
    recorded_local = _recorded_local(target)
    local_table, local = _resolve_local_table(
        target, run_backend, recorded_local, local_model, local_base_url
    )

    # A deliberate override of a recorded fact must be visible at the
    # terminal, not only in `git diff`, and before rendering so the operator
    # sees it even if a later step fails.
    facts: list[tuple[str, str]] = [
        ("prefix", resolved_prefix),
        ("project_type", resolved_project_type),
        ("backend", run_backend),
        ("codegraph", resolved_codegraph),
    ]
    recorded_view: dict[str, Any] = dict(recorded)
    if local is not None:
        facts += [("local.model", local.model), ("local.base_url", local.base_url)]
        for key in ("model", "base_url"):
            if key in recorded_local:
                recorded_view[f"local.{key}"] = recorded_local[key]
        # Validation drops a trailing slash; the record's spelling is no change.
        if "local.base_url" in recorded_view:
            recorded_view["local.base_url"] = str(
                recorded_view["local.base_url"]
            ).rstrip("/")
    for key, resolved_value in facts:
        recorded_value = recorded_view.get(key)
        if recorded_value is not None and recorded_value != resolved_value:
            output.progress(
                "init", f"re-detected {key}: {recorded_value} -> {resolved_value}"
            )

    # Before the target directory is even created: a missing CLI under
    # `required` must fail while nothing has been written, and so must an
    # `opencode.json` the merge could only clobber.
    _require_codegraph_cli(resolved_codegraph)
    if local is not None:
        _require_mergeable_opencode_config(target)

    output.progress("init", f"target: {target}")
    target.mkdir(parents=True, exist_ok=True)

    already_initialized = (target / ".beads").is_dir()
    if already_initialized and not force:
        output.error(
            f"{target} already has a .beads/ workspace",
            hint="pass --force to re-render ortus-owned files (.claude/settings.json, .ortusrc, AGENTS.md, .gitignore)",
        )
        raise typer.Exit(code=1)

    if not already_initialized:
        output.progress("init", f"creating .beads/ workspace (prefix={resolved_prefix})")
        try:
            _bd_init(target, resolved_prefix)
        except subprocess.CalledProcessError as exc:
            # bd's output streamed directly to the operator's terminal, so the
            # error message is already on screen above this line. Just signal
            # the failure and exit.
            output.error(f"bd init failed (exit {exc.returncode})")
            raise typer.Exit(code=1)
        output.success(f"bd workspace initialized (prefix={resolved_prefix})")
        _normalize_initial_branch(target)
        if backend in {"codex", "grok", "local", "opencode"}:
            # bd currently installs its Claude integration unconditionally.
            # These files were created moments ago by this init operation, so
            # remove them before rendering the selected backend's config.
            _remove_bd_claude_scaffold(target)
    elif force:
        output.warn(f".beads/ exists; skipping bd init (--force re-renders templates only)")

    # The workspace exists on both paths above, so the memory is seeded for a
    # fresh repo and retrofitted into an existing one under --force. A failure
    # here (bd too old for `remember`, read-only workspace) costs the repo a
    # prime-time pointer, not its bootstrap, so warn and keep going.
    output.progress("init", f"storing readiness pointer (key={READINESS_MEMORY_KEY})")
    try:
        _bd_remember(target)
    except (subprocess.CalledProcessError, OSError) as exc:
        output.warn(
            f"could not store the readiness memory: {exc}\n"
            f"       add it later with: {readiness_memory_command()}"
        )
    else:
        output.success(f"readiness memory stored (key={READINESS_MEMORY_KEY})")

    # Indexing runs before rendering so a failure leaves no half-written Ortus
    # config behind.
    _bootstrap_codegraph(target, resolved_codegraph)

    # Read the repo's own ignore rules before any rendering touches
    # `.gitignore`, so the refusal reflects what the operator wrote.
    _require_tracked_agent_files(target)

    output.progress(
        "init",
        f"rendering ortus-owned files (project_type={resolved_project_type}, "
        f"package_manager={resolved_pm}, framework={resolved_fw}, linter={resolved_lint}, "
        f"codegraph={resolved_codegraph})",
    )
    ctx = RenderContext(
        prefix=resolved_prefix,
        project_type=resolved_project_type,
        package_manager=resolved_pm,
        framework=resolved_fw,
        linter=resolved_lint,
        codegraph=resolved_codegraph,
        backend=run_backend,
        local_base_url=local_table.get("base_url", DEFAULT_LOCAL_BASE_URL),
        local_model=local_table.get("model"),
        local_api_key_env=local_table.get("api_key_env"),
    )
    written = render_all(
        target, ctx, backends=PROVISIONABLE_BACKENDS if provision_all else None
    )
    for p in written:
        output.success(f"wrote {p.relative_to(target)}")
    if local is not None:
        # Pinned to opencode, or to `local`, its older name: either way the
        # served model is registered in opencode's file, and so is the
        # CodeGraph server opencode runs for the worker, unless the policy
        # is off.
        if _write_opencode_config(
            target,
            local,
            register_codegraph=resolved_codegraph != CodeGraphMode.OFF.value,
        ):
            written.append(target / OPENCODE_CONFIG_FILE)

    try:
        gitignore_outcome = merge_gitignore(target, ctx)
    except AgentFileError as exc:
        output.error(
            f".gitignore has a malformed ortus block: {exc}",
            hint="repair the BEGIN/END markers by hand, then re-run ortus init",
        )
        raise typer.Exit(code=1)
    if gitignore_outcome is BlockOutcome.AHEAD:
        output.warn(".gitignore carries a section from a newer ortus; left untouched")
    elif gitignore_outcome is BlockOutcome.UNCHANGED:
        output.success(".gitignore ortus section already current")
    else:
        output.success(f"{gitignore_outcome.value} .gitignore ortus section")

    output.progress("init", "applying managed AGENTS.md and CLAUDE.md blocks")
    _write_agent_files(target, resolved_codegraph)

    if local is not None:
        _probe_local_server(local)

    if provision_all:
        _summarize_backends(run_backend)

    output.progress(
        "init", f"done ({len(written) + 1} files, prefix={resolved_prefix})"
    )
    output.success(f"ortus init complete: {target}")
