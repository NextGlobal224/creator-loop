"""Explicit B Web release coverage; full coverage remains the default."""

FULL_PROFILE = "B_WEB"
MANUAL_PROFILE = "B_WEB_MANUAL_BETA"


def required_pairs(profile: str = FULL_PROFILE) -> frozenset[tuple[str, str]]:
    if profile == FULL_PROFILE:
        return frozenset(
            (mode, form)
            for mode in ("AUTOMATIC", "MANUAL")
            for form in ("TEXT", "IMAGE_CAPTION", "VIDEO")
        )
    if profile == MANUAL_PROFILE:
        return frozenset({("MANUAL", "TEXT"), ("MANUAL", "IMAGE_CAPTION")})
    raise ValueError("Unknown B Web release profile")
