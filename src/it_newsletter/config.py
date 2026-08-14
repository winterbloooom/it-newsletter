"""Loads the three config files and the secrets, and writes the site registry back.

Every tunable value in this project enters through here. Reading a setting
anywhere else, or defaulting one inline, is what this module exists to prevent.

`load_sites` and `save_sites` sit together on purpose: the CSV column order is
a format, and a format with two independent definitions drifts.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from it_newsletter.models import AppConfig, InterestsConfig, Settings, Site

ENV_ROOT = "IT_NEWSLETTER_ROOT"


def _project_root() -> Path:
    """The directory holding `config/` and `data/`.

    Found by searching outward from the working directory, not by walking up
    from this module. The module-relative form happened to work locally, where
    the package is installed editable and still sits inside the source tree,
    and broke the first time CI ran `pip install .`: the code then lives in
    site-packages and two levels up is `lib/python3.11`.

    That is the right model anyway. The config is a set of files a person edits
    and commits, so it belongs to the project being run, not to the copy of the
    code that happens to be running it.
    """
    override = os.environ.get(ENV_ROOT)
    if override:
        return Path(override).expanduser().resolve()

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "settings.yaml").is_file():
            return candidate

    # An editable install invoked from outside the tree still knows where its
    # source lives, so try that before giving up.
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "config" / "settings.yaml").is_file():
        return source_root

    # Nothing found: return the working directory so the error names the place
    # the user is actually standing rather than a path inside the interpreter.
    return here


PROJECT_ROOT = _project_root()
CONFIG_DIR = PROJECT_ROOT / "config"

SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
INTERESTS_PATH = CONFIG_DIR / "interests.yaml"
SITES_PATH = CONFIG_DIR / "sites.csv"

SITE_COLUMNS = ["name", "url", "fetcher", "source_url", "tz", "params", "enabled", "note"]


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Looked under {PROJECT_ROOT}. Run from the project directory, or "
            f"set {ENV_ROOT} to it."
        )
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_sites(path: Path = SITES_PATH) -> list[Site]:
    """Read the site registry.

    `utf-8-sig` because the registry is edited in spreadsheet tools, which
    write a byte-order mark that would otherwise end up inside the first
    column name.
    """
    if not path.exists():
        raise FileNotFoundError(f"Site registry not found: {path}")

    sites: list[Site] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row_number, row in enumerate(csv.DictReader(f), start=2):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            raw_params = (row.get("params") or "").strip()
            try:
                params = json.loads(raw_params) if raw_params else {}
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path.name} line {row_number} ({name}): params is not valid JSON: {e}"
                ) from e

            sites.append(Site(
                name=name,
                url=(row.get("url") or "").strip(),
                fetcher=(row.get("fetcher") or "feed").strip(),
                source_url=(row.get("source_url") or row.get("url") or "").strip(),
                tz=(row.get("tz") or "").strip() or None,
                params=params,
                enabled=(row.get("enabled") or "1").strip() not in ("0", "false", ""),
                note=(row.get("note") or "").strip(),
            ))
    return sites


def save_sites(sites: list[Site], path: Path = SITES_PATH) -> None:
    """Rewrite the registry, preserving column order.

    Only ever called by a person's tooling, never by the daily run: what the
    run learns goes to `state.py` instead, which is why this file can be
    committed without a bot rewriting it every morning.
    """
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SITE_COLUMNS)
        writer.writeheader()
        for site in sites:
            writer.writerow({
                "name": site.name,
                "url": site.url,
                "fetcher": site.fetcher,
                "source_url": site.source_url,
                "tz": site.tz or "",
                "params": json.dumps(site.params, ensure_ascii=False) if site.params else "",
                "enabled": "1" if site.enabled else "0",
                "note": site.note,
            })


def load_config() -> AppConfig:
    """Load and validate settings, interests, and the site registry."""
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings(**_read_yaml(SETTINGS_PATH))
    _apply_mail_identity(settings)
    return AppConfig(
        settings=settings,
        interests=InterestsConfig(**_read_yaml(INTERESTS_PATH)),
        sites=load_sites(),
    )


def _apply_mail_identity(settings: Settings) -> None:
    """Take the sender and recipients from the environment.

    Who the digest comes from and goes to is the only personal data in the
    configuration, so it lives with the secrets rather than in a tracked file:
    `.env` locally, repository secrets in CI. Everything else in
    `settings.yaml` is an operating parameter, and those are worth keeping in
    version control where a diff shows what changed and when.

    A value in the YAML still wins if the environment does not set one, so a
    private deployment can keep both in one file if it prefers.
    """
    sender = os.environ.get("MAIL_SENDER", "").strip()
    if sender:
        settings.email.sender_address = sender

    recipients = os.environ.get("MAIL_RECIPIENTS", "")
    parsed = [address.strip() for address in recipients.split(",") if address.strip()]
    if parsed:
        settings.email.recipients = parsed


def get_gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and fill it in, "
            "or set it in the environment."
        )
    return key


def get_smtp_password() -> str:
    """The SMTP password, with whitespace removed.

    Google displays an App Password as four space-separated groups, and pasting
    it verbatim produces a 19-character string that SMTP rejects with a generic
    authentication error. Stripping here means the copied form works.
    """
    password = "".join(os.environ.get("SMTP_PASSWORD", "").split())
    if not password:
        raise RuntimeError(
            "SMTP_PASSWORD is not set. Copy .env.example to .env and fill it in, "
            "or set it in the environment."
        )
    return password
