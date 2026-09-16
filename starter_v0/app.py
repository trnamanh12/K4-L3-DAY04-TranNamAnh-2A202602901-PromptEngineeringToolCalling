#!/usr/bin/env python3
"""
IT Helpdesk Agent Web UI & Transcript Viewer
Compliant with Day04 Rubric:
- Runnable Web UI (built-in Python http.server, zero external dependencies required)
- Displays Tool Calls, Inputs (Arguments), Tool Results, and Tool Errors transparently
- Displays Active Version, Artifact Version, Prompt Hash, Tools Hash, Provider & Model
- Real-time Transcript Logging & Historical Transcript Inspector
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

from env_loader import load_lab_env
from providers import make_provider
from providers.base import ToolCall
from tools import TOOL_FUNCTIONS, load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

ROOT = Path(__file__).parent
ARTIFACTS_DIR = ROOT / "artifacts"
TRANSCRIPTS_DIR = ROOT / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
load_lab_env(ROOT)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("_") or "run"


def execute_tool_call(call: ToolCall) -> dict[str, Any]:
    func = TOOL_FUNCTIONS.get(call.name)
    if not func:
        return {
            "tool": call.name,
            "args": call.args,
            "result": {"error": "unknown_tool", "message": f"No local implementation for {call.name}"},
        }
    try:
        result = func(**call.args)
    except Exception as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
    return {"tool": call.name, "args": call.args, "result": result}


def json_text(value: Any, *, max_chars: int | None = None) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>"
    return text


def trim_history(history: list[dict[str, str]], window: int) -> list[dict[str, str]]:
    if window <= 0:
        return []
    return history[-window * 2 :]


def tool_results_message(events: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "TOOL_RESULTS_JSON:\n"
            f"{json_text(events, max_chars=24000)}\n\n"
            "Use only these tool results. If the user asked for an incident report and the findings are ready, "
            "call the reporting tool. Otherwise answer directly, state uncertainty, and give the safest next step."
        ),
    }


def assistant_tool_message(response_text: str | None, calls: list[ToolCall]) -> dict[str, str]:
    call_summary = [{"name": call.name, "args": call.args} for call in calls]
    content = response_text or "I will call the selected tool(s)."
    return {
        "role": "assistant",
        "content": f"{content}\n\nTOOL_CALLS_JSON:\n{json_text(call_summary)}",
    }


def write_transcript_file(path: Path, transcript: dict[str, Any]) -> None:
    transcript["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class AgentSession:
    """Manages an interactive session, version configurations, and live transcript."""

    def __init__(
        self,
        provider_name: str = "gemini",
        version: str = "v3",
        model: str | None = None,
        history_window: int = 5,
        max_tool_rounds: int = 4,
    ) -> None:
        self.lock = threading.Lock()
        self.provider_name = provider_name
        self.version = version
        self.custom_model = model
        self.history_window = history_window
        self.max_tool_rounds = max_tool_rounds
        self.history: list[dict[str, str]] = []
        self.turn_index = 0

        self._load_artifacts()
        self._init_transcript()

    def _load_artifacts(self) -> None:
        version_dir = ARTIFACTS_DIR / "versions" / self.version
        if version_dir.exists() and (version_dir / "system_prompt.md").exists():
            self.system_prompt_path = version_dir / "system_prompt.md"
            self.tools_path = version_dir / "tools.yaml"
        else:
            self.system_prompt_path = ARTIFACTS_DIR / "system_prompt.md"
            self.tools_path = ARTIFACTS_DIR / "tools.yaml"

        self.system_prompt = self.system_prompt_path.read_text(encoding="utf-8")
        self.tool_declarations = load_tool_declarations(self.tools_path)
        self.openai_tools = to_openai_tools(self.tool_declarations)
        self.provider = make_provider(self.provider_name)
        self.model = self.custom_model or getattr(self.provider, "default_model", None)
        self.artifact_version = build_artifact_version(
            self.version, self.system_prompt_path, self.tools_path
        )

    def _init_transcript(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        self.transcript_id = "_".join([
            safe_slug(self.version),
            safe_slug(self.provider_name),
            timestamp,
        ])
        self.transcript_path = TRANSCRIPTS_DIR / f"{self.transcript_id}.transcript.json"
        self.transcript: dict[str, Any] = {
            "transcript_id": self.transcript_id,
            **artifact_version_dict(self.artifact_version),
            "provider": self.provider_name,
            "model": self.model,
            "system_prompt": str(self.system_prompt_path),
            "tools": str(self.tools_path),
            "history_window": self.history_window,
            "max_tool_rounds": self.max_tool_rounds,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "turns": [],
        }

    def reset_chat(self) -> dict[str, Any]:
        with self.lock:
            self.history = []
            self.turn_index = 0
            self._init_transcript()
            return self.get_info()

    def set_version(self, version: str) -> dict[str, Any]:
        with self.lock:
            self.version = version
            self._load_artifacts()
            self.history = []
            self.turn_index = 0
            self._init_transcript()
            return self.get_info()

    def get_info(self) -> dict[str, Any]:
        available_versions = []
        versions_dir = ARTIFACTS_DIR / "versions"
        if versions_dir.exists():
            available_versions = sorted([d.name for d in versions_dir.iterdir() if d.is_dir()])
        if not available_versions:
            available_versions = ["v0", "v1", "v2", "v3"]

        saved_files = []
        if TRANSCRIPTS_DIR.exists():
            saved_files = sorted(
                [f.name for f in TRANSCRIPTS_DIR.glob("*.transcript.json")],
                reverse=True,
            )

        return {
            "version": self.version,
            "artifact_version": self.artifact_version.artifact_version,
            "prompt_hash": self.artifact_version.prompt_hash,
            "tools_hash": self.artifact_version.tools_hash,
            "provider": self.provider_name,
            "model": self.model,
            "transcript_id": self.transcript_id,
            "transcript_filename": self.transcript_path.name,
            "available_versions": available_versions,
            "saved_transcripts": saved_files,
            "turn_count": len(self.transcript["turns"]),
        }

    def chat_turn(self, user_text: str) -> dict[str, Any]:
        with self.lock:
            self.turn_index += 1
            messages = [
                {"role": "system", "content": self.system_prompt},
                *trim_history(self.history, self.history_window),
                {"role": "user", "content": user_text},
            ]

            turn_record: dict[str, Any] = {
                "turn_index": self.turn_index,
                "started_at": now_iso(),
                "user": user_text,
                "status": "started",
                "assistant_text": None,
                "rounds": [],
                "tool_events": [],
            }

            try:
                working_messages = list(messages)
                rounds: list[dict[str, Any]] = []
                all_tool_events: list[dict[str, Any]] = []

                for round_index in range(1, self.max_tool_rounds + 1):
                    response = self.provider.complete(
                        working_messages,
                        self.openai_tools,
                        model=self.custom_model,
                        temperature=0.0,
                    )
                    calls = response.tool_calls
                    round_record: dict[str, Any] = {
                        "round": round_index,
                        "assistant_text": response.text,
                        "tool_calls": [{"name": call.name, "args": call.args} for call in calls],
                        "tool_results": [],
                    }

                    if not calls:
                        rounds.append(round_record)
                        turn_record.update({
                            "status": "answered",
                            "assistant_text": response.text or "",
                            "rounds": rounds,
                            "tool_events": all_tool_events,
                        })
                        break

                    working_messages.append(assistant_tool_message(response.text, calls))
                    non_clarification_events: list[dict[str, Any]] = []

                    is_waiting = False
                    waiting_question = ""

                    for call in calls:
                        event = execute_tool_call(call)
                        round_record["tool_results"].append(event)
                        all_tool_events.append(event)

                        result = event.get("result", {})
                        if isinstance(result, dict) and result.get("awaiting_user"):
                            is_waiting = True
                            waiting_question = (
                                result.get("question")
                                or call.args.get("question")
                                or "Bạn bổ sung thêm thông tin nhé."
                            )
                        else:
                            non_clarification_events.append(event)

                    rounds.append(round_record)

                    if is_waiting:
                        turn_record.update({
                            "status": "waiting_for_user",
                            "assistant_text": waiting_question,
                            "rounds": rounds,
                            "tool_events": all_tool_events,
                        })
                        break

                    working_messages.append(tool_results_message(non_clarification_events))
                else:
                    turn_record.update({
                        "status": "max_tool_rounds",
                        "assistant_text": f"Đã dừng sau {self.max_tool_rounds} vòng gọi công cụ.",
                        "rounds": rounds,
                        "tool_events": all_tool_events,
                    })

                assistant_text = turn_record.get("assistant_text") or ""
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": assistant_text})

            except Exception as exc:
                turn_record.update({
                    "status": "provider_error",
                    "error": f"{type(exc).__name__}: {str(exc)}",
                    "assistant_text": f"Lỗi gọi provider: {type(exc).__name__} - {str(exc)}",
                })

            turn_record["ended_at"] = now_iso()
            self.transcript["turns"].append(turn_record)
            write_transcript_file(self.transcript_path, self.transcript)

            return {
                "turn": turn_record,
                "transcript": self.transcript,
                "info": self.get_info(),
            }


INDEX_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>IT Helpdesk AI Agent — Live UI & Transcript</title>
  <style>
    :root {
      --bg: #0f172a;
      --panel: #1e293b;
      --panel-hover: #334155;
      --card: #0b1120;
      --border: #334155;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --accent: #06b6d4;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --success: #10b981;
      --warning: #f59e0b;
      --danger: #ef4444;
      --code-bg: #020617;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-title {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .header-title h1 {
      font-size: 1.15rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #fff;
    }
    .status-dot {
      width: 10px;
      height: 10px;
      background: var(--success);
      border-radius: 50%;
      box-shadow: 0 0 8px var(--success);
    }
    .header-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--accent);
    }
    .badge-prov { color: #ec4899; }
    .badge-ver { color: #38bdf8; background: rgba(56, 189, 248, 0.1); border-color: rgba(56, 189, 248, 0.3); }
    .badge-hash { font-family: monospace; color: var(--text-muted); font-size: 0.72rem; }
    .version-select {
      background: var(--card);
      color: #fff;
      border: 1px solid var(--border);
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 0.8rem;
      cursor: pointer;
      font-weight: 600;
    }
    .btn {
      background: var(--primary);
      color: #fff;
      border: none;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .btn:hover { background: var(--primary-hover); }
    .btn-secondary {
      background: var(--card);
      border: 1px solid var(--border);
      color: var(--text);
    }
    .btn-secondary:hover { background: var(--panel-hover); }
    .btn-danger { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); }
    .btn-danger:hover { background: rgba(239, 68, 68, 0.4); }

    /* Quick Scenarios Bar */
    .quick-bar {
      background: rgba(15, 23, 42, 0.75);
      border-bottom: 1px solid var(--border);
      padding: 8px 24px;
      display: flex;
      align-items: center;
      gap: 8px;
      overflow-x: auto;
      font-size: 0.8rem;
    }
    .quick-title {
      color: var(--text-muted);
      font-weight: 600;
      white-space: nowrap;
      margin-right: 4px;
    }
    .quick-chip {
      background: var(--panel);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 4px 10px;
      border-radius: 9999px;
      white-space: nowrap;
      cursor: pointer;
      font-size: 0.76rem;
      transition: all 0.15s ease;
    }
    .quick-chip:hover {
      background: var(--primary);
      border-color: var(--primary);
      color: #fff;
      transform: translateY(-1px);
    }

    /* Main Container */
    .main-container {
      display: flex;
      flex: 1;
      height: calc(100vh - 110px);
      overflow: hidden;
    }

    /* Left Chat Stream */
    .chat-section {
      flex: 1;
      display: flex;
      flex-direction: column;
      border-right: 1px solid var(--border);
      background: var(--bg);
      min-width: 0;
    }
    .chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 20px 24px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .empty-state {
      margin: auto;
      text-align: center;
      color: var(--text-muted);
      max-width: 460px;
      padding: 40px 20px;
    }
    .empty-state h3 { color: #fff; margin-bottom: 8px; font-size: 1.1rem; }
    .empty-state p { font-size: 0.88rem; line-height: 1.5; margin-bottom: 16px; }

    .msg-group {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .msg-user {
      align-self: flex-end;
      max-width: 80%;
      background: #1d4ed8;
      color: #fff;
      padding: 10px 16px;
      border-radius: 14px 14px 2px 14px;
      font-size: 0.92rem;
      line-height: 1.45;
      box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    }
    .msg-user-meta {
      align-self: flex-end;
      font-size: 0.72rem;
      color: var(--text-muted);
      margin-top: 2px;
      margin-right: 4px;
    }

    /* Tool Call Card */
    .tool-card {
      align-self: flex-start;
      width: 100%;
      background: var(--card);
      border: 1px solid #3b4252;
      border-left: 4px solid var(--accent);
      border-radius: 8px;
      padding: 12px 16px;
      margin: 4px 0;
      font-size: 0.85rem;
    }
    .tool-card.error {
      border-left-color: var(--danger);
      background: rgba(239, 68, 68, 0.05);
    }
    .tool-card.waiting {
      border-left-color: var(--warning);
      background: rgba(245, 158, 11, 0.05);
    }
    .tool-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .tool-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: monospace;
      font-size: 0.82rem;
      font-weight: 700;
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.12);
      padding: 3px 8px;
      border-radius: 4px;
    }
    .tool-badge.write { color: #f43f5e; background: rgba(244, 63, 94, 0.12); }
    .tool-status-tag {
      font-size: 0.74rem;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 600;
    }
    .status-tag-ok { color: var(--success); background: rgba(16, 185, 129, 0.12); }
    .status-tag-error { color: var(--danger); background: rgba(239, 68, 68, 0.15); font-weight: 700; }
    .status-tag-wait { color: var(--warning); background: rgba(245, 158, 11, 0.15); font-weight: 700; }

    .tool-section { margin-top: 8px; }
    .tool-label {
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 4px;
      font-weight: 700;
    }
    pre.code-box {
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      font-family: "JetBrains Mono", Consolas, Menlo, monospace;
      font-size: 0.78rem;
      color: #e2e8f0;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 240px;
    }
    pre.code-box.err {
      color: #fca5a5;
      border-color: rgba(239, 68, 68, 0.4);
      background: rgba(239, 68, 68, 0.08);
    }

    .msg-assistant {
      align-self: flex-start;
      max-width: 85%;
      background: var(--panel);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 12px 18px;
      border-radius: 14px 14px 14px 2px;
      font-size: 0.92rem;
      line-height: 1.5;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    }
    .msg-assistant pre {
      margin-top: 8px;
      background: var(--code-bg);
      padding: 10px 12px;
      border-radius: 6px;
      border: 1px solid var(--border);
      font-size: 0.8rem;
      white-space: pre-wrap;
      overflow-x: auto;
    }

    /* Input Bar */
    .chat-input-box {
      border-top: 1px solid var(--border);
      background: var(--panel);
      padding: 12px 24px;
      display: flex;
      gap: 12px;
      align-items: center;
    }
    .chat-input-box input {
      flex: 1;
      background: var(--card);
      border: 1px solid var(--border);
      color: #fff;
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 0.92rem;
      outline: none;
      transition: border-color 0.15s ease;
    }
    .chat-input-box input:focus { border-color: var(--primary); }

    /* Right Inspector / Transcripts */
    .inspector-section {
      width: 460px;
      background: var(--panel);
      display: flex;
      flex-direction: column;
      min-width: 360px;
    }
    .tabs-bar {
      display: flex;
      border-bottom: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.6);
    }
    .tab-btn {
      flex: 1;
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--text-muted);
      padding: 10px 12px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s ease;
      text-align: center;
    }
    .tab-btn.active {
      color: #fff;
      border-bottom-color: var(--primary);
      background: rgba(59, 130, 246, 0.08);
    }
    .tab-content {
      flex: 1;
      display: none;
      flex-direction: column;
      overflow: hidden;
      padding: 14px 18px;
    }
    .tab-content.active { display: flex; }

    .transcript-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      gap: 8px;
      flex-wrap: wrap;
    }
    .transcript-viewer {
      flex: 1;
      background: var(--code-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
      font-family: "JetBrains Mono", Consolas, Menlo, monospace;
      font-size: 0.75rem;
      color: #38bdf8;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* Saved Transcripts List */
    .history-list {
      flex: 1;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .history-item {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 12px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .history-item:hover {
      background: var(--panel-hover);
      border-color: var(--primary);
    }
    .history-item-title {
      font-family: monospace;
      font-size: 0.78rem;
      color: #fff;
      font-weight: 600;
      margin-bottom: 4px;
      word-break: break-all;
    }
    .history-item-sub {
      font-size: 0.72rem;
      color: var(--text-muted);
    }

    /* Spinner */
    .loading-spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      border-top-color: #fff;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    @media (max-width: 900px) {
      .main-container { flex-direction: column; height: auto; }
      .inspector-section { width: 100%; height: 450px; }
      .chat-section { height: 600px; }
    }
  </style>
</head>
<body>

  <header>
    <div class="header-title">
      <div class="status-dot"></div>
      <h1>IT Helpdesk AI Agent</h1>
      <span class="badge badge-ver" id="activeVerBadge">v3</span>
      <span class="badge badge-prov" id="activeProvBadge">gemini-3.5-flash-lite</span>
    </div>

    <div class="header-meta">
      <label style="font-size:0.78rem; color:var(--text-muted); font-weight:600;">Phiên bản:</label>
      <select id="versionSelect" class="version-select" onchange="changeVersion(this.value)">
        <option value="v3">v3 (Hiện tại - Fix H16 parallel)</option>
        <option value="v2">v2 (Confirmation Boundary)</option>
        <option value="v1">v1 (Schema clarify & service)</option>
        <option value="v0">v0 (Baseline)</option>
      </select>

      <span class="badge badge-hash" id="artifactHashBadge">artifact: ...</span>
      <button class="btn btn-secondary" onclick="resetChat()" title="Tạo cuộc hội thoại mới với file transcript mới">
        🔄 Chat mới
      </button>
    </div>
  </header>

  <!-- Quick Scenarios Test Bar -->
  <div class="quick-bar">
    <span class="quick-title">⚡ Test mẫu:</span>
    <div class="quick-chip" onclick="quickSend('Kiểm tra VPN production có đang lỗi không?')">
      1. Trạng thái VPN
    </div>
    <div class="quick-chip" onclick="quickSend('Kiểm tra kết nối mạng trên laptop của tôi.')">
      2. Thiếu ID (Clarify text)
    </div>
    <div class="quick-chip" onclick="quickSend('LT-204')">
      2b. Bổ sung LT-204
    </div>
    <div class="quick-chip" onclick="quickSend('Tạo ticket mức high cho lỗi Wi-Fi trên máy LT-204.')">
      3. Yêu cầu tạo ticket (Confirm)
    </div>
    <div class="quick-chip" onclick="quickSend('Đồng ý, hãy tạo ticket giúp tôi.')">
      3b. Xác nhận Yes
    </div>
    <div class="quick-chip" onclick="quickSend('So sánh tình trạng máy LT-204 và máy DT-031.')">
      4. So sánh 2 máy (Parallel)
    </div>
    <div class="quick-chip" onclick="quickSend('Thôi, tôi hủy yêu cầu.')">
      5. Hủy yêu cầu
    </div>
  </div>

  <div class="main-container">
    <!-- Chat Section -->
    <section class="chat-section">
      <div class="chat-messages" id="chatMessages">
        <div class="empty-state" id="emptyState">
          <h3>Hệ thống Trợ lý IT Helpdesk Sẵn sàng</h3>
          <p>
            Agent có khả năng chẩn đoán thiết bị, kiểm tra dịch vụ, tra cứu hướng dẫn và tạo ticket sự cố.
            Mọi lệnh gọi công cụ, tham số và kết quả/lỗi đều được kiểm tra và ghi nhận vào transcript.
          </p>
          <div style="display:flex; justify-content:center; gap:8px;">
            <button class="btn" onclick="quickSend('Kiểm tra VPN production có đang lỗi không?')">Thử câu hỏi mẫu</button>
          </div>
        </div>
      </div>

      <div class="chat-input-box">
        <input type="text" id="userInput" placeholder="Nhập yêu cầu IT trợ giúp (ví dụ: 'Kiểm tra VPN production', 'Tạo ticket...')" onkeydown="if(event.key==='Enter') sendChat();">
        <button class="btn" id="sendBtn" onclick="sendChat()">
          <span>Gửi</span>
        </button>
      </div>
    </section>

    <!-- Right Inspector / Transcripts -->
    <aside class="inspector-section">
      <div class="tabs-bar">
        <button class="tab-btn active" id="tabLiveBtn" onclick="switchTab('live')">📋 Transcript Hiện Tại</button>
        <button class="tab-btn" id="tabSavedBtn" onclick="switchTab('saved')">🗂️ Lịch Sử Transcripts</button>
      </div>

      <!-- Tab: Live Transcript -->
      <div class="tab-content active" id="tabLive">
        <div class="transcript-toolbar">
          <div>
            <span style="font-size:0.75rem; color:var(--text-muted); font-weight:600;">ID: </span>
            <span id="transcriptIdLabel" style="font-family:monospace; font-size:0.75rem; color:#fff;">...</span>
          </div>
          <div style="display:flex; gap:6px;">
            <button class="btn btn-secondary" style="padding:4px 8px; font-size:0.72rem;" onclick="copyTranscript()">📋 Copy JSON</button>
            <button class="btn btn-secondary" style="padding:4px 8px; font-size:0.72rem;" onclick="downloadTranscript()">💾 Tải về</button>
          </div>
        </div>
        <pre class="transcript-viewer" id="liveTranscriptView">{\n  "status": "Đang tải dữ liệu..."\n}</pre>
      </div>

      <!-- Tab: Saved Transcripts -->
      <div class="tab-content" id="tabSaved">
        <div class="transcript-toolbar">
          <span style="font-size:0.75rem; color:var(--text-muted); font-weight:600;">Các file đã lưu trong transcripts/:</span>
          <button class="btn btn-secondary" style="padding:4px 8px; font-size:0.72rem;" onclick="loadInfo()">🔄 Làm mới</button>
        </div>
        <div class="history-list" id="savedList">
          <div style="color:var(--text-muted); font-size:0.8rem;">Đang tải danh sách...</div>
        </div>
      </div>
    </aside>
  </div>

  <script>
    let currentTranscript = null;
    let isSending = false;

    async function loadInfo() {
      try {
        const res = await fetch('/api/info');
        const data = await res.json();
        document.getElementById('activeVerBadge').textContent = data.version;
        document.getElementById('activeProvBadge').textContent = `${data.provider} (${data.model || 'default'})`;
        document.getElementById('artifactHashBadge').textContent = data.artifact_version || data.version;
        document.getElementById('transcriptIdLabel').textContent = data.transcript_filename;
        document.getElementById('versionSelect').value = data.version;

        // Populate saved transcripts
        const listEl = document.getElementById('savedList');
        if (!data.saved_transcripts || data.saved_transcripts.length === 0) {
          listEl.innerHTML = '<div style="color:var(--text-muted); font-size:0.8rem; padding:10px;">Chưa có transcript nào.</div>';
        } else {
          listEl.innerHTML = data.saved_transcripts.map(fn => `
            <div class="history-item" onclick="viewSavedTranscript('${fn}')">
              <div class="history-item-title">📄 ${fn}</div>
              <div class="history-item-sub">Nhấn để xem chi tiết JSON</div>
            </div>
          `).join('');
        }
      } catch (err) {
        console.error("Failed to load server info:", err);
      }
    }

    function switchTab(tab) {
      document.getElementById('tabLive').classList.toggle('active', tab === 'live');
      document.getElementById('tabSaved').classList.toggle('active', tab === 'saved');
      document.getElementById('tabLiveBtn').classList.toggle('active', tab === 'live');
      document.getElementById('tabSavedBtn').classList.toggle('active', tab === 'saved');
    }

    async function changeVersion(ver) {
      try {
        const res = await fetch('/api/switch_version', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ version: ver })
        });
        const data = await res.json();
        document.getElementById('chatMessages').innerHTML = `
          <div class="empty-state">
            <h3>Đã chuyển sang ${ver}</h3>
            <p>Artifact phiên bản đã nạp lại. Toàn bộ hội thoại được làm mới theo phiên bản này.</p>
          </div>
        `;
        await loadInfo();
      } catch (err) {
        alert("Lỗi khi chuyển phiên bản: " + err.message);
      }
    }

    async function resetChat() {
      if (!confirm("Bắt đầu cuộc hội thoại mới? File transcript hiện tại đã được lưu an toàn.")) return;
      try {
        await fetch('/api/reset', { method: 'POST' });
        document.getElementById('chatMessages').innerHTML = `
          <div class="empty-state">
            <h3>Hội thoại mới</h3>
            <p>Phiên chat mới đã được khởi tạo. Hãy nhập câu hỏi hoặc chọn test mẫu.</p>
          </div>
        `;
        await loadInfo();
      } catch (err) {
        alert("Lỗi reset: " + err.message);
      }
    }

    function quickSend(text) {
      document.getElementById('userInput').value = text;
      sendChat();
    }

    async function sendChat() {
      const inputEl = document.getElementById('userInput');
      const text = inputEl.value.trim();
      if (!text || isSending) return;

      const emptyEl = document.getElementById('emptyState');
      if (emptyEl) emptyEl.remove();

      const messagesContainer = document.getElementById('chatMessages');

      // Append User message
      const now = new Date().toLocaleTimeString();
      const userDiv = document.createElement('div');
      userDiv.className = 'msg-group';
      userDiv.innerHTML = `
        <div class="msg-user">${escapeHtml(text)}</div>
        <div class="msg-user-meta">${now}</div>
      `;
      messagesContainer.appendChild(userDiv);
      inputEl.value = '';
      messagesContainer.scrollTop = messagesContainer.scrollHeight;

      // Disable input while loading
      isSending = true;
      const sendBtn = document.getElementById('sendBtn');
      sendBtn.innerHTML = '<span class="loading-spinner"></span>';
      sendBtn.disabled = true;

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text })
        });
        const data = await res.json();

        currentTranscript = data.transcript;
        renderLiveTranscript(data.transcript);

        const turn = data.turn;
        renderTurnResponse(messagesContainer, turn);
      } catch (err) {
        const errDiv = document.createElement('div');
        errDiv.className = 'tool-card error';
        errDiv.innerHTML = `
          <div class="tool-header">
            <span class="tool-badge">LỖI KẾT NỐI</span>
            <span class="tool-status-tag status-tag-error">ERROR</span>
          </div>
          <pre class="code-box err">${escapeHtml(err.message)}</pre>
        `;
        messagesContainer.appendChild(errDiv);
      } finally {
        isSending = false;
        sendBtn.innerHTML = '<span>Gửi</span>';
        sendBtn.disabled = false;
        inputEl.focus();
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
      }
    }

    function renderTurnResponse(container, turn) {
      // If there are tool events in this turn, render Tool Cards clearly
      if (turn.tool_events && turn.tool_events.length > 0) {
        turn.tool_events.forEach(evt => {
          const card = document.createElement('div');
          const isWrite = evt.tool === 'create_ticket';
          const res = evt.result || {};
          const isError = res.error || evt.error;
          const isWaiting = res.awaiting_user;

          let cardClass = 'tool-card';
          let statusTag = '<span class="tool-status-tag status-tag-ok">SUCCESS</span>';
          if (isError) {
            cardClass += ' error';
            statusTag = `<span class="tool-status-tag status-tag-error">TOOL ERROR (${escapeHtml(res.error || evt.error)})</span>`;
          } else if (isWaiting) {
            cardClass += ' waiting';
            statusTag = '<span class="tool-status-tag status-tag-wait">AWAITING USER</span>';
          }

          card.className = cardClass;
          card.innerHTML = `
            <div class="tool-header">
              <span class="tool-badge ${isWrite ? 'write' : ''}">
                ${isWrite ? '📝' : '🔧'} ${escapeHtml(evt.tool)}
              </span>
              ${statusTag}
            </div>

            <div class="tool-section">
              <div class="tool-label">Input Parameters (Arguments):</div>
              <pre class="code-box">${escapeHtml(JSON.stringify(evt.args || {}, null, 2))}</pre>
            </div>

            <div class="tool-section">
              <div class="tool-label">Tool Execution Result / Output:</div>
              <pre class="code-box ${isError ? 'err' : ''}">${escapeHtml(JSON.stringify(res, null, 2))}</pre>
            </div>
          `;
          container.appendChild(card);
        });
      }

      // Render Assistant Text Reply
      if (turn.assistant_text) {
        const assistantDiv = document.createElement('div');
        assistantDiv.className = 'msg-group';
        assistantDiv.innerHTML = `
          <div class="msg-assistant">
            ${formatAssistantText(turn.assistant_text)}
          </div>
        `;
        container.appendChild(assistantDiv);
      }
    }

    function formatAssistantText(text) {
      if (!text) return '';
      // Clean JSON code blocks if formatted as json
      if (text.startsWith('```json') && text.endsWith('```')) {
        const content = text.replace(/^```json\\n/, '').replace(/\\n```$/, '');
        try {
          const parsed = JSON.parse(content);
          if (parsed.reply) {
            return `<div>${escapeHtml(parsed.reply)}</div><pre>${escapeHtml(JSON.stringify(parsed, null, 2))}</pre>`;
          }
        } catch(e) {}
      }
      return escapeHtml(text).replace(/\\n/g, '<br>');
    }

    function renderLiveTranscript(transcript) {
      const viewer = document.getElementById('liveTranscriptView');
      viewer.textContent = JSON.stringify(transcript, null, 2);
    }

    async function viewSavedTranscript(filename) {
      try {
        const res = await fetch('/api/transcript?file=' + encodeURIComponent(filename));
        const data = await res.json();
        currentTranscript = data;
        renderLiveTranscript(data);
        document.getElementById('transcriptIdLabel').textContent = filename + " (Đã lưu)";
        switchTab('live');
      } catch (err) {
        alert("Lỗi tải transcript: " + err.message);
      }
    }

    function copyTranscript() {
      const viewer = document.getElementById('liveTranscriptView');
      navigator.clipboard.writeText(viewer.textContent).then(() => {
        alert("Đã sao chép nội dung Transcript JSON vào Clipboard!");
      }).catch(err => {
        alert("Lỗi sao chép: " + err);
      });
    }

    function downloadTranscript() {
      const text = document.getElementById('liveTranscriptView').textContent;
      const fn = (document.getElementById('transcriptIdLabel').textContent || 'transcript') + '.json';
      const blob = new Blob([text], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = fn.endsWith('.json') ? fn : fn + '.json';
      a.click();
      URL.revokeObjectURL(a.href);
    }

    function escapeHtml(str) {
      if (typeof str !== 'string') return String(str);
      return str.replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
    }

    // Initialize
    loadInfo().then(() => {
      fetch('/api/transcript?file=current').then(r => r.json()).then(d => {
        currentTranscript = d;
        renderLiveTranscript(d);
      }).catch(() => {});
    });
  </script>
</body>
</html>
"""


