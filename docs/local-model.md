# Serving a local model

The `opencode` backend drives a model you host yourself. Nothing here changes
how [grind](grind.md) works; it changes only where the tokens come from.

## What this backend assumes

- **You run the model; Ortus drives it.** A GGUF is not an agent. It can't run
  tools on its own, so Ortus points the [opencode](https://opencode.ai) CLI at a
  server you host. opencode drives the worker (`opencode run`), the same as the
  Claude and Codex backends drive their CLIs. Ortus never starts, stops, or
  manages the model server. That part is yours.
- **The wire is the OpenAI chat-completions API.** The server answers
  `GET /v1/models` with the model listed and executes function calls on
  `POST /v1/chat/completions`. llama.cpp's `llama-server` does. The backend is
  verified against opencode 1.18.27 driving `llama-server`.
- **The default endpoint is `http://127.0.0.1:8080/v1`.** That is llama-server
  on localhost. A different port or a remote host is one config value.
- **The reference model in these docs is `unsloth/Qwen3.8-27B-GGUF:Q4_K_M`.** It
  is a stock Qwen3-27B build. The backend does not care which model you serve.
  We ran more than one Qwen3-27B GGUF through it with no code change, and
  swapping models touches only config. Prefer a text-only GGUF for coding work,
  since a multimodal build spends VRAM on a vision tower the worker never uses.
- **CodeGraph runs as an opencode MCP server, client-side.** opencode launches
  `codegraph serve --mcp` itself and hands the model each tool as an ordinary
  function, so nothing sits between the worker and the server.
- **Two postures.** An implement worker runs with every tool auto-approved and
  nothing wrapping the process, so run it on a host you are willing to let it
  write to. A verify worker runs read-only, with the `bash`, `edit`, and `write`
  tools removed (see [Posture](#posture) below).

## Serving command

This is the same command `ortus check --backend opencode` prints as the
remediation when a probe fails:

```bash
llama-server -hf unsloth/Qwen3.8-27B-GGUF:Q4_K_M --jinja --ctx-size 32768 --flash-attn on --host 127.0.0.1 --port 8080
```

Add `--cache-type-k q8_0 --cache-type-v q8_0` to halve the memory the 32k KV
cache takes. `--jinja` is required for tool calling. Without it the model
narrates the call as text instead of making it. No check row proves tool
calling; the worker's own CodeGraph handshake does, on the wire opencode
actually uses, so a server started without that flag shows up as a worker that
never makes its first tool call and is stopped at the handshake gate. The
context size is a floor, not a suggestion. A worker prompt plus CodeGraph tool
output does not fit a smaller window, and `ortus check` warns when the server
reports less than 32768.

`model` must be the id `GET /v1/models` reports. For llama-server that is the
model path (`unsloth/Qwen3.8-27B-GGUF:Q4_K_M` above) or an `--alias` you pass.
Then pin and verify:

```bash
ortus init . --backend opencode --local-model unsloth/Qwen3.8-27B-GGUF:Q4_K_M
ortus check . --backend opencode
```

Run `ortus init --backend opencode` with no `--local-model` at a terminal and it
lists what the server serves and lets you pick one by number; piped or in a
script it prints the list and exits, so pass `--local-model` there.

## A different port, host, or a keyed server

The endpoint is one value. The default is `http://127.0.0.1:8080/v1`. Override
it at init with `--local-base-url`, or by hand in the `[local]` table's
`base_url`:

```bash
# a different local port
ortus init . --backend opencode --local-base-url http://127.0.0.1:9000/v1 --local-model unsloth/Qwen3.8-27B-GGUF:Q4_K_M
# a model served on another machine on your network
ortus init . --backend opencode --local-base-url http://gpubox.lan:8080/v1 --local-model unsloth/Qwen3.8-27B-GGUF:Q4_K_M
```

For a server behind an API key, set `api_key_env` in `[local]` to the *name* of
an environment variable, never the key itself. opencode reads that variable at
launch, and Ortus writes only the reference `"apiKey": "{env:NAME}"` into
`opencode.json`. A grind worker reaches the server over that `base_url`, so a
remote host has to be reachable from wherever grind runs, and a non-loopback
host should be one you trust with the worker's traffic.

## What init writes

Init writes the `[local]` table into `.ortusrc` and merges two Ortus-owned
entries into the project's `opencode.json`, creating the file when it is absent:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ortuslocal": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ortus local model",
      "options": { "baseURL": "http://127.0.0.1:8080/v1" },
      "models": { "unsloth/Qwen3.8-27B-GGUF:Q4_K_M": {} }
    }
  },
  "mcp": {
    "codegraph": {
      "type": "local",
      "command": ["codegraph", "serve", "--mcp"],
      "enabled": true
    }
  }
}
```

The provider entry is the keyless shape a llama-server accepts. When `[local]`
sets `api_key_env`, the entry gains `"apiKey": "{env:NAME}"`, opencode's own
reference that it substitutes at startup, so the key itself never enters the
file. The `mcp.codegraph` entry is the whole CodeGraph registration for this
backend. opencode launches the server from it and runs every call client-side,
so there is no shim and no per-launch override, and under `codegraph = "off"`
init leaves the `mcp` table alone. The merge is keyed. Re-running init rewrites
exactly those two entries when they have drifted, keeps every other provider,
server, and key in the file in its original order, writes nothing when both are
already current, and refuses a file that is not a JSON object before anything
else is touched.

## What `ortus check` proves

`ortus check --backend opencode` prints the `opencode` binary row, the
`opencode.json` row, and six rows after them. The binary row looks on PATH and
then in `~/.opencode/bin`, where the opencode installer puts the executable and
a non-login shell's PATH does not reach; the worker is launched by that same
resolved absolute path, so a green row is a launchable worker, and a miss names
both fixes: add the directory to PATH, or install opencode. `[local]` validates
the table and, when `api_key_env` is set, that the variable is exported; the row
shows the variable's name and never its value. `opencode provider` compares the
`ortuslocal` entry with the table fact by fact, checking `baseURL`, the served
model among `models`, and the key reference, then names the re-init that repairs
a drift, so a model option you added by hand survives. `opencode endpoint`
requests `/v1/models` and fails if the server is down, demands a key, or does
not list `model`. `opencode mcp` requires an enabled `codegraph` entry in the
`mcp` table and prints the exact JSON to add when it is missing.
`opencode posture` resolves the `permission` table with `OPENCODE_PERMISSION`
from your shell merged over it, the way opencode does at startup, and fails when
`edit`, `write`, or `bash` would be anything but `allow` for an implement
worker, because a denial exported in the shell would quietly cripple every
implement run; the same row reports the verify denial. `opencode context` reads
`n_ctx` from llama-server's `/props` and is informational. It warns below 32768
and never fails the check. No row launches opencode.

`ortus grind` repeats the binary resolution and the endpoint probe at startup,
before it takes the lock or launches a worker, so a missing install or a server
that has gone away fails fast with the same remediation and no issue is claimed,
and its CodeGraph probe treats a missing or disabled `mcp.codegraph` entry as
CodeGraph unavailable, which under `required` stops the run before any issue is
claimed.

## Posture

opencode has no OS sandbox of its own, and its permissions are per tool, not per
file. An implement worker runs `opencode run` headless with every tool
auto-approved and nothing wrapping the process; the OS sandbox `ortus grind`
requires still gates the launch but does not enclose this worker, so run an
unattended local model on a host you are willing to let it write to.

The verify posture is the tool-level denial the runner exports per launch as
`OPENCODE_PERMISSION={"bash": "deny", "edit": "deny", "write": "deny"}`.
opencode drops those three tools from the model's toolset entirely, bash
included, which is the one tool a permission table cannot otherwise contain
because an allowed bash writes through a redirect. A verifier therefore holds
nothing that can touch the tree and needs no read-only root on top. The same
denial is exported once more at agent scope, as
`OPENCODE_CONFIG_CONTENT={"agent": {"build": {"permission": ...}}}` merged over
any document already in that variable, because opencode resolves permissions as
an ordered ruleset in which agent-scoped rows follow the global ones and the
last match wins, so an `agent.build.permission` allow in the project or user
config would otherwise hand the write tools back to the verifier. A value in
that variable that is not a JSON object is refused before launch rather than
replaced. Denied tools simply never appear in the session; the log records no
denial event.

## Wall clock

Local decode is slower than a hosted model, and a worker runs the work spec's
checks inside its window. Raise `--worker-timeout` above the 5400s default for a
large model, for example
`ortus grind . --backend opencode --worker-timeout 10800`; the default is
unchanged for the other backends.

## What to expect

A 27B-class local model can drive the whole loop. We ran more than one
Qwen3-27B GGUF through the backend, config swap only, no code change. In each
run, on llama-server driven by opencode, the worker claimed a fresh hello-world
issue, implemented it with bash/write/read and CodeGraph queries (no grep
fallback), verified under the read-only denial, and closed it with a working
commit, unaided. The model's own turn loop carries each phase, not `/goal`, and
a large context window (`--ctx-size 131072` on the serving side) holds the
worker's context across its tool calls. Expect minutes per phase: a small local
model here is slow but correct, so size `--worker-timeout` for the model, not
the task.
