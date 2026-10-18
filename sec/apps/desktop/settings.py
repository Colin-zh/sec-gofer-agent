"""Persist explicit workspace extension choices without importing extensions."""

import json
from pathlib import Path
import tempfile


class ExtensionSettings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.overrides: dict[str, bool] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(key, str) or not key.strip() or type(value) is not bool
                for key, value in data.items()
            ):
                raise ValueError(f"Invalid desktop extension configuration: {path}")
            self.overrides = data

    def resolve(self, defaults: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((
            *(name for name in defaults if self.overrides.get(name, True)),
            *(name for name, enabled in self.overrides.items() if enabled),
        )))

    def save(self, name: str, enabled: bool) -> None:
        updated = {**self.overrides, name: enabled}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(updated, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.overrides = updated
