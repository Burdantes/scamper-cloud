import re


_IPV4_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+")


def source_ip_from_warts_name(file_name: str) -> str:
    matches = _IPV4_PATTERN.findall(file_name)
    if not matches:
        raise ValueError(f"warts filename does not contain a source IPv4 address: {file_name}")
    return matches[0].strip(".")
