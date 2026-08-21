from __future__ import annotations

PIPELINE_MODULES = {
    "portrait": "illustration",
    "line_art": "lineart",
    "graphic_monochrome": "illustration",
    "landscape": "photo",
    "decorative": "illustration",
    "full_frame_illustration": "illustration",
    "photo": "photo",
}


def pipeline_for_profile(profile_name: str, fallback: str = "illustration") -> str:
    return PIPELINE_MODULES.get(profile_name, fallback)
