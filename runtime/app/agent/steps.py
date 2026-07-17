from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from app.agent.context_budget import budgeted_observations_for_model
from app.project.config import load_project_config


@dataclass(slots=True)
class AgentStep:
    action: str
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
            "source": self.source,
        }
        if self.tool:
            payload["tool"] = self.tool
            payload["args"] = self.args
            payload["step_key"] = step_key(self.tool, self.args)
        return payload


TOOL_DOCS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "read_only": True,
        "description": "List files in the main workspace or a configured read-only workspace.",
        "args": {"path": ".", "workspace": "optional configured workspace name", "max_depth": 1, "limit": 40},
    },
    {
        "name": "find_files",
        "read_only": True,
        "description": "Find files by path or filename query in the main workspace or a configured read-only workspace.",
        "args": {"query": "filename or glob", "path": ".", "workspace": "optional configured workspace name", "limit": 20},
    },
    {
        "name": "detect_project",
        "read_only": True,
        "description": "Detect languages, package manager, and test command.",
        "args": {},
    },
    {
        "name": "git_status",
        "read_only": True,
        "description": "Read git status --short in the main workspace or a configured read-only workspace.",
        "args": {"workspace": "optional configured workspace name"},
    },
    {
        "name": "git_diff",
        "read_only": True,
        "description": "Read current git diff in the main workspace or a configured read-only workspace.",
        "args": {"path": "optional relative path", "workspace": "optional configured workspace name"},
    },
    {
        "name": "git_show",
        "read_only": True,
        "description": "Read git show --stat --oneline for a ref in the main workspace or a configured read-only workspace.",
        "args": {"ref": "HEAD", "workspace": "optional configured workspace name"},
    },
    {
        "name": "read_file",
        "read_only": True,
        "description": "Read a UTF-8 text file inside the main workspace or a configured read-only workspace.",
        "args": {"path": "relative/path", "workspace": "optional configured workspace name", "max_bytes": 30000},
    },
    {
        "name": "search_text",
        "read_only": True,
        "description": "Search text inside the main workspace or a configured read-only workspace.",
        "args": {"query": "keyword", "workspace": "optional configured workspace name", "limit": 40},
    },
    {
        "name": "review_diff",
        "read_only": True,
        "description": "Run deterministic review rules over the current diff.",
        "args": {},
    },
    {
        "name": "run_tests",
        "read_only": False,
        "description": "Run the detected or provided low-risk test command.",
        "args": {"timeout": 120},
    },
    {
        "name": "run_shell",
        "read_only": False,
        "description": "Run an explicit shell command after policy checks; medium-risk commands require user approval.",
        "args": {"command": "python3 -m pytest", "timeout": 120},
    },
]

READ_ONLY_TOOLS = {tool["name"] for tool in TOOL_DOCS if tool["read_only"]}
MAX_DYNAMIC_CONTEXT_READS = 7
MAX_RELATED_DEPENDENCY_QUERIES = 3
MAX_RELATED_TEST_QUERIES = 2
PYTHON_COMMON_EXTERNAL_MODULES = {
    "argparse",
    "asyncio",
    "collections",
    "contextlib",
    "dataclasses",
    "datetime",
    "functools",
    "json",
    "logging",
    "os",
    "pathlib",
    "re",
    "subprocess",
    "sys",
    "typing",
    "unittest",
}
JS_COMMON_EXTERNAL_MODULES = {
    "axios",
    "lodash",
    "next",
    "react",
    "react-dom",
    "vue",
}
GO_COMMON_EXTERNAL_MODULES = {
    "context",
    "encoding/json",
    "errors",
    "fmt",
    "io",
    "log",
    "net/http",
    "os",
    "path/filepath",
    "strings",
    "sync",
    "testing",
    "time",
}


def allowed_tool_names(mode: str) -> set[str]:
    if mode == "review":
        return set(READ_ONLY_TOOLS)
    return {tool["name"] for tool in TOOL_DOCS}


def allowed_tool_docs(mode: str) -> list[dict[str, Any]]:
    names = allowed_tool_names(mode)
    return [tool for tool in TOOL_DOCS if tool["name"] in names]


