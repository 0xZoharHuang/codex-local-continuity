"""Read-only identification of the two observed desktop renderer layouts.

The shared-v2 result exposes identity atoms for display and diagnostics only.
Its local project registry is already shared. Composer recall belongs to the
top-level shared atom map, so callers must never project owner atoms as recall.
This module does not migrate, normalize, copy or write native state.
"""
import hashlib
import json
import re


LEGACY = "electron-account-renderer-state-v1"
SHARED_ATOMS = "electron-persisted-atom-state"
CONTENT_OWNER = "electron-renderer-content-owner"
OWNER_ATOMS = "electron-owner-atoms:"
OWNER_VALUE = "electron-owner-value:"
ACCOUNT_PREFERENCE = "electron-account-remote-preference:"
IDENTITY = "local-document-identity"
OWNER_ID = re.compile(r"[0-9a-f]{64}")
SIGNED_OUT = "signed-out"


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _valid_owner(owner):
    return isinstance(owner, str) and OWNER_ID.fullmatch(owner) is not None


def _verified(owner, atoms):
    """Return False for an uninitialized identity, matching legacy semantics."""
    _require(_valid_owner(owner) and isinstance(atoms, dict), "unsupported-profile-layout")
    identity = atoms.get(IDENTITY)
    if not isinstance(identity, dict) or not all(
        isinstance(identity.get(key), str) and identity[key] for key in ("userId", "accountId")
    ):
        return False
    try:
        encoded = json.dumps(
            [identity["userId"], identity["accountId"]], ensure_ascii=False, separators=(",", ":")
        ).encode()
    except (ValueError, UnicodeError):
        raise ValueError("identity-proof-mismatch") from None
    _require(hashlib.sha256(encoded).hexdigest() == owner, "identity-proof-mismatch")
    return True


def _legacy(state):
    wrapper = state[LEGACY]
    _require(
        isinstance(wrapper, dict) and "owner" in wrapper and isinstance(wrapper.get("inactive"), dict),
        "unsupported-identity-layout",
    )
    active = wrapper["owner"]
    _require(active is None or _valid_owner(active), "unsupported-profile-layout")
    owners = {}
    for owner, profile in wrapper["inactive"].items():
        _require(
            isinstance(profile, dict) and isinstance(profile.get("globals"), dict)
            and isinstance(profile.get("atoms"), dict), "unsupported-profile-layout",
        )
        if _verified(owner, profile["atoms"]):
            owners[owner] = profile["atoms"]
    if active is not None:
        atoms = state.get(SHARED_ATOMS)
        if _verified(active, atoms):
            _require(active not in owners, "duplicate-identity")
            owners[active] = atoms
    return {"kind": "legacy", "active": active if active in owners else None, "owners": owners}


def _shared(state):
    _require(CONTENT_OWNER in state and isinstance(state.get(SHARED_ATOMS), dict),
             "unsupported-identity-layout")
    active = state[CONTENT_OWNER]
    _require(active is None or active == SIGNED_OUT or _valid_owner(active), "unsupported-profile-layout")
    # A nonempty identity in the old common atom location makes the split
    # ambiguous. It cannot establish an account boundary for shared-v2.
    _require(state[SHARED_ATOMS].get(IDENTITY) is None, "mixed-native-layout")
    owners = {}
    for key, atoms in state.items():
        if not key.startswith(OWNER_ATOMS):
            continue
        owner = key[len(OWNER_ATOMS):]
        _require(isinstance(atoms, dict), "unsupported-profile-layout")
        _require("prompt-history" not in atoms, "unexpected-owner-prompt-history")
        if owner == SIGNED_OUT:
            _require(atoms.get(IDENTITY) is None, "signed-out-identity-conflict")
            continue
        _require(_verified(owner, atoms), "uninitialized-native-owner")
        owners[owner] = atoms
    if _valid_owner(active):
        _require(active in owners, "active-native-owner-missing")
    return {"kind": "shared-v2", "active": active if active in owners else None, "owners": owners}


def inspect(state):
    """Return the observed layout and identity atoms, without reading prompt bodies.

    Exceptions contain static codes. References in the result retain native data
    exactly. Callers may inspect them, but must treat shared-v2 as native-managed.
    """
    _require(isinstance(state, dict) and all(isinstance(key, str) for key in state),
             "unsupported-identity-layout")
    # Unrelated UI additions are harmless. New versions of ownership containers
    # are not evidence that the old account-boundary contract still applies.
    prefixes = ("electron-account-renderer-state-", "electron-renderer-content-owner-",
                "electron-owner-atoms-v", "electron-owner-value-v")
    _require(not any(key.startswith(prefixes) and key != LEGACY for key in state),
             "unsupported-ownership-version")
    shared_markers = CONTENT_OWNER in state or any(
        key.startswith((OWNER_ATOMS, OWNER_VALUE, ACCOUNT_PREFERENCE)) for key in state
    )
    if LEGACY in state:
        _require(not shared_markers, "mixed-native-layout")
        return _legacy(state)
    _require(shared_markers, "unsupported-identity-layout")
    return _shared(state)


def profiles(state):
    """List safe opaque labels for the initialized local identities."""
    layout = inspect(state)
    labels = [owner[:12] for owner in layout["owners"]]
    _require(len(labels) == len(set(labels)), "ambiguous-profile-label")
    return [{"label": owner[:12], "active": owner == layout["active"]}
            for owner in sorted(layout["owners"])]
