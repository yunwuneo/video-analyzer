"""Batch runner for the video-analyzer CLI.

The batch runner is configured through environment variables so it can be
reused across local machines, scheduled tasks, and ad-hoc shells without
embedding secrets or workstation paths in source code.
"""

from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import site
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Iterable, Optional, Sequence


ENV_PREFIX = "VIDEO_ANALYZER_BATCH_"
ANALYSIS_FILENAME = "analysis.json"
DEFAULT_EXTENSIONS = (".mp4", ".mkv", ".avi", ".ts")
DEFAULT_ENV_FILES = (".env", ".env.batch", "video-analyzer-batch.env")
DEFAULT_LANGUAGE = "zh"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


ENV_HELP = f"""\
Batch video analysis is configured with .env files and environment variables.

Configuration loading order:
  1. .env, .env.batch, video-analyzer-batch.env from the current directory
  2. File paths listed in {ENV_PREFIX}ENV_FILE
  3. Existing system environment variables override file values

{ENV_PREFIX}ENV_FILE may contain one or more paths separated by os.pathsep
(";" on Windows, ":" on macOS/Linux).

Required:
  {ENV_PREFIX}INPUT_DIR          Source video root directory.
  {ENV_PREFIX}OUTPUT_DIR         Result root directory. analysis.json files are
                                 mirrored here as <video_stem>_analysis.json.

Optional batch settings:
  {ENV_PREFIX}EXTENSIONS         Comma separated extensions.
                                 Default: .mp4,.mkv,.avi,.ts
  {ENV_PREFIX}TEMP_OUTPUT_DIR    Working output directory passed to video-analyzer.
                                 Default: ./output
  {ENV_PREFIX}ENV_FILE           Additional .env file path(s) to load.
  {ENV_PREFIX}COMMAND            Analyzer command. Default: video-analyzer
  {ENV_PREFIX}SKIP_EXISTING      Skip videos whose result already exists.
                                 Default: false
  {ENV_PREFIX}OVERWRITE_EXISTING Replace existing result files. Default: true
  {ENV_PREFIX}CLEAN_TEMP         Clean temporary analyzer files after each video.
                                 Default: true
  {ENV_PREFIX}ADD_CUDA_DLL_DIRS  On Windows, add NVIDIA cublas/cudnn DLL dirs
                                 from the active environment. Default: true

Optional video-analyzer settings:
  {ENV_PREFIX}CONFIG             Value for --config
  {ENV_PREFIX}CLIENT             Value for --client
  {ENV_PREFIX}OLLAMA_URL         Value for --ollama-url
  {ENV_PREFIX}API_KEY            Value for --api-key
  {ENV_PREFIX}API_URL            Value for --api-url
  {ENV_PREFIX}MODEL              Value for --model
  {ENV_PREFIX}DURATION           Value for --duration
  {ENV_PREFIX}KEEP_FRAMES        Add --keep-frames when true
  {ENV_PREFIX}WHISPER_MODEL      Value for --whisper-model
  {ENV_PREFIX}START_STAGE        Value for --start-stage
  {ENV_PREFIX}MAX_FRAMES         Value for --max-frames
  {ENV_PREFIX}LOG_LEVEL          Value for --log-level
  {ENV_PREFIX}PROMPT             Value for --prompt
  {ENV_PREFIX}LANGUAGE           Value for --language. Default: {DEFAULT_LANGUAGE}
  {ENV_PREFIX}DEVICE             Value for --device
  {ENV_PREFIX}TEMPERATURE        Value for --temperature
  {ENV_PREFIX}EXTRA_ARGS         Additional shell-style arguments.

Example:
  set {ENV_PREFIX}INPUT_DIR=C:\\Users\\me\\videos
  set {ENV_PREFIX}OUTPUT_DIR=.\\dy
  set {ENV_PREFIX}CLIENT=openai_api
  set {ENV_PREFIX}API_KEY=sk-...
  set {ENV_PREFIX}API_URL=https://api.openai.com/v1
  set {ENV_PREFIX}MODEL=gpt-4o
  set {ENV_PREFIX}DEVICE=cuda
  set {ENV_PREFIX}MAX_FRAMES=20
  video-analyzer-batch
"""


class ConfigError(ValueError):
    """Raised when required batch configuration is missing or invalid."""


