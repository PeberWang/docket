"""The judge's API key in the macOS keychain, through /usr/bin/security.

Docket.app writes the item with `security -i` (the key goes in on stdin as hex, never in
argv), so the item's creator and its partition are Apple's `security` tool, and reading it
back here with `security find-generic-password` needs no prompt. An item a Swift process
adds through SecItemAdd always prompts when `security` reads it, whatever its ACL says,
which a launchd daemon cannot answer.

Service: config.json `judge.keychain_service` (default "docket.judge"); account: the
backend name ("systemone" or "openai"). Nothing here logs or raises with the key in it.
"""

from __future__ import annotations

import subprocess

SECURITY = "/usr/bin/security"
TIMEOUT = 5
NOT_FOUND = 44  # errSecItemNotFound, as security's exit status


class KeychainError(RuntimeError):
    pass


def read(service: str, account: str) -> str | None:
    """The stored key, or None when there is no item. Raises KeychainError when the read fails."""
    try:
        done = subprocess.run(
            [SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        # Only a keychain prompt waits this long: the item was not written by `security`.
        raise KeychainError(f"reading {service}/{account} from the keychain timed out (an access prompt?)") from error
    except OSError as error:
        raise KeychainError(f"cannot run {SECURITY}: {error.strerror}") from error
    if done.returncode == NOT_FOUND:
        return None
    if done.returncode != 0:
        raise KeychainError(f"security find-generic-password returned {done.returncode} for {service}/{account}")
    return done.stdout.rstrip("\n") or None


def write(service: str, account: str, key: str) -> None:
    """Add or replace the item; the same command Docket.app runs. Used by scripts and tests."""
    command = f'add-generic-password -U -s "{service}" -a "{account}" -l "Docket judge ({account})" -X {key.encode("utf-8").hex()}\n'
    try:
        done = subprocess.run([SECURITY, "-i"], input=command, capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as error:
        raise KeychainError(f"cannot write {service}/{account} to the keychain") from error
    if done.returncode != 0:
        raise KeychainError(f"security add-generic-password returned {done.returncode} for {service}/{account}")
