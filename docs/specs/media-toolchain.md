# Media toolchain: yt-dlp JavaScript runtime and `clipper doctor`

Status: approved 2026-11-11. Stories: [CLP-127](https://linear.app/tengdev/issue/CLP-127)
(A) and [CLP-128](https://linear.app/tengdev/issue/CLP-128) (B), each its own PR into `dev`.

## Problem

- yt-dlp 2026.x needs a JavaScript runtime to solve YouTube's player challenges. Without
  one it falls back to the `visionos` client, prints
  `WARNING: [youtube] No supported JavaScript runtime could be found ... YouTube extraction
  without a JS runtime has been deprecated, and some formats may be missing`, and will stop
  working when that fallback is removed. The pipeline passes `--no-warnings`, so the warning
  never reaches our logs. Confirmed on 2026-11-10 on macOS (Homebrew ffmpeg 8.1.2, no Deno).
- When Deno is missing or broken, yt-dlp only warns. The missing formats surface as
  `requested format is not available`, which the pipeline maps to `NO_FORMAT` and defers for
  up to 48 hours. Nothing fails loudly.
- Nothing checks the external toolchain (ffmpeg, ffprobe, yt-dlp, Deno, node) before the
  worker starts. A missing or old binary only shows up as a failed job.

Platforms: macOS with Homebrew for development, the Linux Docker image (Debian bookworm,
`read_only: true`, `tmpfs: /tmp`, uid 10001) in production. Windows is not a target.

## Decisions

| Area | Decision | Why |
|---|---|---|
| JS runtime | **Deno**, the only runtime yt-dlp enables by default. yt-dlp's floor is 2.3.0 (`DenoJsRuntime.MIN_SUPPORTED_VERSION`); its `pin-deno` extra pins `==2.9.8`. | Node works too but needs `--no-js-runtimes --js-runtimes node` and Node 22+; the image's node is for Remotion only. Bun is deprecated upstream. |
| How Deno is installed | The **`deno` PyPI wheel** through the yt-dlp extra: `yt-dlp[default,pin-deno]` (`deno==2.9.8` for yt-dlp 2026.10.21, so Deno moves with every yt-dlp upgrade and picks up V8 security fixes). The wheel is published by the Deno organisation ([denoland/deno_pypi](https://github.com/denoland/deno_pypi), "Python redistribution of Deno binaries", MIT), about 40 MB per platform, `manylinux_2_27` and macOS arm64 builds. `uv.lock` pins the version with hashes. The binary lands in the venv `bin/` (`/opt/venv/bin/deno` in the image, `.venv/bin/deno` locally), which yt-dlp's `_find_exe` checks before `PATH`. | One mechanism for macOS and Linux, no `brew install deno`, no Dockerfile stage or digest to maintain, and CI's `test` job gets Deno for free. Verified: `uv run --with deno==2.9.5 -- deno --version` works on Apple Silicon. Fallback if the wheel ever becomes a problem: `COPY --from=denoland/deno:bin-2.9.8@sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543 /deno /usr/local/bin/deno` (multi-arch index digest). |
| Challenge scripts | The **`yt-dlp[default]` extra**, which pins `yt-dlp-ejs==0.8.0` for the locked yt-dlp (2026.10.21). No `--remote-components`. Lower bound stays at the locked version: `yt-dlp[default,pin-deno]>=2026.10.21`. | With the package installed, yt-dlp runs Deno as `deno run --ext=js --no-code-cache --no-prompt --no-remote --no-lock --node-modules-dir=none --no-config --no-npm --cached-only -`: no npm, no network, nothing fetched at runtime. yt-dlp rejects an ejs package whose major.minor differs from its vendored `VERSION`, and the `==` pin inside the extra keeps them in step on every `uv lock --upgrade-package yt-dlp` ([CLP-42](https://linear.app/tengdev/issue/CLP-42) relies on that). The `default` extra also brings `brotli`, `certifi`, `mutagen`, `pycryptodomex`, `requests`, `urllib3`, `websockets`; with `requests` installed yt-dlp switches to its Requests HTTP handler, and these packages enter the `pip-audit` gate (non-blocking on `dev`). |
| Deno's cache on the read-only rootfs | Dockerfile `ENV DENO_DIR=/tmp/deno DENO_NO_UPDATE_CHECK=1`. Add `DENO_DIR` and `DENO_NO_UPDATE_CHECK` to `CHILD_ENV_ALLOWLIST` in `ytdlp.py`. | yt-dlp starts Deno with `os.environ.copy()`, which for our child is the allowlisted environment. Without the allowlist entry Deno falls back to `$HOME/.cache/deno`, and `/home/clipper` sits on the read-only layer. Measured: Deno 2.9.8 tolerates that silently (the solver works with the cache on the read-only layer), so `DENO_DIR` is defensive: yt-dlp treats any non-empty Deno stderr as a failure, and a later Deno could warn about the cache. Same reason for `DENO_NO_UPDATE_CHECK`. `XDG_CACHE_HOME` (already allowlisted) was considered and left to [CLP-130](https://linear.app/tengdev/issue/CLP-130). |
| Runtime override env var | **None.** | `--js-runtimes` appends to the default list and Deno has the highest priority, so an override could not switch runtime without `--no-js-runtimes`. The binary path is fixed in the image and found through the venv locally. An operator knob over which binary runs untrusted JavaScript is not worth having. |
| ffmpeg binary override | **None.** `ffmpeg`/`ffprobe` stay bare names on `PATH`. | yt-dlp spawns its own ffmpeg for `--download-sections` and merges, so an ffmpeg-path environment variable would not give "one place" without also passing `--ffmpeg-location`, and absolute paths in argv would break the test fakes that dispatch on `args[0] == "ffprobe"`. |
| Preflight | New CLI command **`clipper doctor`** that checks every external tool and exits non-zero when one is missing, too old, or (for Deno) cannot run the challenge solver in our child environment. CI runs it inside the read-only, non-root container. | Fails fast at deploy instead of after 48 hours of deferrals. `deno --version` alone proves nothing about the read-only cache dir, so the check runs the real solver scripts. |
| ffmpeg version drift | Out of scope. macOS has ffmpeg 8.1.2, Debian bookworm ships `7:5.1.9-0+deb12u1`. `doctor` prints the version so the gap is visible. | Pinning a static ffmpeg 8 build in the image is a separate decision; only worth it if numbers (loudnorm, scene cuts) differ between local tests and production. |
| `--no-warnings` | Keep. | Dropping it would spam logs on every yt-dlp call. `doctor` plus the slow regression test cover the runtime warning. |

### Security note

yt-dlp runs Deno with no `--allow-*` flags, so Deno denies file, network, environment, child
process and FFI access by default; `--no-prompt` turns a permission request into an error;
`--no-remote`, `--no-npm` and `--cached-only` block module fetches; `--no-config` blocks
permission sets from a config file. yt-dlp copies proxy credentials into `HTTP(S)_PROXY` for
Deno, but the script cannot read them without `--allow-env`. The residual risk is a V8
escape that runs code as uid 10001, the same blast radius as ffmpeg decoding untrusted media
([CLP-71](https://linear.app/tengdev/issue/CLP-71) tracks isolating decoders). `cap_drop: ALL`
and `no-new-privileges` only limit the damage after such an escape. yt-dlp's
`--extractor-args "youtube-ejs:jitless=true"` (`--v8-flags=--jitless`) would remove the JIT
attack surface at an unmeasured CPU cost; not adopted.

## Stories and acceptance criteria

### A. yt-dlp runs with a JavaScript runtime ([CLP-127](https://linear.app/tengdev/issue/CLP-127))

Files: `pyproject.toml`, `uv.lock`, `Dockerfile`, `src/clipper/pipeline/ytdlp.py`,
`tests/test_ytdlp.py`, `.github/workflows/ci.yml`, `README.md`, `docs/DEPLOYMENT.md`.

- [ ] `pyproject.toml` depends on `yt-dlp[default,pin-deno]>=2026.10.21`; `uv.lock` contains the `yt-dlp-ejs` version the locked yt-dlp requires (0.8.0 today) and `deno` wheels for macOS arm64, Linux x86_64 and Linux aarch64.
- [ ] After `uv sync`, `.venv/bin/deno --version` reports a version `>= 2.9.8`; in the image, `/opt/venv/bin/deno` does the same.
- [ ] The Dockerfile sets `DENO_DIR=/tmp/deno` and `DENO_NO_UPDATE_CHECK=1`.
- [ ] `CHILD_ENV_ALLOWLIST` includes `DENO_DIR` and `DENO_NO_UPDATE_CHECK`; other `DENO_*` variables (for example `DENO_AUTH_TOKENS`) stay stripped.
- [ ] CI Docker smoke test runs JavaScript with Deno inside the read-only, uid 10001 container with yt-dlp's sandbox flags and fails on any stderr.
- [ ] A `@pytest.mark.slow` test (gated by `CLIPPER_SLOW_TESTS=1`; it fails rather than skips without Deno, since `uv sync` always installs it) runs the real yt-dlp `probe` through `run_command` with `--no-warnings` replaced by `-v` and an empty env (no developer cookies or proxy), and asserts: stderr contains `Solving JS challenges using deno`; stderr contains none of `No supported JavaScript runtime`, `n challenge solving failed`, `Signature solving failed`, `Error solving`.
- [ ] `README.md` quickstart: `brew install ffmpeg` is the only system install on macOS; `uv sync` brings yt-dlp, yt-dlp-ejs and Deno. `docs/DEPLOYMENT.md` gets a "yt-dlp" section covering `CLIPPER_YTDLP_COOKIES`, `CLIPPER_YTDLP_PROXY`, the Deno wheel, `DENO_DIR` and the security note above.
- [ ] Live check (recorded on the Linear issue): `uv run python -m yt_dlp --ignore-config --no-playlist -v -F -- <public video>` on macOS prints `Solving JS challenges using deno` and no JS-runtime warning; the same probe inside the image (`docker run --read-only --tmpfs /tmp --user 10001:10001 ...`) also succeeds. Record the probe's wall time and Deno's peak memory inside the container (the monitor runs under `mem_limit: 512m`); raise the limit in `docker-compose.yml` if needed.

Tests (unit): allowlist pass-through and stripping; the ejs package and the Deno binary
present in the environment. Existing tests keep passing unchanged.

### B. `clipper doctor` preflight ([CLP-128](https://linear.app/tengdev/issue/CLP-128))

Files: new `src/clipper/doctor.py`, new `tests/test_doctor.py`, `src/clipper/cli.py`,
`tests/test_cli.py`, `.github/workflows/ci.yml`, `docs/DEPLOYMENT.md`,
`docs/specs/media-toolchain.md`.

- [ ] `clipper doctor` needs no config file and no database. It prints one line per check (`ok` / `warn` / `fail`, name, version, resolved path), then a summary line, and exits 0 when nothing failed, 1 otherwise. Checks:
  - `ffmpeg` and `ffprobe`: found on `PATH` (`shutil.which`) and version parsed from `-version` (parser tested against the 5.1.9 and 8.1.2 banners).
  - ffmpeg filters `loudnorm`, `ebur128`, `silencedetect`, `scale`, `crop` present in `ffmpeg -hide_banner -filters`; a missing filter is named.
  - `yt-dlp`: installed, version printed. `yt-dlp-ejs`: installed and its major.minor equals yt-dlp's vendored script version (`yt_dlp.extractor.youtube.jsc._builtin.vendor._info.VERSION`); missing or mismatched fails with the hint `uv sync` (yt-dlp[default]). The version is the `yt_dlp_ejs.version` module attribute, which yt-dlp's `_pypackage_source` reads. Like yt-dlp's `_get_script`, the check also compares the sha3-512 of the lib and core scripts with the vendored `HASHES['yt.solver.lib.min.js']` and `HASHES['yt.solver.core.min.js']`; a mismatch fails with the same hint and prints no hash values.
  - Deno: found (venv `bin/` first, then `PATH`, the same order yt-dlp uses), version `>= 2.3.0`, **and** a solver smoke run: Deno is started through `run_command` (so with the allowlisted environment) with yt-dlp's exact flags, the `yt_dlp_ejs` lib and core scripts on stdin followed by `Object.assign(globalThis, lib); console.log(typeof jsc)`; requires exit 0, stdout `function`, empty stderr. Failure names the platform-specific fix (`uv sync` locally; rebuild the image in production). The solver run happens only when the `yt-dlp-ejs` check passed; otherwise Deno fails with "solver skipped" and is never fed scripts that yt-dlp would refuse.
  - `node --version` found: `fail` when `CLIPPER_RENDER_DIR` is set (renders are expected), `warn` otherwise.
- [ ] Output never includes environment values or child stderr; a failed run prints the exit code only. Printed versions must be short tokens without control characters, and `format_check` replaces any unprintable character in a line with `?`, so a path from `PATH` or a version from package metadata cannot inject terminal escapes.
- [ ] All checks are unit-tested with an injected runner, an injected `which`, an injected package-version lookup, and an explicit `platform`. No live binaries needed. `doctor.py` types the runner with its own alias, `Runner = Callable[..., RunResult]`, because the Deno solver run passes `stdin_path` and the `Runner` protocol in `ytdlp.py` does not declare it; the default is still `run_command`. One `skipif(not HAS_FFMPEG)` test runs the real checks on the developer machine and asserts the ffmpeg, ffprobe and filter checks are ok (it does not assert the exit code). A second live test always runs the real Deno solver check and requires it to be ok, without a skip, because `uv sync` always installs the pin-deno wheel.
- [ ] CI Docker smoke test runs `docker run --rm --read-only --tmpfs /tmp --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges:true clipper:ci doctor` before the render smoke test; a non-zero exit fails the job. The step also passes `--network none --pids-limit 512 --memory 1g` (`--network none` proves the solver needs no network) and has `timeout-minutes: 5`.
- [ ] `tests/test_cli.py` `COMMANDS` includes `doctor`.
- [ ] `docs/DEPLOYMENT.md` runbook: run `docker compose run --rm --no-deps worker doctor` after every image update and before filing a download bug.

## Out of scope and follow-ups

- Pinning ffmpeg 8 in the Docker image (see Decisions).
- The nightly rebuild workflow itself ([CLP-42](https://linear.app/tengdev/issue/CLP-42)).
- Startup preflight in `run-worker` / `run-monitor`: [CLP-129](https://linear.app/tengdev/issue/CLP-129).
- yt-dlp's own cache is unwritable in the container, so solved challenges are never cached:
  [CLP-130](https://linear.app/tengdev/issue/CLP-130).
- Windows support.

## Order

A first (protects production downloads), then B. Separate PRs into `dev`, one per story,
following [BRANCHING.md](../BRANCHING.md).
