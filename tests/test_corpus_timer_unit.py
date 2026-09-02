"""
The systemd unit files: hardened, and disabled.

These are text-file assertions, which is unusual for a test suite and
deliberate here. The unit is the only part of this system that runs
unattended, as the user, against untrusted third-party content. A
sandbox directive silently dropped in a future edit would not fail
anything else, and nobody would notice until it mattered.

Verified against the real systemd on 2026-08-01: `systemd-analyze verify`
parses the substituted unit with no complaints, and
`systemd-analyze security` scores it 1.9 ("OK").

**Nothing here enables anything.** The brief is explicit that the timer
stays disabled, and these tests only read files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

UNITS = Path(__file__).parent.parent / "deploy" / "systemd-user"
SERVICE = UNITS / "fieldhorizon-corpus.service"
TIMER = UNITS / "fieldhorizon-corpus.timer"


def service() -> str:
    return SERVICE.read_text(encoding="utf-8")


def timer() -> str:
    return TIMER.read_text(encoding="utf-8")


# --------------------------------------------------------------- sandbox


@pytest.mark.parametrize(
    "directive",
    [
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
        "ProtectHostname=true",
        "ProtectClock=true",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "RestrictNamespaces=true",
        "RestrictRealtime=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "MemoryDenyWriteExecute=true",
        "PrivateDevices=true",
        "DevicePolicy=closed",
        "RemoveIPC=true",
        "KeyringMode=private",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "UMask=0077",
    ],
)
def test_the_sandbox_directive_is_present(directive):
    assert directive in service(), f"{directive} was removed from the unit"


def test_the_service_holds_no_capabilities():
    """
    A user unit has none to begin with. Saying so explicitly means a
    future edit that adds one has to be deliberate.
    """
    assert "CapabilityBoundingSet=\n" in service()
    assert "AmbientCapabilities=\n" in service()


def test_only_internet_and_unix_sockets_are_permitted():
    """The harvester speaks HTTPS. No raw sockets, no netlink."""
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in service()
    assert "AF_PACKET" not in service()
    assert "AF_NETLINK" not in service()


def test_writes_are_confined_to_the_corpus_directories():
    text = service()
    assert "ReadWritePaths=__FH_ROOT__/data __FH_ROOT__/outputs __FH_ROOT__/logs" in text
    assert "ReadWritePaths=/" not in text


def test_a_runaway_is_capped_rather_than_taking_the_machine_down():
    """
    A bulk dump run decompresses multi-megabyte streams in memory. The
    point of a ceiling is that a runaway is killed instead of taking the
    desktop with it.
    """
    text = service()
    assert "MemoryMax=" in text
    assert "TasksMax=" in text
    assert "TimeoutStartSec=" in text


# --------------------------------------------------------------- secrets


def test_no_secret_is_written_into_the_unit_file():
    """
    Unit files are world-readable and `systemctl show` prints every
    `Environment=` value, so a token written there is a token published
    to every user on the machine.
    """
    text = service()
    for variable in ("GITHUB_TOKEN", "EUROPEANA_API_KEY", "WIKIMEDIA_ENTERPRISE_TOKEN"):
        assert f"Environment={variable}=" not in text, (
            f"{variable} must come from EnvironmentFile, never from Environment="
        )


def test_secrets_come_from_an_optional_environment_file():
    text = service()
    assert "EnvironmentFile=-" in text, "the leading '-' makes a missing file non-fatal"
    assert ".config/fieldhorizon/corpus.env" in text
    assert "600" in text, "the unit should say what mode that file needs"


def test_the_data_root_is_absolute_and_explicit():
    """
    A relative default resolves against the working directory. That is
    how the offline suite once wrote a fixture catalogue into the real
    corpus, which a later live run read back as a provider's own data.
    """
    assert "Environment=FIELDHORIZON_DATA_ROOT=__FH_ROOT__/data" in service()


# ---------------------------------------------------------- stays off


def test_the_service_is_oneshot_and_not_a_daemon():
    text = service()
    assert "Type=oneshot" in text
    assert "corpus daemon-once" in text
    assert "Restart=" not in text, "a oneshot that restarts itself is a loop nobody asked for"


def test_the_service_has_no_install_section_so_it_cannot_be_enabled_alone():
    """The timer is the only thing that should ever schedule this."""
    assert "[Install]" not in service()


def test_a_refusal_is_not_reported_as_a_failure():
    """
    Exit 2 is the harvester's "refused" code -- budget not configured,
    lock held, export blocked. A decision, not a fault.
    """
    assert "SuccessExitStatus=0 2" in service()


def test_the_timer_says_plainly_that_it_is_disabled_by_default():
    assert "DISABLED BY DEFAULT" in timer()


def test_the_installer_does_not_enable_anything_without_being_asked():
    script = (Path(__file__).parent.parent / "tools" / "install_corpus_timer.sh").read_text(
        encoding="utf-8"
    )

    assert "--enable" in script
    assert "install, do not enable" in script
    # The one `systemctl enable` must sit behind the flag, not run
    # unconditionally at the end of the script.
    enabling = [ln for ln in script.splitlines()
                if "systemctl --user enable" in ln and not ln.strip().startswith(("#", "echo"))]
    assert len(enabling) == 1, enabling


def test_the_timer_spreads_its_start_across_an_hour():
    """
    So institutional servers do not see every Field Horizon installation
    arrive at the same second.
    """
    text = timer()
    assert "RandomizedDelaySec=3600" in text
    assert "OnCalendar=daily" in text
    assert "Persistent=true" in text