def choose_rule_step(message: str, mode: str, observations: list[dict[str, Any]], context_tools: list[tuple[str, dict[str, Any]]]) -> AgentStep:
    sequence = [
        ("list_files", {"path": ".", "max_depth": 1, "limit": 40}),
        ("detect_project", {}),
        ("git_status", {}),
        *context_tools,
    ]
    allowed = allowed_tool_names(mode)
    for tool, args in sequence:
        if tool not in allowed:
            continue
        if not observation_seen(observations, tool, args):
            return AgentStep(action="tool", tool=tool, args=args, reason=f"collect context with {tool}", source="rules")
    dynamic_step = choose_dynamic_context_step(observations, allowed)
    if dynamic_step is not None:
        return dynamic_step
    return AgentStep(action="finish", reason="required context is collected", source="rules")


def choose_dynamic_context_step(observations: list[dict[str, Any]], allowed: set[str]) -> AgentStep | None:
    if "read_file" in allowed and count_tool_observations(observations, "read_file") < MAX_DYNAMIC_CONTEXT_READS:
        find_result_step = choose_context_candidate_read_step(observations, preferred_tools={"find_files"})
        if find_result_step is not None:
            return find_result_step

    if "find_files" in allowed:
        test_step = choose_related_test_search_step(observations)
        if test_step is not None:
            return test_step
        dependency_step = choose_related_dependency_search_step(observations)
        if dependency_step is not None:
            return dependency_step

    if "read_file" not in allowed:
        return None
    if count_tool_observations(observations, "read_file") >= MAX_DYNAMIC_CONTEXT_READS:
        return None

    return choose_context_candidate_read_step(observations)


def choose_context_candidate_read_step(observations: list[dict[str, Any]], preferred_tools: set[str] | None = None) -> AgentStep | None:
    for observation in observations:
        if preferred_tools is not None and observation.get("tool") not in preferred_tools:
            continue
        for candidate in context_file_candidates(observation):
            args: dict[str, Any] = {"path": candidate["path"], "max_bytes": 24_000}
            if candidate["workspace"] != "main":
                args["workspace"] = candidate["workspace"]
            if read_file_seen(observations, args):
                continue
            return AgentStep(
                action="tool",
                tool="read_file",
                args=args,
                reason=f"read located context file {candidate['path']}",
                source="rules",
            )
    return None


def choose_related_test_search_step(observations: list[dict[str, Any]]) -> AgentStep | None:
    for observation in observations:
        if observation.get("tool") != "read_file" or not observation.get("success"):
            continue
        data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        workspace = str(data.get("workspace") or "main")
        path = strip_workspace_prefix(str(data.get("path") or ""), workspace)
        for query in related_test_queries(path):
            args: dict[str, Any] = {"query": query, "limit": 20}
            if workspace != "main":
                args["workspace"] = workspace
            if not observation_seen(observations, "find_files", args):
                return AgentStep(
                    action="tool",
                    tool="find_files",
                    args=args,
                    reason=f"locate related test file for {path}",
                    source="rules",
                )
    return None


def choose_related_dependency_search_step(observations: list[dict[str, Any]]) -> AgentStep | None:
    for observation in observations:
        if observation.get("tool") != "read_file" or not observation.get("success"):
            continue
        data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        workspace = str(data.get("workspace") or "main")
        path = strip_workspace_prefix(str(data.get("path") or ""), workspace)
        if is_test_path(path):
            continue
        text = read_file_text_from_observation(observation)
        for query in related_dependency_queries(path, text):
            args: dict[str, Any] = {"query": query, "limit": 20}
            if workspace != "main":
                args["workspace"] = workspace
            if not observation_seen(observations, "find_files", args):
                return AgentStep(
                    action="tool",
                    tool="find_files",
                    args=args,
                    reason=f"locate dependency context for {path}",
                    source="rules",
                )
    return None


def count_tool_observations(observations: list[dict[str, Any]], tool: str) -> int:
    return sum(1 for observation in observations if observation.get("tool") == tool)


def context_file_candidates(observation: dict[str, Any]) -> list[dict[str, str]]:
    if not observation.get("success"):
        return []
    data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
    workspace = str(data.get("workspace") or "main")
    if observation.get("tool") == "find_files":
        files = data.get("files") if isinstance(data.get("files"), list) else []
        return normalize_candidate_files(files, workspace)
    if observation.get("tool") == "search_text":
        matches = data.get("matches") if isinstance(data.get("matches"), list) else []
        return normalize_candidate_files([search_match_path(str(match), workspace) for match in matches], workspace)
    return []


