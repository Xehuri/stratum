"""Segmentation: selectors, profiles, block identity, re-segmentation and manual blocks."""

from .identity import BlockDraft, build_block_drafts
from .manual import ManualBlock, create_manual_block, resolve_manual_block
from .profiles import Profile, ProfileSet, load_profiles, load_profiles_text
from .resegment import BlockSnapshot, ResegmentationResult, resegment
from .selectors import Selection, SelectorError, select, select_blocks

__all__ = [
    "BlockDraft",
    "BlockSnapshot",
    "ManualBlock",
    "Profile",
    "ProfileSet",
    "ResegmentationResult",
    "Selection",
    "SelectorError",
    "build_block_drafts",
    "create_manual_block",
    "load_profiles",
    "load_profiles_text",
    "resegment",
    "resolve_manual_block",
    "select",
    "select_blocks",
]
