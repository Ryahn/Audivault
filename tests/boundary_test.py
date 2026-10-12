"""core/ must not import catalog, scanner, providers, or management."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "app" / "core"
FORBIDDEN = ("app.catalog", "app.scanner", "app.providers", "app.management")

FAIL = []


def check(cond, msg):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def _package(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts[:-1])


def imported(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _package(path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                found.append(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = package.split(".")
            base = base[: len(base) - (node.level - 1)]
            if node.module:
                found.append(".".join(base + node.module.split(".")))
            else:
                found.append(".".join(base))
    return found


def main() -> int:
    files = sorted(p for p in CORE.glob("*.py"))
    check(bool(files), "app/core has modules to check")
    for path in files:
        bad = []
        for name in imported(path):
            hit = next((prefix for prefix in FORBIDDEN if name == prefix or name.startswith(prefix + ".")), None)
            if hit:
                bad.append(name)
        check(not bad, f"{path.relative_to(ROOT)} stays inside core" + (f" (imports {', '.join(bad)})" if bad else ""))
    if FAIL:
        print(f"\n{len(FAIL)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
