"""IP addresses and networks for the proxy and allowed source settings.

Every place that matches a client address against a configured list goes through this module,
so IPv4 and IPv6 behave alike. An IPv4-mapped IPv6 address (``::ffff:10.0.0.5``, what a dual
stack socket reports for an IPv4 client) is read as the IPv4 address it carries, once, here.
"""

import ipaddress
from collections.abc import Iterable, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network

MAPPED_PREFIX = 96
WILDCARD = "*"


def parse_address(text: str) -> IPAddress | None:
    """The address a text names, or None when it is not an IP address.

    Surrounding spaces are ignored and an IPv4-mapped IPv6 address becomes its IPv4 form. Ports
    are not part of an address: use ``parse_forwarded_entry`` for header values that may carry one.
    """
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def parse_forwarded_entry(text: str) -> tuple[IPAddress, int] | None:
    """The address and port (0 when absent) of one forwarded header entry, or None.

    Accepts a bare address, ``a.b.c.d:port`` and ``[v6]:port``; anything else is not an address.
    """
    entry = text.strip()
    port = 0
    if entry.startswith("["):
        host, bracket, remainder = entry[1:].partition("]")
        if not bracket or (remainder and not remainder.startswith(":")):
            return None
        entry = host
        if remainder:
            if not remainder[1:].isdigit():
                return None
            port = int(remainder[1:])
    elif entry.count(":") == 1:
        host, _, port_text = entry.partition(":")
        if not port_text.isdigit():
            return None
        entry, port = host, int(port_text)
    address = parse_address(entry)
    return None if address is None else (address, port)


def normalize_address(text: str | None) -> str | None:
    """The canonical text of an address (mapped IPv4 unwrapped), or None when it is not one."""
    if text is None:
        return None
    address = parse_address(text)
    return None if address is None else str(address)


def parse_network(entry: str, *, allow_everything: bool = True) -> IPNetwork:
    """One address or network (CIDR) as written in a setting, or a ValueError saying what is wrong.

    A bare address is a network of one. Host bits set in a network are refused, so a typo such as
    ``10.0.0.5/24`` is noticed instead of silently widening to ``10.0.0.0/24``. A network that
    holds every address is refused when ``allow_everything`` is False, like the ``*`` wildcard.
    """
    text = entry.strip()
    if text == WILDCARD:
        raise ValueError(f"{WILDCARD!r} is not accepted, list addresses or networks")
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError:
        try:
            suggestion = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError(f"{text!r} is not an IPv4 or IPv6 address or network") from None
        raise ValueError(f"{text!r} has host bits set, write the network as {suggestion}") from None
    if isinstance(network, IPv6Network):
        mapped = network.network_address.ipv4_mapped
        if mapped is not None and network.prefixlen >= MAPPED_PREFIX:
            network = IPv4Network((mapped, network.prefixlen - MAPPED_PREFIX))
    if not allow_everything and network.prefixlen == 0:
        raise ValueError(f"{text!r} covers every address, list the addresses or networks you trust")
    return network


def parse_networks(
    value: str | Iterable[str | IPNetwork], setting: str, *, allow_everything: bool = True
) -> list[IPNetwork]:
    """The networks of a comma separated setting (or a list of entries), refusing bad entries.

    Every bad entry is reported at once, each named, so startup says everything to fix.
    """
    entries = value.split(",") if isinstance(value, str) else list(value)
    networks: list[IPNetwork] = []
    problems: list[str] = []
    for entry in entries:
        text = str(entry)
        if not text.strip():
            continue
        try:
            networks.append(parse_network(text, allow_everything=allow_everything))
        except ValueError as error:
            problems.append(str(error))
    if problems:
        raise ValueError(f"{setting}: {'; '.join(problems)}")
    return networks


class NetworkSet:
    """A fixed set of networks that answers whether an address is inside one of them."""

    def __init__(self, networks: Sequence[IPNetwork] = ()) -> None:
        """Keep the networks, split by IP version so a lookup only compares like with like."""
        self.networks = tuple(networks)
        self._by_version: dict[int, tuple[IPNetwork, ...]] = {
            version: tuple(network for network in self.networks if network.version == version)
            for version in (4, 6)
        }

    def __bool__(self) -> bool:
        """Whether the set holds any network."""
        return bool(self.networks)

    def contains(self, address: IPAddress) -> bool:
        """Whether the address (mapped IPv4 already unwrapped) lies inside any network."""
        return any(address in network for network in self._by_version[address.version])
