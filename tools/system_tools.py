import subprocess
import sys
from pathlib import Path
from . import register_tool


def _resolve_path(raw_path: str, cwd: str) -> Path:
    if "~/" in raw_path and not raw_path.startswith("~/"):
        raw_path = "~/" + raw_path.split("~/", 1)[1]
    p = Path(raw_path).expanduser()
    if p.is_absolute():
        return p.resolve()
    base = Path(cwd).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    return (base / p).resolve()


@register_tool(
    name="write_file",
    description="Create folders and save text/code to a file inside Workspace or Desktop (automatically creates any parent folders in path!)",
    schema_example='{"tool": "write_file", "path": "my_folder/info.txt", "content": "text content"}',
)
def tool_write_file(payload: dict, context: dict) -> str:
    raw_path = payload.get("path", "").strip()
    content = payload.get("content", "")
    if not raw_path:
        return "[TERMINAL_ERROR] Missing 'path' field."

    target = _resolve_path(raw_path, context.get("cwd", "./Workspace"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"[WRITE_SUCCESS] Created directory '{target.parent}' and saved {len(content)} characters to '{target}'."


@register_tool(
    name="read_file",
    description="Read contents of a local file or list contents of a directory in Workspace or Desktop",
    schema_example='{"tool": "read_file", "path": "my_folder/info.txt"}',
)
def tool_read_file(payload: dict, context: dict) -> str:
    raw_path = payload.get("path", "").strip() or "."
    target = _resolve_path(raw_path, context.get("cwd", "./Workspace"))
    if target.is_dir():
        items = sorted(target.iterdir())
        lines = [f"{'[DIR]' if i.is_dir() else '[FILE]'} {i.name}" for i in items if not i.name.startswith(".")]
        return f"[DIRECTORY_LISTING for {target}]:\n" + ("\n".join(lines) if lines else "[Empty directory]")
    if not target.is_file():
        return f"[TERMINAL_ERROR] File not found at {target}"
    obs_limit = int(context.get("obs_limit", 4000))
    return target.read_text(encoding="utf-8", errors="replace")[:obs_limit]


@register_tool(
    name="run_terminal",
    description="Run macOS shell commands in Workspace (e.g. ls, rm, mv, cp, find, du, mkdir, cat, git, etc.)",
    schema_example='{"tool": "run_terminal", "command": "rm filename.mp4"}',
)
def tool_run_terminal(payload: dict, context: dict) -> str:
    command = payload.get("command", "").strip()
    if not command:
        return "[TERMINAL_ERROR] Missing 'command' field."

    if "\n" in command or "<<" in command:
        return (
            "[TERMINAL_BLOCKED] Do NOT use multi-line strings in run_terminal to write files. "
            "Use the 'write_file' tool instead—it creates folders and writes files safely."
        )

    cwd_path = Path(context.get("cwd", "./Workspace")).expanduser().resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    timeout = int(context.get("timeout", 30))
    obs_limit = int(context.get("obs_limit", 4000))

    proc = subprocess.run(
        command, shell=True, cwd=str(cwd_path), capture_output=True, text=True, timeout=timeout
    )
    out = proc.stdout + (f"\n[STDERR]\n{proc.stderr}" if proc.stderr else "")
    out = out.strip()
    return out[:obs_limit] if out else f"[Command '{command}' succeeded with exit code {proc.returncode}]"


@register_tool(
    name="python_exec",
    description="Execute Python code inside Workspace for calculations, data processing, or automation",
    schema_example='{"tool": "python_exec", "code": "import os; print(os.listdir(\'.\'))"}',
)
def tool_python_exec(payload: dict, context: dict) -> str:
    code = payload.get("code", "")
    if not code:
        return "[TERMINAL_ERROR] Missing 'code' field."

    cwd_path = Path(context.get("cwd", "./Workspace")).expanduser().resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    timeout = int(context.get("timeout", 30))
    obs_limit = int(context.get("obs_limit", 4000))

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = proc.stdout + (f"\n[STDERR]\n{proc.stderr}" if proc.stderr else "")
    out = out.strip()
    return out[:obs_limit] if out else f"[Code finished with returncode {proc.returncode}]"
