"""Seed a handful of real presets into one workspace for manual testing.

The Presets page had no backend at all before this — nothing to seed.
This inserts real, persisted documents via the same `presets` collection
the API reads/writes, so the page actually shows real data (not
INITIAL_PRESETS) after running this against your dev workspace.

Usage:
    python -m scripts.seed_presets --workspace-id <id> --user-id <id>

Find your workspace_id/user_id via GET /api/v1/workspaces or the
workspace_members collection — this script does not create either.
"""

import argparse
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import presets

SEED_PRESETS = [
    {
        "title": "5-Part Executive Contrast Framework",
        "category": "text_thread",
        "category_label": "X & Threads",
        "description": "Contrast-driven thread structure for executive takes — before/after, old way/new way.",
        "target_channels": ["twitter", "threads"],
        "structure_rules": [
            {"step_index": 1, "section_name": "Hook Thesis", "char_limit": 240, "guidelines": "Deliver the clear main takeaway up front."},
            {"step_index": 2, "section_name": "Old Way", "char_limit": 280, "guidelines": "Describe the conventional approach and its cost."},
            {"step_index": 3, "section_name": "New Way", "char_limit": 280, "guidelines": "Contrast with the better approach."},
            {"step_index": 4, "section_name": "Proof", "char_limit": 280, "guidelines": "One concrete result or example."},
            {"step_index": 5, "section_name": "Takeaway CTA", "char_limit": 200, "guidelines": "Actionable closing thought."},
        ],
        "default_hashtags": ["#Growth", "#Systems"],
        "hook_formula_example": "Everyone does X. Here's why that's costing you Y.",
    },
    {
        "title": "Founder Story Carousel",
        "category": "carousel",
        "category_label": "Visual Carousel",
        "description": "Personal-narrative carousel structure — moment, struggle, turning point, lesson.",
        "target_channels": ["instagram", "linkedin"],
        "structure_rules": [
            {"step_index": 1, "section_name": "The Moment", "char_limit": 150, "guidelines": "Open on a specific scene, not a summary."},
            {"step_index": 2, "section_name": "The Struggle", "char_limit": 200, "guidelines": "What was actually hard, with real detail."},
            {"step_index": 3, "section_name": "The Turning Point", "char_limit": 200, "guidelines": "What changed and why."},
            {"step_index": 4, "section_name": "The Lesson", "char_limit": 180, "guidelines": "One transferable takeaway."},
        ],
        "default_hashtags": ["#FounderStory", "#BuildInPublic"],
        "hook_formula_example": "Three years ago I almost shut it all down.",
    },
    {
        "title": "60-Second Audio Brief",
        "category": "audio_brief",
        "category_label": "Audio Brief",
        "description": "Tight spoken-word structure for a short audio segment — hook, insight, close.",
        "target_channels": ["youtube"],
        "structure_rules": [
            {"step_index": 1, "section_name": "Cold Open", "char_limit": 120, "guidelines": "A single striking line, no preamble."},
            {"step_index": 2, "section_name": "Core Insight", "char_limit": 400, "guidelines": "The one idea worth the listener's minute."},
            {"step_index": 3, "section_name": "Close", "char_limit": 120, "guidelines": "A memorable line to end on, not a recap."},
        ],
        "default_hashtags": [],
        "hook_formula_example": "Nobody tells you this part.",
    },
    {
        "title": "Product Demo Hook Script",
        "category": "video_script",
        "category_label": "Video Hook",
        "description": "First-3-seconds-matter script shape for a short product demo video.",
        "target_channels": ["instagram", "youtube"],
        "structure_rules": [
            {"step_index": 1, "section_name": "Pattern Interrupt", "char_limit": 100, "guidelines": "Visually or verbally break expectation immediately."},
            {"step_index": 2, "section_name": "Problem", "char_limit": 200, "guidelines": "Name the exact frustration this solves."},
            {"step_index": 3, "section_name": "Demo Beat", "char_limit": 250, "guidelines": "Show, don't narrate, the fix."},
            {"step_index": 4, "section_name": "CTA", "char_limit": 100, "guidelines": "One clear next action."},
        ],
        "default_hashtags": ["#ProductDemo"],
        "hook_formula_example": "Wait — it does THAT?",
    },
    {
        "title": "Weekly Newsletter Digest",
        "category": "newsletter",
        "category_label": "Newsletter",
        "description": "Recurring digest shape — one big idea, three links, one ask.",
        "target_channels": ["substack"],
        "structure_rules": [
            {"step_index": 1, "section_name": "This Week's Idea", "char_limit": 600, "guidelines": "One developed thought, not a list of updates."},
            {"step_index": 2, "section_name": "Worth Your Time", "char_limit": 400, "guidelines": "2-3 links with one honest sentence each on why."},
            {"step_index": 3, "section_name": "The Ask", "char_limit": 150, "guidelines": "One specific, low-friction ask of the reader."},
        ],
        "default_hashtags": [],
        "hook_formula_example": "This week I kept coming back to one idea.",
    },
]


async def seed(workspace_id: str, user_id: str) -> None:
    now = datetime.now(timezone.utc)
    inserted = 0
    for preset in SEED_PRESETS:
        doc = {
            "id": str(uuid4()),
            "workspace_id": workspace_id,
            "brand_id": None,
            "version": 1,
            **preset,
            "voice_binding_id": None,
            "usage_count": 0,
            "tone_score": 90,
            "is_system_default": False,
            "created_by": user_id,
            "created_at": now,
            "updated_at": now,
            "deleted": False,
        }
        await presets.insert_one(doc)
        inserted += 1
        print(f"  + {doc['title']}")
    print(f"\nSeeded {inserted} presets into workspace {workspace_id}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True, help="Target workspace_id to seed presets into.")
    parser.add_argument("--user-id", required=True, help="User id recorded as created_by on each preset.")
    args = parser.parse_args()
    asyncio.run(seed(args.workspace_id, args.user_id))


if __name__ == "__main__":
    main()
