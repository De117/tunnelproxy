import re, json
from dataclasses import dataclass
from typing import Union, Set, Tuple

################################################################
#                  Configuration parsing
################################################################

class Port(int):
    """
    An integer in the range [0, 2**16).
    """
    def __init__(self, n: Union[int, str]):
        try:
            if isinstance(n, str):
                n = int(n)
        except ValueError:
            raise ValueError(f"Invalid port number: {n}")

        if n not in range(65536):
            raise ValueError(f"Invalid port number: {n}")

class Domain(str):
    """
    A non-empty domain name, according to RFC 1035.
    """
    def __init__(self, s: str):
        # Grammar taken from RFC 1035, §2.3.1
        # (The "subdomain" node, because we do not allow the empty string.)
        letter_digit_hyphen = "[a-zA-Z0-9-]"
        letter_digit = "[a-zA-Z0-9]"
        letter = "[a-zA-Z]"
        label = f"{letter}({letter_digit_hyphen}*{letter_digit}+)*"
        domain = f"({label}\\.)*{label}"

        if not re.match(f"^{domain}$", s):
            raise ValueError("Malformed domain: " + s)

@dataclass
class Configuration:
    version: int
    whitelist: Set[Tuple[Domain, Port]]


def parse_host_and_port(s: str) -> Tuple[Domain, Port]:
    """
    Parses a "host[:port]" string into a (host, port) pair.
    Default port is 80.

    Raises ValueError if parsing fails.
    """
    if ":" in s:
        h, p = s.split(":", maxsplit=1)
        return Domain(h), Port(p)
    else:
        return Domain(s), Port(80)


def parse_configuration_v1(b: bytes) -> Configuration:
    """
    Parses configuration. Raises ValueError if it fails.
    """
    try:
        config = json.loads(b)
        assert "version" in config, "Missing field: version"
        assert config["version"] == 1, "Unsupported version"
        assert "allowed_hosts" in config, "Missing field: allowed_hosts"
        hostnames = config["allowed_hosts"]
        assert isinstance(hostnames, list), "Malformed field: hostname"
        whitelist = {parse_host_and_port(h) for h in hostnames}
        return Configuration(version=1, whitelist=whitelist)
    except (json.JSONDecodeError, AssertionError, ValueError) as e:
        raise ValueError("Could not parse v1 configuration") from e


def load_configuration_from_file(filename: str) -> Configuration:
    """
    Loads configuration from file. Raises RuntimeError if it fails.
    """
    try:
        with open(filename, "rb") as f:
            return parse_configuration_v1(f.read())
    except (FileNotFoundError, ValueError) as e:
        raise RuntimeError(f"Could not open or parse configuration file: {e}") from e
