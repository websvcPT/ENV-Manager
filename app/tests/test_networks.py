"""Addresses and networks of the proxy and allowed source settings."""

import ipaddress

import pytest

from app.core.networks import (
    NetworkSet,
    normalize_address,
    parse_address,
    parse_forwarded_entry,
    parse_network,
    parse_networks,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10.0.0.5", "10.0.0.5"),
        (" 10.0.0.5 ", "10.0.0.5"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:0DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("::ffff:10.0.0.5", "10.0.0.5"),
        ("::FFFF:A00:5", "10.0.0.5"),
        ("0:0:0:0:0:ffff:10.0.0.5", "10.0.0.5"),
        ("::1", "::1"),
    ],
)
def test_addresses_are_normalised_and_mapped_ipv4_is_unwrapped(text: str, expected: str) -> None:
    """IPv4 and IPv6 read alike and an IPv4-mapped IPv6 address becomes its IPv4 form."""
    assert normalize_address(text) == expected


@pytest.mark.parametrize("text", ["", "unknown", "10.0.0", "10.0.0.256", "10.0.0.5/24", "*", "::g"])
def test_text_that_is_not_an_address_is_none(text: str) -> None:
    """Anything that is not a single IP address is refused, never guessed at."""
    assert parse_address(text) is None
    assert normalize_address(text) is None
    assert normalize_address(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("203.0.113.9", ("203.0.113.9", 0)),
        ("203.0.113.9:4430", ("203.0.113.9", 4430)),
        ("2001:db8::9", ("2001:db8::9", 0)),
        ("[2001:db8::9]", ("2001:db8::9", 0)),
        ("[2001:db8::9]:4430", ("2001:db8::9", 4430)),
        ("::ffff:203.0.113.9", ("203.0.113.9", 0)),
        ("[::ffff:203.0.113.9]:80", ("203.0.113.9", 80)),
    ],
)
def test_forwarded_entries_may_carry_a_port(text: str, expected: tuple[str, int]) -> None:
    """A forwarded entry is a bare address, ``v4:port`` or ``[v6]:port``."""
    parsed = parse_forwarded_entry(text)
    assert parsed is not None
    assert (str(parsed[0]), parsed[1]) == expected


@pytest.mark.parametrize(
    "text", ["", "unknown", "_hidden", "203.0.113.9:port", "[2001:db8::9", "[2001:db8::9]x", "a:b"]
)
def test_forwarded_entries_that_are_not_addresses_are_none(text: str) -> None:
    """An obfuscated identifier, a name or a broken bracket is not an address."""
    assert parse_forwarded_entry(text) is None


def test_networks_accept_addresses_and_cidr_of_both_versions() -> None:
    """A bare address is a network of one, and both IP versions parse."""
    assert parse_network("10.0.0.5") == ipaddress.ip_network("10.0.0.5/32")
    assert parse_network("10.0.0.0/8") == ipaddress.ip_network("10.0.0.0/8")
    assert parse_network("2001:db8::/32") == ipaddress.ip_network("2001:db8::/32")
    assert parse_network("2001:db8::1") == ipaddress.ip_network("2001:db8::1/128")


def test_mapped_networks_are_read_as_ipv4() -> None:
    """An IPv4-mapped IPv6 entry names the IPv4 address or network it carries."""
    assert parse_network("::ffff:10.0.0.5") == ipaddress.ip_network("10.0.0.5/32")
    assert parse_network("::ffff:10.0.0.0/104") == ipaddress.ip_network("10.0.0.0/8")


def test_wildcard_and_junk_are_refused_with_a_message_that_names_the_entry() -> None:
    """The wildcard, hostnames and typos are errors, each saying what is wrong."""
    with pytest.raises(ValueError, match=r"'\*' is not accepted"):
        parse_network("*")
    with pytest.raises(ValueError, match=r"'proxy\.internal' is not an IPv4 or IPv6"):
        parse_network("proxy.internal")
    with pytest.raises(ValueError, match=r"'10\.0\.0\.5/24' has host bits set, .* 10\.0\.0\.0/24"):
        parse_network("10.0.0.5/24")
    with pytest.raises(ValueError, match=r"'10\.0\.0\.0/33'"):
        parse_network("10.0.0.0/33")


def test_a_network_of_every_address_is_refused_only_where_asked() -> None:
    """Trusting every peer is the wildcard in other words, so it can be refused."""
    for entry in ("0.0.0.0/0", "::/0"):
        assert parse_network(entry).prefixlen == 0
        with pytest.raises(ValueError, match="covers every address"):
            parse_network(entry, allow_everything=False)
    assert parse_network("0.0.0.0/1", allow_everything=False).prefixlen == 1


def test_lists_are_comma_separated_and_errors_name_the_setting() -> None:
    """A comma list skips empty items, and a bad one is reported with the setting name."""
    networks = parse_networks(" 10.0.0.1 , ,2001:db8::/32,", "ENVMANAGER_ALLOWED_SOURCES")
    assert [str(network) for network in networks] == ["10.0.0.1/32", "2001:db8::/32"]
    assert parse_networks("", "ENVMANAGER_ALLOWED_SOURCES") == []
    with pytest.raises(ValueError, match=r"^ENVMANAGER_ALLOWED_SOURCES: 'bogus' is not an IPv4"):
        parse_networks("10.0.0.1,bogus", "ENVMANAGER_ALLOWED_SOURCES")
    with pytest.raises(ValueError) as caught:
        parse_networks("10.0.0.5/8, nope, 10.0.0.1", "SETTING")
    assert str(caught.value) == (
        "SETTING: '10.0.0.5/8' has host bits set, write the network as 10.0.0.0/8; "
        "'nope' is not an IPv4 or IPv6 address or network"
    )


def test_network_sets_match_by_version_and_unwrap_nothing_themselves() -> None:
    """Membership is by network, never across IP versions."""
    networks = NetworkSet(parse_networks("10.0.0.0/8,2001:db8::/32,192.0.2.7", "setting"))
    assert networks
    assert not NetworkSet()
    inside = ("10.200.1.1", "2001:db8:ffff::1", "192.0.2.7")
    outside = ("11.0.0.1", "2001:db9::1", "192.0.2.8", "::1")
    for text in inside:
        address = parse_address(text)
        assert address is not None and networks.contains(address), text
    for text in outside:
        address = parse_address(text)
        assert address is not None and not networks.contains(address), text