def normalize_candidate_files(raw_files: list[Any], workspace: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_files:
        path = normalize_context_path(strip_workspace_prefix(str(raw or "").strip(), workspace))
        if not path:
            continue
        key = (workspace, path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"workspace": workspace, "path": path})
        if len(candidates) >= MAX_DYNAMIC_CONTEXT_READS:
            break
    return candidates


def search_match_path(match: str, workspace: str) -> str:
    match = strip_workspace_prefix(match, workspace)
    path, _, _rest = match.partition(":")
    return normalize_context_path(path)


def strip_workspace_prefix(value: str, workspace: str) -> str:
    if workspace != "main":
        prefix = f"{workspace}:"
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def normalize_context_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def read_file_seen(observations: list[dict[str, Any]], args: dict[str, Any]) -> bool:
    target_path = normalize_context_path(str(args.get("path") or ""))
    target_workspace = str(args.get("workspace") or "main")
    for observation in observations:
        if observation.get("tool") != "read_file":
            continue
        obs_args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
        obs_workspace = str(obs_args.get("workspace") or "main")
        obs_path = normalize_context_path(str(obs_args.get("path") or ""))
        data = observation.get("data") if isinstance(observation.get("data"), dict) else {}
        data_workspace = str(data.get("workspace") or obs_workspace or "main")
        data_path = normalize_context_path(strip_workspace_prefix(str(data.get("path") or obs_path), data_workspace))
        if target_workspace == data_workspace and target_path == data_path:
            return True
    return False


def related_test_queries(path: str) -> list[str]:
    path = path.replace("\\", "/").strip()
    if not path or is_test_path(path):
        return []
    candidate = PurePosixPath(path)
    suffix = candidate.suffix.lower()
    stem = candidate.stem
    queries: list[str] = []
    if suffix == ".py":
        queries.extend([f"test_{stem}.py", f"{stem}_test.py"])
    elif suffix == ".go":
        queries.append(f"{stem}_test.go")
    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        queries.extend([f"{stem}.test{suffix}", f"{stem}.spec{suffix}"])
    return queries[:MAX_RELATED_TEST_QUERIES]


def related_dependency_queries(path: str, text: str) -> list[str]:
    path = normalize_context_path(path)
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        return python_dependency_queries(path, text)
    if suffix == ".go":
        return go_dependency_queries(text)
    if suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return js_dependency_queries(path, text)
    return []


def python_dependency_queries(path: str, text: str) -> list[str]:
    queries: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        from_match = re.match(r"from\s+([.\w]+)\s+import\s+([\w*,\s]+)", stripped)
        if from_match:
            module = from_match.group(1)
            imported = from_match.group(2)
            queries.extend(python_module_queries(path, module))
            if module == ".":
                queries.extend(f"{name}.py" for name in imported_python_names(imported))
            continue
        import_match = re.match(r"import\s+(.+)", stripped)
        if import_match:
            imports = import_match.group(1).split(",")
            for item in imports:
                module = item.strip().split(" as ", 1)[0].strip()
                queries.extend(python_module_queries(path, module))
    return unique_queries(queries, MAX_RELATED_DEPENDENCY_QUERIES)


def python_module_queries(path: str, module: str) -> list[str]:
    module = module.strip()
    if not module:
        return []
    if module.startswith("."):
        base_dir = PurePosixPath(path).parent
        leading_dots = len(module) - len(module.lstrip("."))
        relative = module[leading_dots:].replace(".", "/")
        if not relative:
            return []
        for _ in range(max(0, leading_dots - 1)):
            base_dir = base_dir.parent
        resolved = normalize_context_path(posixpath.normpath(str(base_dir / relative)))
        return [f"{resolved}.py", f"{PurePosixPath(resolved).name}.py"]
    root = module.split(".", 1)[0]
    if root in PYTHON_COMMON_EXTERNAL_MODULES:
        return []
    module_path = module.replace(".", "/")
    return [f"{module_path}.py", f"{PurePosixPath(module_path).name}.py"]


def imported_python_names(raw: str) -> list[str]:
    names: list[str] = []
    for item in raw.split(","):
        name = item.strip().split(" as ", 1)[0].strip()
        if name and name != "*":
            names.append(name)
    return names


def go_dependency_queries(text: str) -> list[str]:
    queries: list[str] = []
    for module in re.findall(r'"([^"]+)"', text):
        if module in GO_COMMON_EXTERNAL_MODULES:
            continue
        if "/" not in module and "." not in module:
            continue
        normalized = module.strip("./")
        if not normalized:
            continue
        queries.append(PurePosixPath(normalized).name)
    return unique_queries(queries, MAX_RELATED_DEPENDENCY_QUERIES)


