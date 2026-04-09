"""
Color classification profiles for well detection.

All color-specific data lives here so the analyzer can stay focused on image
processing instead of hardcoded HSV constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


HSVValue = Tuple[int, int, int]
HSVRange = Dict[str, HSVValue]


@dataclass(frozen=True)
class ColorProfile:
    """
    One named color classification profile.
    """

    name: str
    ranges: List[HSVRange]
    render_bgr: Tuple[int, int, int]
    gene_value: int = 0


COLOR_PROFILES: List[ColorProfile] = [
    ColorProfile(
        name="white",
        ranges=[
            {"lower": (0, 0, 200), "upper": (179, 45, 255)},
        ],
        render_bgr=(245, 245, 245),
    ),
    ColorProfile(
        name="brown",
        ranges=[
            {"lower": (8, 90, 45), "upper": (22, 255, 170)},
        ],
        render_bgr=(42, 95, 165),
    ),
    ColorProfile(
        name="red",
        ranges=[
            {"lower": (0, 120, 80), "upper": (7, 255, 255)},
            {"lower": (174, 120, 80), "upper": (179, 255, 255)},
        ],
        render_bgr=(0, 0, 255),
    ),
    ColorProfile(
        name="orange",
        ranges=[
            {"lower": (8, 130, 171), "upper": (22, 255, 255)},
        ],
        render_bgr=(0, 140, 255),
    ),
    ColorProfile(
        name="yellow",
        ranges=[
            {"lower": (23, 90, 140), "upper": (38, 255, 255)},
        ],
        render_bgr=(0, 255, 255),
        gene_value=1,
    ),
    ColorProfile(
        name="lime",
        ranges=[
            {"lower": (39, 90, 100), "upper": (55, 255, 255)},
        ],
        render_bgr=(0, 255, 170),
    ),
    ColorProfile(
        name="green",
        ranges=[
            {"lower": (56, 80, 70), "upper": (84, 255, 255)},
        ],
        render_bgr=(0, 180, 0),
    ),
    ColorProfile(
        name="cyan",
        ranges=[
            {"lower": (85, 70, 80), "upper": (100, 255, 255)},
        ],
        render_bgr=(255, 255, 0),
    ),
    ColorProfile(
        name="blue",
        ranges=[
            {"lower": (101, 70, 70), "upper": (120, 255, 255)},
        ],
        render_bgr=(255, 80, 0),
    ),
    ColorProfile(
        name="indigo",
        ranges=[
            {"lower": (121, 70, 70), "upper": (135, 255, 255)},
        ],
        render_bgr=(130, 0, 75),
    ),
    ColorProfile(
        name="violet",
        ranges=[
            {"lower": (136, 70, 70), "upper": (159, 255, 255)},
        ],
        render_bgr=(180, 60, 180),
    ),
    ColorProfile(
        name="pink",
        ranges=[
            {"lower": (160, 100, 170), "upper": (173, 255, 255)},
        ],
        render_bgr=(78, 32, 238),
    ),
]

COLOR_PROFILE_BY_NAME: Dict[str, ColorProfile] = {
    profile.name: profile for profile in COLOR_PROFILES
}
DEFAULT_RENDER_COLOR: Tuple[int, int, int] = (220, 220, 220)
