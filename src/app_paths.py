import os
import shutil
import sys

APP_DIR_NAME = "Clai_TALOS"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def resource_root() -> str:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return os.path.realpath(getattr(sys, "_MEIPASS"))
    return source_root()


def executable_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.realpath(sys.executable))
    return source_root()


def _default_data_root() -> str:
    override = str(os.getenv("TALOS_DATA_DIR", "")).strip()
    if override:
        return os.path.realpath(os.path.expanduser(override))

    # Preserve current repo-local behavior for source/dev runs.
    if not is_frozen():
        return source_root()

    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.realpath(os.path.join(base, APP_DIR_NAME))

    if sys.platform == "darwin":
        return os.path.realpath(
            os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}")
        )

    xdg_data_home = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return os.path.realpath(os.path.join(os.path.expanduser(xdg_data_home), APP_DIR_NAME.lower()))

    return os.path.realpath(os.path.expanduser(f"~/.local/share/{APP_DIR_NAME.lower()}"))


def data_root() -> str:
    return _default_data_root()


def data_path(*parts: str) -> str:
    return os.path.realpath(os.path.join(data_root(), *parts))


def tools_resource_dir() -> str:
    return os.path.join(resource_root(), "tools")


def web_resource_dir() -> str:
    return os.path.join(resource_root(), "web")


def static_resource_dir() -> str:
    return os.path.join(web_resource_dir(), "static")


def system_prompt_resource_path() -> str:
    return os.path.join(resource_root(), "system_prompt.md")


def env_file_path() -> str:
    return data_path(".env")


def credentials_file_path() -> str:
    return data_path(".credentials")


def security_log_path() -> str:
    return data_path(".security.log")


def setup_config_path() -> str:
    return data_path(".setup_config")


def tools_config_path() -> str:
    return data_path(".tools_config")


def oauth_tokens_path() -> str:
    return data_path(".google_oauth.json")


def terminal_config_path() -> str:
    return data_path("terminal_config.json")


def db_path() -> str:
    return data_path("talos.db")


def logs_dir() -> str:
    return data_path("logs")


def browser_artifacts_dir() -> str:
    return data_path("logs", "browser")


def scrape_cache_dir() -> str:
    return data_path("logs", "scrape_cache")


def web_upload_dir() -> str:
    return data_path("logs", "web_uploads")


def community_hub_dir() -> str:
    return data_path("community_hub")


def community_hub_packages_dir() -> str:
    return data_path("community_hub", "packages")


def community_hub_index_path() -> str:
    return data_path("community_hub", "index.json")


def projects_dir() -> str:
    return data_path("projects")


def gateway_config_path() -> str:
    return data_path("projects", "gateway.json")


def dynamic_registry_path() -> str:
    return data_path("projects", "dynamic_tools.json")


def dynamic_tools_docs_dir() -> str:
    return data_path("tools")


def bin_dir() -> str:
    return data_path("bin")


def himalaya_dir() -> str:
    return data_path(".himalaya")


def ensure_runtime_dirs() -> None:
    for path in [
        data_root(),
        logs_dir(),
        browser_artifacts_dir(),
        scrape_cache_dir(),
        web_upload_dir(),
        community_hub_dir(),
        community_hub_packages_dir(),
        projects_dir(),
        bin_dir(),
        himalaya_dir(),
        dynamic_tools_docs_dir(),
    ]:
        os.makedirs(path, exist_ok=True)


def migrate_legacy_runtime_data() -> list[str]:
    """Migrate legacy files from executable directory into runtime data dir for frozen builds."""
    moved: list[str] = []
    if not is_frozen():
        return moved

    legacy_root = executable_dir()
    target_root = data_root()
    if os.path.realpath(legacy_root) == os.path.realpath(target_root):
        return moved

    items = [
        ".env",
        ".credentials",
        ".security.log",
        ".setup_config",
        ".tools_config",
        ".google_oauth.json",
        "talos.db",
        "talos.db-wal",
        "talos.db-shm",
        "terminal_config.json",
        "projects",
        "logs",
        "bin",
        ".himalaya",
    ]

    for rel in items:
        src = os.path.realpath(os.path.join(legacy_root, rel))
        dst = os.path.realpath(os.path.join(target_root, rel))

        if not os.path.exists(src) or os.path.exists(dst):
            continue

        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            moved.append(rel)
        except Exception:
            # Best-effort migration only.
            continue

    return moved
