"""Tools"""


def escape_markdown_v2(text: str) -> str:
    """Markdown escape"""
    special_chars = r"*_[]()~`>#+=|{}.!\\"
    return "".join(f"\\{char}" if char in special_chars else char for char in text)


def wei_to_unit(wei: int) -> float:
    """Converts wei to currency unit."""
    return wei / 10**18


def wei_to_olas(wei: int) -> str:
    """Converts and formats wei to OLAS."""
    return f"{wei_to_unit(wei):.2f} OLAS"


def str_to_bool(value: str | bool) -> bool:
    """Converts string or bool to bool"""
    return str(value).lower() in ["true", "1", "yes"]