class AgentHttpHandler(SimpleHTTPRequestHandler):
    session: AgentSession

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in {"/", "/index.html"}:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return

        if path == "/api/info":
            info = self.session.get_info()
            self._send_json(info)
            return

        if path == "/api/transcript":
            file_name = query.get("file", ["current"])[0]
            if file_name == "current":
                self._send_json(self.session.transcript)
                return
            target_path = TRANSCRIPTS_DIR / file_name
            if target_path.exists() and target_path.is_file():
                content = json.loads(target_path.read_text(encoding="utf-8"))
                self._send_json(content)
            else:
                self._send_json({"error": "not_found", "message": f"File {file_name} not found"}, 404)
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/chat":
            user_text = payload.get("message", "").strip()
            if not user_text:
                self._send_json({"error": "empty_message"}, 400)
                return
            result = self.session.chat_turn(user_text)
            self._send_json(result)
            return

        if path == "/api/reset":
            info = self.session.reset_chat()
            self._send_json(info)
            return

        if path == "/api/switch_version":
            new_ver = payload.get("version", "v3")
            info = self.session.set_version(new_ver)
            self._send_json(info)
            return

        self._send_json({"error": "not_found"}, 404)

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep server log clean
        sys.stderr.write(f"[{datetime.now().strftime('%H:%M:%S')}] {format % args}\n")


