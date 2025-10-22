from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field


router = APIRouter(prefix="/tools", tags=["tools"])


# Resolve repository root (workspace) = two levels up from this file: .../trainflow/
REPO_ROOT = Path(__file__).resolve().parents[2]


def _safe_path(rel_or_abs: str) -> Path:
    """Resolve a user-supplied path and ensure it stays within the repository root."""
    p = Path(rel_or_abs)
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    else:
        p = p.resolve()
    if REPO_ROOT not in p.parents and p != REPO_ROOT:
        raise ValueError("Path escapes repository root")
    return p


class FileReadRequest(BaseModel):
    path: str = Field(..., description="File path relative to repo root or absolute within repo")
    max_bytes: int = Field(1_000_000, description="Maximum bytes to read (default 1MB)")


class FileWriteRequest(BaseModel):
    path: str
    content: str
    create_dirs: bool = Field(True, description="Create parent directories if missing")


@router.post("/file/read")
def read_file(req: FileReadRequest) -> dict:
    target = _safe_path(req.path)
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": f"Not a file: {target}"}
    data = target.read_bytes()[: req.max_bytes]
    try:
        text = data.decode("utf-8")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    return {"ok": True, "path": str(target.relative_to(REPO_ROOT)), "content": text}


@router.post("/file/write")
def write_file(req: FileWriteRequest) -> dict:
    target = _safe_path(req.path)
    if req.create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(req.content, encoding="utf-8")
    return {"ok": True, "path": str(target.relative_to(REPO_ROOT)), "bytes": len(req.content.encode("utf-8"))}


class GitCommitRequest(BaseModel):
    message: str
    add_all: bool = True


def _run(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = "<timeout>"
    return proc.returncode, out


@router.post("/git/status")
def git_status() -> dict:
    code, out = _run(["git", "status", "--porcelain=v1"], cwd=REPO_ROOT)
    return {"ok": code == 0, "output": out}


@router.post("/git/diff")
def git_diff() -> dict:
    code, out = _run(["git", "diff"], cwd=REPO_ROOT)
    return {"ok": code == 0, "output": out}


@router.post("/git/commit")
def git_commit(req: GitCommitRequest) -> dict:
    if req.add_all:
        _run(["git", "add", "-A"], cwd=REPO_ROOT)
    code, out = _run(["git", "commit", "-m", req.message], cwd=REPO_ROOT)
    return {"ok": code == 0, "output": out}


class RunRequest(BaseModel):
    project: str = Field("frontend", description="frontend|backend")
    command: Optional[str] = Field(None, description="optional custom command override")


@router.post("/run/lint")
def run_lint(req: RunRequest) -> dict:
    if req.project == "frontend":
        cmd = req.command.split() if req.command else ["npm", "run", "lint", "--", "--no-open"]
        cwd = REPO_ROOT / "TrainFlow"
    else:
        # No Python linter configured; run a basic syntax check over backend
        cmd = req.command.split() if req.command else ["python", "-m", "py_compile", "app/main.py"]
        cwd = REPO_ROOT / "backtrader.BE"
    code, out = _run(cmd, cwd=cwd)
    return {"ok": code == 0, "output": out}


@router.post("/run/typecheck")
def run_typecheck(req: RunRequest) -> dict:
    if req.project == "frontend":
        cmd = req.command.split() if req.command else ["npx", "tsc", "--noEmit"]
        cwd = REPO_ROOT / "TrainFlow"
    else:
        # No mypy config; provide stub response
        return {"ok": False, "error": "Python typecheck not configured"}
    code, out = _run(cmd, cwd=cwd)
    return {"ok": code == 0, "output": out}


@router.post("/run/pytest")
def run_pytest() -> dict:
    # Run python tests in backend if any
    cmd = ["pytest", "-q"]
    cwd = REPO_ROOT / "backtrader.BE"
    code, out = _run(cmd, cwd=cwd)
    return {"ok": code == 0, "output": out}

