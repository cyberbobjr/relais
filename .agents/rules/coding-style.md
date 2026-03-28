---
trigger: always_on
---

# Python Coding Style

> This file extends [common/coding-style.md](../common/coding-style.md) with Python specific content.

## Documentation (PyDoc)

Every function and method **MUST** include a docstring following the **Google Style** format:
- **Summary**: A concise 2-3 line explanation of the purpose.
- **Args**: Explicit description of each parameter with its role.
- **Returns**: Clear description of the return value and its type.
- **Raises**: (Optional) List exceptions that are explicitly raised.

## Standards & Typing

- **PEP 8**: Strict adherence required.
- **Type Annotations**: Mandatory for all function signatures (parameters and return types).
- **Modern Syntax**: Use Python 3.10+ typing features (e.g., `str | int` instead of `Union[str, int]`, and built-in collections like `list[str]` instead of `List[str]`).

## Immutability & Data Modeling

Prioritize immutable structures to prevent side effects:
- Use `@dataclass(frozen=True)` for complex data objects.
- Use `NamedTuple` for simple record-like structures.
- Use `MappingProxyType` or `Final` from `typing` where appropriate to signal intent.

## Tooling & Linting

The agent must ensure code is compatible with:
- **Ruff**: Use as the primary linter and formatter (replaces Black, isort, and Flake8).
- **Strict Mode**: Treat type-checking failures (via Mypy/Pyright) as blockers.