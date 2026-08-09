"""
Fonctions utilitaires pour HXYLIVE
"""


def format_bytes(bytes_value: int) -> str:
    """
    Format a byte size in a readable way (MB or GB if > 1000 MB)

    Args:
        bytes_value: Taille en bytes

    Returns:
        Formatted string (e.g. "1.5 GB", "256 MB")
    """
    mb = bytes_value / (1024 * 1024)

    if mb >= 1000:
        gb = mb / 1024
        return f"{gb:.2f} GB"
    else:
        return f"{mb:.1f} MB"