def js_dependency_queries(path: str, text: str) -> list[str]:
    queries: list[str] = []
    patterns = [
        r"\bimport\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]",
        r"\bexport\s+.+?\s+from\s+['\"]([^'\"]+)['\"]",
        r"\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)",
    ]
    for pattern in patterns:
        for module in re.findall(pattern, text):
            queries.extend(js_module_queries(path, module))
    return unique_queries(queries, MAX_RELATED_DEPENDENCY_QUERIES)


def js_module_queries(path: str, module: str) -> list[str]:
    module = module.strip()
    if not module:
        return []
    if module.startswith("."):
        base_dir = PurePosixPath(path).parent
        resolved = normalize_context_path(posixpath.normpath(str(base_dir / module)))
        return [resolved, *js_extension_queries(resolved), PurePosixPath(resolved).name]
    if module.startswith("@/"):
        normalized = normalize_context_path(module[2:])
        return [normalized, *js_extension_queries(normalized), PurePosixPath(normalized).name]
    root = module.split("/", 1)[0]
    if root in JS_COMMON_EXTERNAL_MODULES or module.startswith("@"):
        return []
    if "/" in module:
        normalized = normalize_context_path(module)
        return [normalized, *js_extension_queries(normalized), PurePosixPath(normalized).name]
    return []


def js_extension_queries(path: str) -> list[str]:
    if PurePosixPath(path).suffix:
        return [path]
    return [f"{path}.ts", f"{path}.tsx", f"{path}.js", f"{path}.jsx"]


def unique_queries(queries: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = normalize_context_path(query).strip()
        if not normalized or normalized == "." or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if len(out) >= limit:
            break
    return out


def read_file_text_from_observation(observation: dict[str, Any]) -> str:
    text = str(observation.get("text") or "")
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join(lines[1:])
    return text


def is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    name = parts[-1] if parts else normalized
    if any(part in {"test", "tests", "__tests__"} for part in parts[:-1]):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    return any(marker in name for marker in [".test.", ".spec."])


def build_planner_messages(
    *,
    language: str,
    message: str,
    mode: str,
    workspace: str,
    observations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    language_name = "English" if language.startswith("en") else "中文"
    system = (
        f"你是 aicode 的工具规划器。使用{language_name}思考，但只能输出一个 JSON object。"
        "不要输出 Markdown。不要解释。"
        '格式只能是 {"action":"tool","tool":"read_file","args":{"path":"README.md"},"reason":"..."} '
        '或 {"action":"finish","reason":"..."}。'
        "只能选择 allowed_tools 中的工具。review 模式只允许只读分析。"
        "如果 observation 带 context_compacted，说明部分输出被预算层压缩；不要猜测被省略内容。"
    )
    budgeted_observations, context_budget = budgeted_observations_for_model(observations, "planner")
    payload = {
        "user_request": message,
        "mode": mode,
        "workspace": workspace,
        "configured_workspaces": configured_workspace_docs(workspace),
        "allowed_tools": allowed_tool_docs(mode),
        "observations": budgeted_observations,
        "context_budget": context_budget,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def parse_agent_step(text: str, allowed_tools: set[str]) -> AgentStep | None:
    raw = extract_json_object(text)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if action == "finish":
        return AgentStep(action="finish", reason=reason or "planner finished", source="model")
    if action != "tool":
        return None

    tool = str(payload.get("tool") or "").strip()
    if tool not in allowed_tools:
        return None
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        return None
    return AgentStep(action="tool", tool=tool, args=args, reason=reason or f"planner selected {tool}", source="model")


def extract_json_object(text: str) -> str | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < start:
        return None
    return candidate[start : end + 1]


def configured_workspace_docs(workspace: str) -> list[dict[str, str]]:
    config = load_project_config(Path(workspace))
    return [{"name": ref.name, "mode": ref.mode} for ref in config.workspaces]


def observation_seen(observations: list[dict[str, Any]], tool: str, args: dict[str, Any]) -> bool:
    expected = step_key(tool, args)
    for observation in observations:
        if observation.get("step_key") == expected:
            return True
        if "step_key" not in observation and observation.get("tool") == tool:
            return True
    return False


def step_key(tool: str, args: dict[str, Any]) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True, default=str)
