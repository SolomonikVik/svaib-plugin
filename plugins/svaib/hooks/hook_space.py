"""Корень клиентского пространства для хуков карты, спецификации и телеметрии.

`resolve_root` — корень сессии, один для всех хуков: от него карта и спецификация отсчитывают
пути. `ready_usage_root` берёт тот же корень и строже: пишет только в готовую `.svaib/usage/`
и молчит, когда действие могло уйти в другую базу. Пути корня — в записи хоста, реальные пути — только
для сравнения: иначе у пространства за симлинком цели не попадут внутрь корня.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable

ROOT_MARKERS = ("CLAUDE.md", "AGENTS.md")  # README.md есть у каждого узла — не маркер


def state_dir() -> str:
    """Состояние хуков вне пространства: маркеры сессий, курсоры, лог."""
    override = os.environ.get("SVAIB_STATE_DIR")  # стенд и тесты: свой каталог на прогон
    if override:
        return override
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local")), "svaib")
    return os.path.expanduser("~/.local/state/svaib")


def has_root_marker(path: str) -> bool:
    return any(os.path.isfile(os.path.join(path, name)) for name in ROOT_MARKERS)


def _walk_up(start: str, *, stop_at_home: bool = True):
    """Каталоги от start вверх; по умолчанию без HOME, где может лежать чужой маркер."""
    home = os.path.abspath(os.path.expanduser("~"))
    current = os.path.abspath(start)
    while not stop_at_home or os.path.normcase(current) != os.path.normcase(home):
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def _svaib_directory(path: str) -> bool:
    try:
        mode = os.lstat(os.path.join(path, ".svaib")).st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError("workspace_boundary_invalid") from error
    if not stat.S_ISDIR(mode):
        raise ValueError("workspace_boundary_invalid")
    return True


def find_svaib_root(start: str) -> str | None:
    """Ближайшая `.svaib/` от выбранного хостом start, без запасного поиска по маркерам.

    Содержимое привязки не проверяется: неполное пространство уже останавливает подъём.
    Невалидная запись `.svaib` прекращает поиск с отказом, не открывает родительскую базу.
    Путь сохраняет запись хоста, в том числе симлинк, как и остальные корни навигации.
    """
    for path in _walk_up(start, stop_at_home=False):
        if _svaib_directory(path):
            return path
    return None


def find_root(start: str) -> str | None:
    """Ближайшая `.svaib/`; только без неё — самый верхний CLAUDE.md/AGENTS.md."""
    try:
        space = find_svaib_root(start)
    except ValueError:
        return None
    if space is not None:
        return space
    found = None
    for path in _walk_up(start):
        if has_root_marker(path):
            found = path
    return found


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _workspace_folders() -> list[str]:
    """Cowork «Only on this computer»: подключённые папки из CLAUDE_CODE_WORKSPACE_HOST_PATHS.
    Несколько папок приложение склеивает через `|`; сначала значение целиком — вдруг `|`
    окажется в имени единственной папки."""
    raw = (os.environ.get("CLAUDE_CODE_WORKSPACE_HOST_PATHS") or "").strip()
    parts = [raw] if os.path.isdir(raw) else raw.split("|")
    folders: list[str] = []
    for part in (item.strip() for item in parts):
        if part and os.path.isdir(part) and not any(_same(part, seen) for seen in folders):
            folders.append(os.path.abspath(part))
    return folders


def workspace_root(on_ambiguous: Callable[[int, int, list[str]], None] | None = None) -> str | None:
    """Одна папка: с маркером — корень как есть (агенту Cowork доступна только она), без маркера —
    подъём к корню. Несколько папок: пространство — та, где лежит `.svaib/`, остальные просто
    подключены; ни одной или больше одной — корня нет."""
    folders = _workspace_folders()
    try:
        if len(folders) == 1:
            path = folders[0]
            if _svaib_directory(path):
                return path
            return path if has_root_marker(path) else find_root(path)
        spaces = [path for path in folders if _svaib_directory(path)]
    except ValueError:
        return None
    if len(spaces) == 1:
        return spaces[0]
    if folders and on_ambiguous:
        on_ambiguous(len(spaces), len(folders), folders)
    return None


def _cowork() -> bool:
    return os.environ.get("CLAUDE_CODE_IS_COWORK") == "1" and bool((os.environ.get("CLAUDE_CODE_WORKSPACE_HOST_PATHS") or "").strip())


def claude_project_dir() -> str | None:
    """Исходный каталог Claude от хоста; Bash cd не меняет эту привязку сессии."""
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project or "\0" in project or not os.path.isabs(project) or not os.path.isdir(project):
        return None
    return os.path.abspath(project)


def resolve_root(hook_input: dict, on_ambiguous: Callable[[int, int, list[str]], None] | None = None,
                 *, use_claude_env: bool = True) -> str | None:
    """Корень сессии. Cowork с подключёнными папками — их ответ окончательный, отказ тоже:
    служебные CLAUDE_PROJECT_DIR и cwd привели бы к чужому маркеру. Иначе исходный каталог
    CLAUDE_PROJECT_DIR задаёт сессию: ближайшая `.svaib/` важнее AGENTS-маркера подпапки.
    Только без `.svaib/` сохраняется навигационный fallback по маркерам.
    Codex передаёт use_claude_env=False: окружение Claude не определяет его пространство."""
    if not isinstance(hook_input, dict):
        return None
    if use_claude_env and _cowork():
        return workspace_root(on_ambiguous)
    project = claude_project_dir() if use_claude_env else None
    if project:
        try:
            space = find_svaib_root(project)
        except ValueError:
            return None
        if space is not None:
            return space
        if has_root_marker(project):
            return project
        return find_root(project)
    start = hook_input.get("cwd") or os.getcwd()
    if not isinstance(start, str) or "\0" in start:
        return None
    return find_root(start)


def _foreign(path: str, real_root: str) -> bool:
    """На пути от path к корню — другая база или свой проект: другая `.svaib/` или маркер."""
    for current in _walk_up(os.path.realpath(path)):
        if os.path.normcase(current) == os.path.normcase(real_root):
            return False
        if has_root_marker(current) or _svaib_directory(current):
            return True
    return False


def ready_usage_root(hook_input: dict, *, use_claude_env: bool = True) -> str | None:
    """База для строки учёта: корень сессии с `.svaib/usage/README.md`. Молчит, если действие
    могло уйти в другую базу: между cwd (в Cowork — любой подключённой папкой) и корнем лежит
    другая `.svaib/` или свой CLAUDE.md/AGENTS.md — вложенная база, копия клиента, отдельный
    проект, другая база вне корня. Так Claude и Codex в одной папке пишут одинаково."""
    try:
        root = resolve_root(hook_input, use_claude_env=use_claude_env)
        if not root or not os.path.isfile(os.path.join(root, ".svaib", "usage", "README.md")):
            return None
        real_root = os.path.realpath(root)
        if use_claude_env and _cowork():  # cwd у Cowork — служебная папка сессии, судят подключённые папки
            paths = _workspace_folders()
        else:
            cwd = hook_input.get("cwd")
            paths = [cwd] if isinstance(cwd, str) and os.path.isdir(cwd) else []
        return None if any(_foreign(path, real_root) for path in paths) else root
    except (OSError, TypeError, ValueError):
        return None
