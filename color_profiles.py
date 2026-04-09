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
        name="black",
        ranges=[
            {"lower": (0, 0, 0), "upper": (179, 255, 45)},
        ],
        render_bgr=(35, 35, 35),
    ),
    ColorProfile(
        name="white",
        ranges=[
            {"lower": (0, 0, 190), "upper": (179, 40, 255)},
        ],
        render_bgr=(245, 245, 245),
    ),
    ColorProfile(
        name="gray",
        ranges=[
            {"lower": (0, 0, 46), "upper": (179, 45, 189)},
        ],
        render_bgr=(160, 160, 160),
    ),
    ColorProfile(
        name="brown",
        ranges=[
            {"lower": (5, 80, 35), "upper": (20, 255, 170)},
        ],
        render_bgr=(42, 95, 165),
    ),
    ColorProfile(
        name="red",
        ranges=[
            {"lower": (0, 90, 60), "upper": (10, 255, 255)},
            {"lower": (170, 90, 60), "upper": (179, 255, 255)},
        ],
        render_bgr=(0, 0, 255),
    ),
    ColorProfile(
        name="orange",
        ranges=[
            {"lower": (11, 90, 70), "upper": (22, 255, 255)},
        ],
        render_bgr=(0, 140, 255),
    ),
    ColorProfile(
        name="yellow",
        ranges=[
            {"lower": (23, 70, 80), "upper": (38, 255, 255)},
        ],
        render_bgr=(0, 255, 255),
        gene_value=1,
    ),
    ColorProfile(
        name="lime",
        ranges=[
            {"lower": (39, 70, 70), "upper": (50, 255, 255)},
        ],
        render_bgr=(0, 255, 170),
    ),
    ColorProfile(
        name="green",
        ranges=[
            {"lower": (51, 60, 50), "upper": (85, 255, 255)},
        ],
        render_bgr=(0, 180, 0),
    ),
    ColorProfile(
        name="cyan",
        ranges=[
            {"lower": (86, 55, 60), "upper": (100, 255, 255)},
        ],
        render_bgr=(255, 255, 0),
    ),
    ColorProfile(
        name="purple",
        ranges=[
            {"lower": (131, 55, 55), "upper": (150, 255, 255)},
        ],
        render_bgr=(180, 60, 180),
    ),
    ColorProfile(
        name="magenta",
        ranges=[
            {"lower": (151, 70, 70), "upper": (169, 255, 255)},
        ],
        render_bgr=(255, 0, 255),
    ),
    ColorProfile(
        name="pink",
        ranges=[
            {"lower": (160, 20, 140), "upper": (179, 150, 255)},
        ],
        render_bgr=(203, 192, 255),
    ),
]

COLOR_PROFILE_BY_NAME: Dict[str, ColorProfile] = {
    profile.name: profile for profile in COLOR_PROFILES
}
DEFAULT_RENDER_COLOR: Tuple[int, int, int] = (220, 220, 220)
