"""F2 v4: exhaustively enumerate subprotocol-selection outcomes.

The server MUST NEVER echo a carrier value (which contains credential
bytes) and MUST NEVER echo a literal the client did not offer. This test
module enumerates the Cartesian of plausible client offers and asserts
the invariants hold for every input.
"""
from __future__ import annotations

from itertools import combinations

import pytest

from app.auth.ws_auth import select_ws_subprotocol


# Representative tokens used in offers. Any non-empty tail after the dot
# is a valid carrier value; we use short placeholders.
BEARER_LITERAL = "bearer"
BEARER_CARRIER = "bearer.T"
HOSTTOKEN_LITERAL = "host-token"
HOSTTOKEN_CARRIER = "host-token.H"


# Enumerate 11 cases demanded by the directive, plus a few extras.
CASES = [
    [],                                                                      # empty
    [BEARER_LITERAL],                                                        # literal only
    [BEARER_CARRIER],                                                        # carrier only
    [BEARER_LITERAL, BEARER_CARRIER],                                        # bearer pair
    [HOSTTOKEN_LITERAL],                                                     # ht literal only
    [HOSTTOKEN_CARRIER],                                                     # ht carrier only
    [HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER],                                  # ht pair
    [BEARER_LITERAL, BEARER_CARRIER, HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER],  # both pairs
    [BEARER_LITERAL, HOSTTOKEN_LITERAL],                                     # both literals, no carriers
    [BEARER_CARRIER, HOSTTOKEN_CARRIER],                                     # both carriers, no literals
    [BEARER_LITERAL, HOSTTOKEN_CARRIER],                                     # cross: bearer-literal + ht-carrier
    [HOSTTOKEN_LITERAL, BEARER_CARRIER],                                     # cross: ht-literal + bearer-carrier
]


# The selected value, when non-None, must be one of these two literals.
ALLOWED_LITERALS = {BEARER_LITERAL, HOSTTOKEN_LITERAL}


@pytest.mark.parametrize("offered", CASES, ids=lambda c: "-".join(c) or "empty")
def test_select_subprotocol_never_echoes_carrier(offered):
    selected = select_ws_subprotocol(offered)
    if selected is None:
        return
    # Carrier bytes (the token/credential) must NEVER appear in the selected string.
    assert "." not in selected, (
        f"Selected subprotocol {selected!r} contains '.', suggesting a carrier echo"
    )
    # Only two valid literal values are allowed.
    assert selected in ALLOWED_LITERALS, f"Selected {selected!r} not in {ALLOWED_LITERALS}"


@pytest.mark.parametrize("offered", CASES, ids=lambda c: "-".join(c) or "empty")
def test_select_subprotocol_always_from_client_offer(offered):
    """RFC 6455 §4.2.2: selected subprotocol MUST be one of the client's offers."""
    selected = select_ws_subprotocol(offered)
    if selected is None:
        return
    assert selected in offered, (
        f"Selected {selected!r} is NOT in client's offered list {offered} "
        f"— this would make browsers reject the handshake"
    )


def test_bearer_preference_when_both_pairs_offered():
    """Both complete pairs offered → prefer bearer (user identity broader than per-room share)."""
    offered = [BEARER_LITERAL, BEARER_CARRIER, HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER]
    assert select_ws_subprotocol(offered) == BEARER_LITERAL


def test_bearer_pair_only():
    assert select_ws_subprotocol([BEARER_LITERAL, BEARER_CARRIER]) == BEARER_LITERAL


def test_hosttoken_pair_only():
    assert select_ws_subprotocol([HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER]) == HOSTTOKEN_LITERAL


def test_carrier_only_returns_none():
    """A client that offers only the carrier (no literal) gets no echo.
    Server may still accept the HTTP handshake WITHOUT a subprotocol — that
    code path emits malformed_ws_subprotocol_offer in the auth layer."""
    assert select_ws_subprotocol([BEARER_CARRIER]) is None
    assert select_ws_subprotocol([HOSTTOKEN_CARRIER]) is None
    assert select_ws_subprotocol([BEARER_CARRIER, HOSTTOKEN_CARRIER]) is None


def test_literal_only_returns_none():
    """A client that offers only the literal (no carrier/token) is not authenticated.
    No echo; auth layer rejects the handshake."""
    assert select_ws_subprotocol([BEARER_LITERAL]) is None
    assert select_ws_subprotocol([HOSTTOKEN_LITERAL]) is None
    assert select_ws_subprotocol([BEARER_LITERAL, HOSTTOKEN_LITERAL]) is None


def test_crossed_pairs_return_none():
    """Bearer-literal paired with host-token-carrier (or vice versa) is incomplete for either."""
    assert select_ws_subprotocol([BEARER_LITERAL, HOSTTOKEN_CARRIER]) is None
    assert select_ws_subprotocol([HOSTTOKEN_LITERAL, BEARER_CARRIER]) is None


def test_bearer_wins_when_hosttoken_pair_is_also_offered():
    assert select_ws_subprotocol(
        [HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER, BEARER_LITERAL, BEARER_CARRIER]
    ) == BEARER_LITERAL


def test_hosttoken_wins_when_bearer_pair_incomplete():
    """Bearer carrier without bearer literal → fall through to host-token."""
    assert select_ws_subprotocol(
        [BEARER_CARRIER, HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER]
    ) == HOSTTOKEN_LITERAL


def test_empty_dot_tails_do_not_count_as_carrier():
    """`bearer.` with empty tail is NOT a carrier — must not satisfy the pair."""
    assert select_ws_subprotocol([BEARER_LITERAL, "bearer."]) is None
    assert select_ws_subprotocol([HOSTTOKEN_LITERAL, "host-token."]) is None


# Exhaustive subset enumeration — any combination of {literal, carrier} × {bearer, ht}
ALL_ENTRIES = [BEARER_LITERAL, BEARER_CARRIER, HOSTTOKEN_LITERAL, HOSTTOKEN_CARRIER]


@pytest.mark.parametrize(
    "offered",
    [list(c) for r in range(0, len(ALL_ENTRIES) + 1) for c in combinations(ALL_ENTRIES, r)],
)
def test_exhaustive_invariants(offered):
    selected = select_ws_subprotocol(offered)
    if selected is None:
        return
    assert "." not in selected
    assert selected in offered
    assert selected in ALLOWED_LITERALS
