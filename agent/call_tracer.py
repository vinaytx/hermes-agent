"""Human-readable call-flow tracer for Hermes Agent.

Writes a dated trace file to ~/.hermes/logs/trace_YYYY-MM-DD.log that captures:
  - User input per turn
  - Exact api_messages sent to the LLM on each iteration
  - Raw LLM response (content + tool calls) and latency
  - Tool call arguments and results
  - Iteration numbers and wall-clock timestamps

Environment variables:
    HERMES_TRACE=1               Enable tracing (required)
    HERMES_TRACE_SYSTEM_PROMPT=1 Also print the system prompt in each
                                 "SENDING TO LLM" block (default: off,
                                 because the system prompt rarely changes
                                 between iterations and adds noise)

The file is appended to (never truncated) so multiple concurrent sessions
share one daily file. A threading lock serialises writes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Per-day file handles and locks ─────────────────────────────────────────
_lock = threading.Lock()
_open_files: dict[str, Any] = {}  # date-string → open file handle


def _enabled() -> bool:
    return os.environ.get("HERMES_TRACE", "").strip() not in ("", "0", "false", "no")


def _show_system_prompt() -> bool:
    return os.environ.get("HERMES_TRACE_SYSTEM_PROMPT", "").strip() not in ("", "0", "false", "no")


def _get_file(date_str: str):
    """Return (and cache) an open append-mode file for *date_str*."""
    if date_str in _open_files:
        return _open_files[date_str]
    try:
        from hermes_constants import get_hermes_home
        log_dir = get_hermes_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"trace_{date_str}.log"
        fh = path.open("a", encoding="utf-8", buffering=1)  # line-buffered
        _open_files[date_str] = fh
        return fh
    except Exception:
        return None


def _write(lines: list[str], date_str: str) -> None:
    with _lock:
        fh = _get_file(date_str)
        if fh is None:
            return
        try:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
        except Exception:
            pass


# ── Formatting helpers ──────────────────────────────────────────────────────

_WIDE = "=" * 80
_THIN = "-" * 80

def _now() -> tuple[str, str]:
    """Return (date_str YYYY-MM-DD, timestamp_str HH:MM:SS.mmm)."""
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _trunc(text: str, limit: int = 0) -> str:
    """Coerce to str. The limit parameter is kept for call-site compatibility but ignored."""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return "<non-string>"
    return text


def _format_message(msg: dict, include_system: bool = True) -> list[str]:
    """Render a single OpenAI-style message dict as human-readable lines.

    When *include_system* is False, system messages are replaced with a
    single summary line so they don't flood the output on every iteration.
    """
    role = msg.get("role", "?")
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    tool_call_id = msg.get("tool_call_id")

    lines: list[str] = []

    if role == "system":
        if not include_system:
            lines.append(f"  [system] ({len(content)} chars)  <set HERMES_TRACE_SYSTEM_PROMPT=1 to expand>")
            return lines
        lines.append(f"  [{role}] ({len(content)} chars)")
        lines.append(f"    {_trunc(content, 600).replace(chr(10), chr(10) + '    ')}")
    elif role == "tool":
        label = f"  [tool-result id={tool_call_id}]" if tool_call_id else "  [tool-result]"
        lines.append(f"{label} ({len(content)} chars)")
        lines.append(f"    {_trunc(content, 800).replace(chr(10), chr(10) + '    ')}")
    elif role == "assistant" and tool_calls:
        if content:
            lines.append(f"  [assistant] {_trunc(content, 400)}")
        for i, tc in enumerate(tool_calls, 1):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name", "?")
            args = fn.get("arguments", "{}")
            lines.append(f"  [assistant tool_call {i}] {name}")
            try:
                parsed = json.loads(args)
                pretty = json.dumps(parsed, indent=4, ensure_ascii=False)
            except Exception:
                pretty = args
            lines.append(f"    {_trunc(pretty, 600).replace(chr(10), chr(10) + '    ')}")
    else:
        if isinstance(content, list):
            # Multimodal content blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "image_url":
                        text_parts.append("[image]")
                    else:
                        text_parts.append(f"[{block.get('type', '?')}]")
            content = " ".join(text_parts)
        lines.append(f"  [{role}] {_trunc(content, 1000)}")

    return lines


def _format_response(response: Any) -> list[str]:
    """Render an OpenAI completion response object as human-readable lines."""
    lines: list[str] = []
    if response is None:
        lines.append("  <None>")
        return lines

    choices = getattr(response, "choices", None) or []
    usage = getattr(response, "usage", None)
    model = getattr(response, "model", None)

    if model:
        lines.append(f"  model: {model}")
    if usage:
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        if pt is not None or ct is not None:
            lines.append(f"  tokens: prompt={pt}  completion={ct}")

    for choice in choices:
        msg = getattr(choice, "message", None)
        finish = getattr(choice, "finish_reason", None)
        if finish:
            lines.append(f"  finish_reason: {finish}")
        if msg is None:
            continue
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None) or []
        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None) or ""

        if reasoning:
            lines.append(f"  <think> ({len(reasoning)} chars)")
            lines.append(f"    {_trunc(reasoning, 400).replace(chr(10), chr(10) + '    ')}")

        if content:
            lines.append(f"  content: ({len(content)} chars)")
            lines.append(f"    {_trunc(content, 1500).replace(chr(10), chr(10) + '    ')}")

        if tool_calls:
            lines.append(f"  tool_calls: {len(tool_calls)}")
            for i, tc in enumerate(tool_calls, 1):
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", "?") if fn else "?"
                args = getattr(fn, "arguments", "{}") if fn else "{}"
                lines.append(f"    [{i}] {name}")
                try:
                    parsed = json.loads(args)
                    pretty = json.dumps(parsed, indent=6, ensure_ascii=False)
                except Exception:
                    pretty = args
                lines.append(f"      {_trunc(pretty, 600).replace(chr(10), chr(10) + '      ')}")

    return lines


# ── Public API ──────────────────────────────────────────────────────────────

def trace_event(agent: Any, event: str, **kwargs) -> None:
    """Write a single trace event for *agent* to the daily trace file.

    Silently swallows all exceptions — the tracer must never break the agent.
    """
    if not _enabled():
        return
    try:
        _write_event(agent, event, kwargs)
    except Exception:
        pass


def _write_event(agent: Any, event: str, kw: dict) -> None:
    date_str, ts = _now()
    session_id = getattr(agent, "session_id", None) or "?"
    model = getattr(agent, "model", None) or "?"
    provider = getattr(agent, "provider", None) or "?"
    lines: list[str] = []

    if event == "turn_start":
        user_message: str = kw.get("user_message", "")
        lines += [
            "",
            _WIDE,
            f"TURN START  {ts}  session={session_id}  model={model}  provider={provider}",
            _WIDE,
            "USER INPUT",
            f"  {_trunc(user_message, 2000).replace(chr(10), chr(10) + '  ')}",
        ]

    elif event == "llm_send":
        iteration: int = kw.get("iteration", 0)
        api_messages: list = kw.get("api_messages", [])
        lines += [
            "",
            _THIN,
            f"ITERATION {iteration}  {ts}",
            _THIN,
            f"→ SENDING TO LLM  messages={len(api_messages)}",
        ]
        include_sys = _show_system_prompt()
        for msg in api_messages:
            lines += _format_message(msg, include_system=include_sys)

    elif event == "llm_response":
        iteration: int = kw.get("iteration", 0)
        response: Any = kw.get("response")
        duration: float = kw.get("duration", 0.0)
        lines += [
            f"← LLM RESPONSE  iteration={iteration}  duration={duration:.2f}s",
        ]
        lines += _format_response(response)

    elif event == "tool_calls":
        iteration: int = kw.get("iteration", 0)
        tool_calls: list = kw.get("tool_calls", [])
        lines += [f"⚙ TOOL CALLS  iteration={iteration}  count={len(tool_calls)}"]
        for i, tc in enumerate(tool_calls, 1):
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "?") if fn else "?"
            args = getattr(fn, "arguments", "{}") if fn else "{}"
            lines.append(f"  [{i}] {name}")
            try:
                parsed = json.loads(args)
                pretty = json.dumps(parsed, indent=4, ensure_ascii=False)
            except Exception:
                pretty = args
            lines.append(f"    {_trunc(pretty, 800).replace(chr(10), chr(10) + '    ')}")

    elif event == "tool_results":
        iteration: int = kw.get("iteration", 0)
        new_messages: list = kw.get("new_messages", [])
        tool_msgs = [m for m in new_messages if m.get("role") == "tool"]
        lines += [f"✓ TOOL RESULTS  iteration={iteration}  count={len(tool_msgs)}"]
        for i, msg in enumerate(tool_msgs, 1):
            tid = msg.get("tool_call_id", "?")
            content = msg.get("content", "")
            lines.append(f"  [{i}] id={tid}  ({len(content)} chars)")
            lines.append(f"    {_trunc(content, 1000).replace(chr(10), chr(10) + '    ')}")

    elif event == "final_response":
        iteration: int = kw.get("iteration", 0)
        response: str = kw.get("response", "")
        lines += [
            "",
            _WIDE,
            f"FINAL RESPONSE  {ts}  iterations={iteration}",
            _WIDE,
            f"  {_trunc(response, 3000).replace(chr(10), chr(10) + '  ')}",
            _WIDE,
        ]

    if lines:
        _write(lines, date_str)
