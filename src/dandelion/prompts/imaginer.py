"""Imagination prompt — generates N seeds per theme in one call."""

IMAGINER_SYSTEM = """\
You are a creative futurist specializing in the theme: "{theme_name}".

Your task: imagine {n} DIFFERENT, vivid, specific future scenarios for this theme.

Rules:
- Each scenario must be genuinely distinct — different outcomes, different mechanisms, different timeframes.
- Be bold and specific — not vague trends, but concrete scenarios that could actually happen.
- Ground your imagination in the research data provided.
- Vary time_months across scenarios (near-term 3-6mo, mid-term 12-24mo, long-term 36-60mo).
- Write in the same language as the user's context.
"""


def build_imaginer_system(theme_name: str, n: int = 10) -> str:
    return IMAGINER_SYSTEM.format(theme_name=theme_name, n=n)


def build_imaginer_user(theme_description: str, context_packet: str, n: int = 10) -> str:
    truncated = context_packet[:4000] if len(context_packet) > 4000 else context_packet
    return (
        f"Theme: {theme_description}\n\n"
        f"Research Data:\n{truncated}\n\n"
        f"Generate exactly {n} different future scenarios for this theme. "
        f"Each must have a unique perspective — avoid any overlap between scenarios."
    )
