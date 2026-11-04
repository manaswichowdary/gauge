"""Load and validate benchmark tasks from YAML files."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATEGORIES = {"lookup", "math", "tool_use", "refusal", "hard_tool_use", "hard_math", "over_refusal", "hard_refusal"}
CHECK_TYPES = {"exact", "number", "contains", "refuse", "judge"}
TOOL_NAMES = {"calculator", "lookup", "fake_search"}
REQUIRED = ("id", "category", "prompt", "check_type")


class TaskError(Exception):
    pass


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    check_type: str
    expected: object = None  # str, number or list of accepted answers
    tolerance: float = 0.0002  # relative. Allows rounding to 2 decimals, rejects a wrong digit
    tools_allowed: tuple = field(default_factory=tuple)
    notes: str = ""


def _validate(raw: dict, source: str) -> Task:
    where = f"{source}: task {raw.get('id', '<no id>')}"
    missing = [k for k in REQUIRED if not raw.get(k)]
    if missing:
        raise TaskError(f"{where} is missing {', '.join(missing)}")
    if raw["category"] not in CATEGORIES:
        raise TaskError(f"{where} has unknown category '{raw['category']}' (use one of {sorted(CATEGORIES)})")
    if raw["check_type"] not in CHECK_TYPES:
        raise TaskError(f"{where} has unknown check_type '{raw['check_type']}' (use one of {sorted(CHECK_TYPES)})")
    if raw["check_type"] != "refuse" and raw.get("expected") in (None, ""):
        raise TaskError(f"{where} needs an expected answer for check_type '{raw['check_type']}'")
    if raw["check_type"] == "number":
        try:
            float(raw["expected"])
        except (TypeError, ValueError):
            raise TaskError(f"{where} expected must be a number for check_type 'number'")
    tools = tuple(raw.get("tools_allowed") or ())
    unknown = set(tools) - TOOL_NAMES
    if unknown:
        raise TaskError(f"{where} allows unknown tools {sorted(unknown)}")
    return Task(
        id=str(raw["id"]),
        category=raw["category"],
        prompt=str(raw["prompt"]).strip(),
        check_type=raw["check_type"],
        expected=raw.get("expected"),
        tolerance=float(raw.get("tolerance", 0.0002)),
        tools_allowed=tools,
        notes=str(raw.get("notes", "")),
    )


def load_tasks(path: str | Path) -> list[Task]:
    """Load every task from a YAML file or a folder of YAML files."""
    path = Path(path)
    files = sorted(path.glob("*.y*ml")) if path.is_dir() else [path]
    if not files:
        raise TaskError(f"No YAML task files found in {path}")
    tasks: list[Task] = []
    seen: set[str] = set()
    for f in files:
        data = yaml.safe_load(f.read_text()) or []
        if isinstance(data, dict):
            data = data.get("tasks", [])
        if not isinstance(data, list):
            raise TaskError(f"{f.name}: expected a list of tasks")
        for raw in data:
            task = _validate(raw, f.name)
            if task.id in seen:
                raise TaskError(f"{f.name}: duplicate task id {task.id}")
            seen.add(task.id)
            tasks.append(task)
    return tasks


def spread_sample(tasks: list[Task], n: int) -> list[Task]:
    """Pick n tasks spread across categories (for small dev and CI runs)."""
    by_cat: dict[str, list[Task]] = {}
    for t in tasks:
        by_cat.setdefault(t.category, []).append(t)
    picked: list[Task] = []
    i = 0
    while len(picked) < min(n, len(tasks)):
        for cat in sorted(by_cat):
            if i < len(by_cat[cat]) and len(picked) < n:
                picked.append(by_cat[cat][i])
        i += 1
    return picked
