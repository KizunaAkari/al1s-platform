"""Pixel coordinates for diagnostic overlays; never used to control execution."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PixelRect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class ImageSize:
    width: int
    height: int


@dataclass(frozen=True)
class FailureRecognitionGeometry:
    template_rect: PixelRect | None
    search_region: PixelRect | None
    basis_size: ImageSize | None
    match_rect: PixelRect | None
    match_size: ImageSize | None
    match_score: float | None
    match_threshold: float | None
    match_passed: bool | None