def find_available_port(start_port: int, max_attempts: int = 10) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start_port


def main() -> None:
    parser = argparse.ArgumentParser(description="IT Helpdesk Agent Web UI & Transcript Viewer.")
    parser.add_argument("--provider", default="gemini", choices=["gemini", "openai", "anthropic", "openrouter"])
    parser.add_argument("--version", default="v3", help="Artifact version, e.g. v0, v1, v2, v3")
    parser.add_argument("--model", default=None, help="Custom model override")
    parser.add_argument("--port", type=int, default=7860, help="Web server port (default 7860)")
    parser.add_argument("--host", default="0.0.0.0", help="Web server bind host (default 0.0.0.0)")
    args = parser.parse_args()

    port = find_available_port(args.port)
    session = AgentSession(
        provider_name=args.provider,
        version=args.version,
        model=args.model,
    )

    AgentHttpHandler.session = session
    server = HTTPServer((args.host, port), AgentHttpHandler)

    print("=" * 70)
    print("🚀 IT Helpdesk Agent Web UI & Transcript Viewer")
    print(f"👉 Local URL:    http://localhost:{port}")
    print(f"👉 Network URL:  http://127.0.0.1:{port}")
    print(f"📦 Version:      {session.version} ({session.artifact_version.artifact_version})")
    print(f"🤖 Provider:     {session.provider_name} (Model: {session.model})")
    print(f"📝 Transcript:   {session.transcript_path}")
    print("=" * 70)
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.server_close()


if __name__ == "__main__":
    main()
