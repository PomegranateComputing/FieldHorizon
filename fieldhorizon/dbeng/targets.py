"""
Target databases: where they are, which identities observe and operate
them, and what environment they belong to. Never how to authenticate.

Credentials are supplied from outside: a libpq password file (``passfile``
here, or the PGPASSFILE environment variable), or the standard PG*
environment variables. A target file that contains anything resembling a
password is refused outright, so a secret cannot end up in Git, in a
snapshot, or in an evidence bundle by way of this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

ENV_TARGETS_FILE = "FIELDHORIZON_DBENG_TARGETS"
DEFAULT_TARGETS_FILENAME = "dbeng_targets.yaml"

ENVIRONMENT_ISOLATED_TEST = "isolated_test"

_FORBIDDEN_KEYS = re.compile(r"pass(word)?|secret|token|credential", re.IGNORECASE)
_PASSWORD_IN_CONNINFO = re.compile(r"(password\s*=\s*)('[^']*'|\S+)", re.IGNORECASE)
_PASSWORD_IN_URI = re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)([^@\s]+)(@)", re.IGNORECASE)


class TargetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TargetConfig:
    name: str
    engine: str
    host: str
    port: int
    dbname: str
    observer_user: str
    operator_user: str | None
    environment: str
    passfile: str | None = None
    instance_marker: str | None = None
    system_identifier: str | None = None
    data_directory: str | None = None
    sslmode: str = "prefer"
    # DB-02: workload measurement runs under its own identity (no owner rights), and the operator
    # reaches the table owner's privileges only through an explicit SET ROLE to owner_role.
    measure_user: str | None = None
    owner_role: str | None = None
    # What the lab holds: "synthetic" for the generated bench; anything else is treated as sensitive.
    data_classification: str = "unspecified"

    @property
    def writable_environment(self) -> bool:
        return self.environment == ENVIRONMENT_ISOLATED_TEST

    def describe(self) -> dict:
        """Everything about the target that is safe to put in evidence: no secret is ever held here."""
        return asdict(self)

    def fingerprint(self) -> str:
        """Hash of the whole declared configuration: an operation refuses to act if it changes mid-flight."""
        return hashlib.sha256(json.dumps(self.describe(), sort_keys=True).encode("utf-8")).hexdigest()


def redact(text: str) -> str:
    """Defensive scrubbing for driver error messages before they reach logs or evidence."""
    text = _PASSWORD_IN_CONNINFO.sub(r"\1***", text)
    return _PASSWORD_IN_URI.sub(r"\1***\3", text)


def default_targets_path(config_root: Path) -> Path:
    override = os.environ.get(ENV_TARGETS_FILE, "").strip()
    return Path(override) if override else config_root / DEFAULT_TARGETS_FILENAME


def _reject_secrets(raw: dict, where: str) -> None:
    for key in raw:
        if key != "passfile" and _FORBIDDEN_KEYS.search(str(key)):
            raise TargetConfigError(
                f"{where}: key {key!r} looks like a credential. Credentials must not be stored in the "
                "target file; use a libpq password file (passfile / PGPASSFILE)."
            )


def _target(name: str, raw: object, where: str) -> TargetConfig:
    if not isinstance(raw, dict):
        raise TargetConfigError(f"{where}: target {name!r} must be a mapping")
    _reject_secrets(raw, f"{where}: target {name!r}")
    try:
        engine = str(raw["engine"])
        if engine != "postgresql":
            raise TargetConfigError(f"{where}: target {name!r}: unsupported engine {engine!r} (supported: postgresql)")
        sysid = raw.get("system_identifier")
        return TargetConfig(
            name=name,
            engine=engine,
            host=str(raw["host"]),
            port=int(raw["port"]),
            dbname=str(raw["dbname"]),
            observer_user=str(raw["observer_user"]),
            operator_user=str(raw["operator_user"]) if raw.get("operator_user") else None,
            environment=str(raw["environment"]),
            passfile=str(raw["passfile"]) if raw.get("passfile") else None,
            instance_marker=str(raw["instance_marker"]) if raw.get("instance_marker") else None,
            system_identifier=str(sysid) if sysid is not None else None,
            data_directory=str(raw["data_directory"]) if raw.get("data_directory") else None,
            sslmode=str(raw.get("sslmode", "prefer")),
            measure_user=str(raw["measure_user"]) if raw.get("measure_user") else None,
            owner_role=str(raw["owner_role"]) if raw.get("owner_role") else None,
            data_classification=str(raw.get("data_classification", "unspecified")),
        )
    except KeyError as exc:
        raise TargetConfigError(f"{where}: target {name!r} is missing required key {exc.args[0]!r}") from exc


def load_targets(path: Path) -> dict[str, TargetConfig]:
    if not path.exists():
        raise TargetConfigError(f"No target file at {path} (run `field-horizon db bench-setup` or write one).")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    targets = raw.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise TargetConfigError(f"{path}: expected a non-empty 'targets' mapping")
    return {name: _target(name, spec, str(path)) for name, spec in targets.items()}


def write_targets(path: Path, targets: dict[str, TargetConfig]) -> None:
    body = {"targets": {name: {k: v for k, v in t.describe().items() if k != "name" and v is not None} for name, t in targets.items()}}
    header = (
        "# Field Horizon DB engineering targets. No credentials here -- they come from the libpq\n"
        "# password file named by `passfile` (or PGPASSFILE). Written by `field-horizon db bench-setup`.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + yaml.safe_dump(body, sort_keys=True), encoding="utf-8", newline="\n")