@dataclass(frozen=True)
class BatchConfig:
    input_dir: Path
    output_dir: Path
    temp_output_dir: Path
    extensions: tuple[str, ...]
    analyzer_command: tuple[str, ...]
    skip_existing: bool
    overwrite_existing: bool
    clean_temp: bool
    add_cuda_dll_dirs: bool
    config: Optional[str]
    client: Optional[str]
    ollama_url: Optional[str]
    api_key: Optional[str]
    api_url: Optional[str]
    model: Optional[str]
    duration: Optional[str]
    keep_frames: bool
    whisper_model: Optional[str]
    start_stage: Optional[str]
    max_frames: Optional[str]
    log_level: Optional[str]
    prompt: Optional[str]
    language: Optional[str]
    device: Optional[str]
    temperature: Optional[str]
    extra_args: tuple[str, ...]


@dataclass
class BatchState:
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    current_index: int = 0
    current_video: str = ""
    status: str = "Starting"
    started_at: float = 0.0
    video_started_at: float = 0.0

    @property
    def processed(self) -> int:
        return self.completed + self.failed + self.skipped


def _env(
    name: str,
    default: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> Optional[str]:
    source = os.environ if env is None else env
    value = source.get(f"{ENV_PREFIX}{name}")
    if value is None or value == "":
        return default
    return value


def _read_bool(name: str, default: bool, env: dict[str, str]) -> bool:
    value = _env(name, env=env)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{ENV_PREFIX}{name} must be a boolean value, got {value!r}")


def _read_required_path(name: str, env: dict[str, str]) -> Path:
    value = _env(name, env=env)
    if not value:
        raise ConfigError(f"Missing required environment variable: {ENV_PREFIX}{name}")
    return Path(value).expanduser().resolve()


def _read_path(name: str, default: str, env: dict[str, str]) -> Path:
    value = _env(name, default, env=env)
    return Path(value).expanduser().resolve()


def _read_extensions(env: dict[str, str]) -> tuple[str, ...]:
    raw = _env("EXTENSIONS", ",".join(DEFAULT_EXTENSIONS), env=env)
    extensions = []
    for item in raw.split(","):
        ext = item.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        extensions.append(ext)
    if not extensions:
        raise ConfigError(f"{ENV_PREFIX}EXTENSIONS did not contain any extensions")
    return tuple(dict.fromkeys(extensions))


def _split_shell_words(value: Optional[str], default: str = "") -> tuple[str, ...]:
    raw = value if value is not None else default
    try:
        return tuple(shlex.split(raw))
    except ValueError as exc:
        raise ConfigError(f"Could not parse shell arguments {raw!r}: {exc}") from exc


def parse_dotenv_line(line: str, path: Path, line_number: int) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()
    if "=" not in stripped:
        raise ConfigError(f"{path}:{line_number}: expected KEY=value")

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not DOTENV_KEY_RE.match(key):
        raise ConfigError(f"{path}:{line_number}: invalid key {key!r}")

    return key, parse_dotenv_value(raw_value.strip(), path, line_number)


def parse_dotenv_value(raw_value: str, path: Path, line_number: int) -> str:
    if not raw_value:
        return ""

    quote = raw_value[0]
    if quote in {"'", '"'}:
        value_chars = []
        escaped = False
        for index, char in enumerate(raw_value[1:], start=1):
            if quote == '"' and escaped:
                value_chars.append(
                    {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "\\": "\\",
                        '"': '"',
                        "$": "$",
                    }.get(char, char)
                )
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                remainder = raw_value[index + 1 :].strip()
                if remainder and not remainder.startswith("#"):
                    raise ConfigError(
                        f"{path}:{line_number}: unexpected text after quoted value"
                    )
                return "".join(value_chars)
            value_chars.append(char)
        raise ConfigError(f"{path}:{line_number}: unterminated quoted value")

    if " #" in raw_value:
        raw_value = raw_value.split(" #", 1)[0]
    return raw_value.strip()


def load_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                parsed = parse_dotenv_line(line, path, line_number)
                if parsed is None:
                    continue
                key, value = parsed
                values[key] = value
    except OSError as exc:
        raise ConfigError(f"Could not read env file {path}: {exc}") from exc
    return values


def split_env_file_paths(raw: str) -> list[Path]:
    paths = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            paths.append(Path(item).expanduser().resolve())
    return paths


def build_effective_env(base_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    system_env = dict(os.environ if base_env is None else base_env)
    effective_env: dict[str, str] = {}

    dotenv_paths = [
        Path(name).resolve() for name in DEFAULT_ENV_FILES if Path(name).exists()
    ]
    env_file_value = system_env.get(f"{ENV_PREFIX}ENV_FILE")
    if env_file_value:
        dotenv_paths.extend(split_env_file_paths(env_file_value))

    seen_paths: set[Path] = set()
    for path in dotenv_paths:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        effective_env.update(load_dotenv_file(path))

    effective_env.update(system_env)
    return effective_env


def load_config_from_env() -> BatchConfig:
    env = build_effective_env()
    command = _split_shell_words(_env("COMMAND", env=env), "video-analyzer")
    if not command:
        raise ConfigError(f"{ENV_PREFIX}COMMAND cannot be empty")

    return BatchConfig(
        input_dir=_read_required_path("INPUT_DIR", env),
        output_dir=_read_required_path("OUTPUT_DIR", env),
        temp_output_dir=_read_path("TEMP_OUTPUT_DIR", "./output", env),
        extensions=_read_extensions(env),
        analyzer_command=command,
        skip_existing=_read_bool("SKIP_EXISTING", False, env),
        overwrite_existing=_read_bool("OVERWRITE_EXISTING", True, env),
        clean_temp=_read_bool("CLEAN_TEMP", True, env),
        add_cuda_dll_dirs=_read_bool("ADD_CUDA_DLL_DIRS", True, env),
        config=_env("CONFIG", env=env),
        client=_env("CLIENT", env=env),
        ollama_url=_env("OLLAMA_URL", env=env),
        api_key=_env("API_KEY", env=env),
        api_url=_env("API_URL", env=env),
        model=_env("MODEL", env=env),
        duration=_env("DURATION", env=env),
        keep_frames=_read_bool("KEEP_FRAMES", False, env),
        whisper_model=_env("WHISPER_MODEL", env=env),
        start_stage=_env("START_STAGE", env=env),
        max_frames=_env("MAX_FRAMES", env=env),
        log_level=_env("LOG_LEVEL", env=env),
        prompt=_env("PROMPT", env=env),
        language=_env("LANGUAGE", DEFAULT_LANGUAGE, env=env),
        device=_env("DEVICE", env=env),
        temperature=_env("TEMPERATURE", env=env),
        extra_args=_split_shell_words(_env("EXTRA_ARGS", env=env)),
    )


def configure_text_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_windows_cuda_dll_dirs(enabled: bool) -> None:
    if not enabled or not hasattr(os, "add_dll_directory"):
        return

    candidates = []
    for site_packages in site.getsitepackages():
        root = Path(site_packages)
        candidates.extend(
            [
                root / "nvidia" / "cublas" / "bin",
                root / "nvidia" / "cudnn" / "bin",
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            os.add_dll_directory(str(candidate))


def discover_videos(input_dir: Path, extensions: Sequence[str]) -> list[Path]:
    extension_set = {ext.lower() for ext in extensions}
    videos = [
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in extension_set
    ]
    return sorted(videos, key=lambda path: str(path.relative_to(input_dir)).lower())


def target_path_for(config: BatchConfig, video: Path) -> Path:
    relative_path = video.relative_to(config.input_dir)
    return config.output_dir / relative_path.parent / f"{video.stem}_analysis.json"


def build_analyzer_command(config: BatchConfig, video: Path) -> list[str]:
    cmd = [*config.analyzer_command, str(video)]
    cmd.extend(config.extra_args)

    option_pairs = [
        ("--config", config.config),
        ("--client", config.client),
        ("--ollama-url", config.ollama_url),
        ("--api-key", config.api_key),
        ("--api-url", config.api_url),
        ("--model", config.model),
        ("--duration", config.duration),
        ("--whisper-model", config.whisper_model),
        ("--start-stage", config.start_stage),
        ("--max-frames", config.max_frames),
        ("--log-level", config.log_level),
        ("--prompt", config.prompt),
        ("--language", config.language),
        ("--device", config.device),
        ("--temperature", config.temperature),
    ]
    for flag, value in option_pairs:
        if value is not None:
            cmd.extend([flag, str(value)])

    if config.keep_frames:
        cmd.append("--keep-frames")

    cmd.extend(["--output", str(config.temp_output_dir)])
    return cmd


def redact_command(cmd: Sequence[str]) -> str:
    redacted = []
    hide_next = False
    for item in cmd:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        redacted.append(item)
        if item == "--api-key":
            hide_next = True
    return shlex.join(redacted)


def sanitize_line(line: str) -> str:
    line = ANSI_ESCAPE_RE.sub("", line)
    line = line.replace("\r", "\n").replace("\t", "    ")
    line = CONTROL_RE.sub("", line)
    return line.strip()


def looks_like_error(line: str) -> bool:
    upper = line.upper()
    return any(
        token in upper
        for token in (
            "ERROR",
            "CRITICAL",
            "TRACEBACK",
            "EXCEPTION",
            "FAILED",
            "FAILURE",
        )
    )


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class Dashboard:
    def __init__(self, state: BatchState) -> None:
        self.state = state
        self.logs: Deque[str] = deque(maxlen=300)
        self.errors: Deque[str] = deque(maxlen=120)
        self.interactive = sys.stdout.isatty()
        self.last_render = 0.0

    def __enter__(self) -> "Dashboard":
        if self.interactive:
            sys.stdout.write("\033[?1049h\033[?25l\033[2J\033[H")
            sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.interactive:
            self.render(force=True)
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
            print(
                "Video Analyzer Batch complete: "
                f"ok={self.state.completed}, "
                f"failed={self.state.failed}, "
                f"skipped={self.state.skipped}, "
                f"total={self.state.total}"
            )

    def add_log(self, line: str) -> None:
        line = sanitize_line(line)
        if not line:
            return
        for part in line.splitlines():
            part = sanitize_line(part)
            if not part:
                continue
            self.logs.append(part)
            if looks_like_error(part):
                self.errors.append(part)
            if not self.interactive:
                print(part)

    def add_error(self, line: str) -> None:
        line = sanitize_line(line)
        if not line:
            return
        self.errors.append(line)
        self.logs.append(line)
        if not self.interactive:
            print(line, file=sys.stderr)

    def render(self, force: bool = False) -> None:
        if not self.interactive:
            return

        now = time.monotonic()
        if not force and now - self.last_render < 0.15:
            return
        self.last_render = now

        size = shutil.get_terminal_size((110, 32))
        width = max(60, size.columns)
        height = max(20, size.lines)
        rows = self._build_rows(width, height)
        rows = rows[:height]
        if len(rows) < height:
            rows.extend([""] * (height - len(rows)))

        screen = ["\033[H"]
        for row in rows:
            screen.append(self._fit(row, width))
            screen.append("\033[K\n")
        sys.stdout.write("".join(screen))
        sys.stdout.flush()

    def _build_rows(self, width: int, height: int) -> list[str]:
        rows = []
        now = time.monotonic()
        total = max(self.state.total, 1)
        processed = self.state.processed
        percent = processed / total
        bar_width = min(36, max(12, width - 64))
        filled = int(bar_width * percent)
        bar = "#" * filled + "-" * (bar_width - filled)

        rows.append("Video Analyzer Batch")
        rows.append("=" * min(width, 80))
        rows.append(
            f"Progress [{bar}] {processed}/{self.state.total} "
            f"({percent * 100:5.1f}%) | ok {self.state.completed} | "
            f"failed {self.state.failed} | skipped {self.state.skipped}"
        )
        rows.append(
            f"Status: {self.state.status} | elapsed {format_duration(now - self.state.started_at)} "
            f"| current {format_duration(now - self.state.video_started_at)}"
        )
        rows.append(f"Current: {self.state.current_video or '-'}")
        rows.append("")

        error_height = max(5, min(10, height // 4))
        log_height = max(5, height - len(rows) - error_height - 4)

        rows.append("Latest logs")
        rows.append("-" * min(width, 80))
        rows.extend(self._tail_wrapped(self.logs, width, log_height))
        rows.append("")
        rows.append("Latest errors")
        rows.append("-" * min(width, 80))
        if self.errors:
            rows.extend(self._tail_wrapped(self.errors, width, error_height))
        else:
            rows.append("No errors recorded.")
        return rows

    def _tail_wrapped(self, lines: Iterable[str], width: int, max_lines: int) -> list[str]:
        wrapper = textwrap.TextWrapper(
            width=max(20, width - 2),
            replace_whitespace=False,
            drop_whitespace=True,
        )
        wrapped: list[str] = []
        for line in lines:
            parts = wrapper.wrap(line) or [""]
            wrapped.extend(parts)
        return wrapped[-max_lines:] if wrapped else ["-"]

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."


def _reader_thread(stream, stream_name: str, output: "queue.Queue[tuple[str, str]]") -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put((stream_name, line))
    finally:
        stream.close()


def drain_process_output(output: "queue.Queue[tuple[str, str]]", dashboard: Dashboard) -> None:
    while True:
        try:
            stream_name, line = output.get_nowait()
        except queue.Empty:
            return
        prefix = "stderr" if stream_name == "stderr" else "stdout"
        dashboard.add_log(f"[{prefix}] {line}")


def remove_analysis_file(temp_output_dir: Path) -> None:
    analysis_path = temp_output_dir / ANALYSIS_FILENAME
    if analysis_path.exists():
        analysis_path.unlink()


def clean_temp_output(temp_output_dir: Path) -> None:
    if not temp_output_dir.exists():
        return
    for child in temp_output_dir.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def path_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_config(config: BatchConfig) -> None:
    if not config.clean_temp:
        return

    temp_dir = config.temp_output_dir
    protected_dirs = (config.input_dir, config.output_dir)
    for protected_dir in protected_dirs:
        if temp_dir == protected_dir or path_contains(temp_dir, protected_dir):
            raise ConfigError(
                f"{ENV_PREFIX}TEMP_OUTPUT_DIR must not be {protected_dir} or one "
                f"of its parents when {ENV_PREFIX}CLEAN_TEMP is true"
            )


def run_one_video(config: BatchConfig, video: Path, dashboard: Dashboard) -> int:
    config.temp_output_dir.mkdir(parents=True, exist_ok=True)
    remove_analysis_file(config.temp_output_dir)

    cmd = build_analyzer_command(config, video)
    dashboard.add_log(f"Running: {redact_command(cmd)}")

    process_output: "queue.Queue[tuple[str, str]]" = queue.Queue()
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    threads = []
    for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_reader_thread,
            args=(stream, stream_name, process_output),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    while process.poll() is None:
        drain_process_output(process_output, dashboard)
        dashboard.render()
        time.sleep(0.05)

    for thread in threads:
        thread.join(timeout=1.0)
    drain_process_output(process_output, dashboard)
    dashboard.render(force=True)
    return process.returncode


def archive_result(config: BatchConfig, video: Path) -> Path:
    source = config.temp_output_dir / ANALYSIS_FILENAME
    if not source.exists():
        raise FileNotFoundError(f"Analyzer did not create {source}")

    target = target_path_for(config, video)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not config.overwrite_existing:
            raise FileExistsError(
                f"Result already exists and overwrite is disabled: {target}"
            )
        target.unlink()
    shutil.move(str(source), str(target))
    return target


def run_batch(config: BatchConfig) -> int:
    validate_config(config)
    if not config.input_dir.exists():
        raise ConfigError(f"Input directory does not exist: {config.input_dir}")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    videos = discover_videos(config.input_dir, config.extensions)
    state = BatchState(total=len(videos), started_at=time.monotonic())

    with Dashboard(state) as dashboard:
        dashboard.add_log(f"Input: {config.input_dir}")
        dashboard.add_log(f"Output: {config.output_dir}")
        dashboard.add_log(f"Extensions: {', '.join(config.extensions)}")

        if not videos:
            state.status = "No videos found"
            dashboard.add_error(f"No videos found in {config.input_dir}")
            dashboard.render(force=True)
            return 1

        dashboard.add_log(f"Queued {len(videos)} video(s).")
        for index, video in enumerate(videos, start=1):
            relative_video = str(video.relative_to(config.input_dir))
            target = target_path_for(config, video)
            state.current_index = index
            state.current_video = relative_video
            state.video_started_at = time.monotonic()

            if config.skip_existing and target.exists():
                state.skipped += 1
                state.status = "Skipped existing result"
                dashboard.add_log(f"Skipped existing: {relative_video} -> {target}")
                dashboard.render(force=True)
                continue

            state.status = "Analyzing"
            dashboard.add_log(f"Processing [{index}/{len(videos)}]: {relative_video}")
            dashboard.render(force=True)

            try:
                return_code = run_one_video(config, video, dashboard)
                if return_code != 0:
                    state.failed += 1
                    state.status = f"Failed with exit code {return_code}"
                    dashboard.add_error(
                        f"Command failed for {relative_video} with exit code {return_code}"
                    )
                    continue

                archived_path = archive_result(config, video)
                state.completed += 1
                state.status = "Archived"
                dashboard.add_log(f"Archived: {relative_video} -> {archived_path}")
            except Exception as exc:
                state.failed += 1
                state.status = "Failed"
                dashboard.add_error(f"{relative_video}: {exc}")
            finally:
                if config.clean_temp and not config.keep_frames:
                    try:
                        clean_temp_output(config.temp_output_dir)
                    except Exception as exc:
                        dashboard.add_error(
                            f"Could not clean temp output {config.temp_output_dir}: {exc}"
                        )
                dashboard.render(force=True)

        state.current_video = ""
        state.status = "Complete"
        dashboard.add_log(
            f"Done. ok={state.completed}, failed={state.failed}, skipped={state.skipped}"
        )
        dashboard.render(force=True)

    return 0 if state.failed == 0 else 2


def main() -> None:
    configure_text_streams()
    if any(arg in {"-h", "--help", "--help-env"} for arg in sys.argv[1:]):
        print(ENV_HELP)
        raise SystemExit(0)

    try:
        config = load_config_from_env()
        configure_windows_cuda_dll_dirs(config.add_cuda_dll_dirs)
        exit_code = run_batch(config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        print(ENV_HELP, file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
