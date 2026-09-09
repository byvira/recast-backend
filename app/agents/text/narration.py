"""
app/agents/text/narration.py

Pure string builders — no logic, no imports.
Every message the agent emits to the activity stream lives here.
Centralised so tone and language stays consistent across all nodes.

Usage:
    from app.agents.text.narration import msg
    await state["emitter"].emit_log(msg.analyzing_source(word_count=847))
"""



class msg:
    """Static factory methods for every agent log message."""

    # ── Source Analysis ───────────────────────────────────────────────────────

    @staticmethod
    def analyzing_source(word_count: int) -> str:
        return f"Analyzing source content — {word_count:,} words detected"

    @staticmethod
    def source_type_detected(source_type: str) -> str:
        labels = {
            "text":      "Direct text input",
            "url":       "URL content extracted",
            "prompt":    "Prompt interpreted",
            "repurpose": "Repurpose mode — brand voice will be re-applied",
        }
        return f"Source type: {labels.get(source_type, source_type)}"

    @staticmethod
    def brand_voice_loaded(rules_count: int, brand_name: str) -> str:
        return f"Brand voice loaded — {brand_name} · {rules_count} rules active"

    @staticmethod
    def banned_words_loaded(count: int) -> str:
        if count == 0:
            return "No banned words configured"
        return f"Banned word filter active — {count} words blocked"

    # ── Angle Extraction ──────────────────────────────────────────────────────

    @staticmethod
    def extracting_angles() -> str:
        return "Extracting content angles..."

    @staticmethod
    def angles_found(count: int) -> str:
        return f"Found {count} angle{'s' if count != 1 else ''} — ranking by brand relevance"

    @staticmethod
    def angle_ranked(rank: int, name: str, score: int) -> str:
        return f"Angle #{rank}: {name} (score {score})"

    @staticmethod
    def angle_selected(name: str, score: int) -> str:
        return f"Selected angle: {name} (score {score}) — highest relevance"

    @staticmethod
    def angle_tie_detected(name_a: str, score_a: int, name_b: str, score_b: int) -> str:
        return (
            f"Tie detected — {name_a} ({score_a}) vs {name_b} ({score_b}) "
            f"within threshold · pausing for your input"
        )

    # ── Hook Generation ───────────────────────────────────────────────────────

    @staticmethod
    def generating_hooks(platform: str) -> str:
        return f"Generating hook variations for {platform}..."

    @staticmethod
    def hook_scored(version: int, score: int, threshold: int) -> str:
        status = "passed ✓" if score >= threshold else "below threshold"
        return f"Hook v{version} scored {score} — {status} (min {threshold})"

    @staticmethod
    def hook_selected(version: int, score: int) -> str:
        return f"Hook v{version} selected — score {score}"

    @staticmethod
    def all_hooks_failed(threshold: int) -> str:
        return f"All hooks below threshold ({threshold}) — using best available"

    # ── Platform Generation ───────────────────────────────────────────────────

    @staticmethod
    def generating_platform(platform: str, angle: str) -> str:
        return f"Generating {platform} post — angle: {angle}"

    @staticmethod
    def platform_formatting(platform: str) -> str:
        return f"Applying {platform} format rules and character limits"

    @staticmethod
    def extras_applying(extras: list[str]) -> str:
        if not extras:
            return "No extras requested"
        return f"Applying extras: {', '.join(extras)}"

    @staticmethod
    def platform_complete(platform: str, hook_score: int) -> str:
        return f"{platform} complete — hook score {hook_score} → ready for approval"

    # ── Scoring ───────────────────────────────────────────────────────────────

    @staticmethod
    def scoring_hook(platform: str) -> str:
        return f"Scoring hook for {platform}..."

    @staticmethod
    def scoring_readability(platform: str) -> str:
        return f"Scoring readability for {platform}..."

    @staticmethod
    def scores_complete(platform: str, hook: int, readability: str) -> str:
        return f"{platform} scored — hook {hook} · readability {readability}"

    # ── Session ───────────────────────────────────────────────────────────────

    @staticmethod
    def session_started(session_id: str, platform_count: int) -> str:
        return (
            f"Session started — {platform_count} platform"
            f"{'s' if platform_count != 1 else ''} queued"
        )

    @staticmethod
    def all_complete(count: int) -> str:
        return f"All {count} platform{'s' if count != 1 else ''} complete — session ready for approval"

    @staticmethod
    def saving_to_storage() -> str:
        return "Saving content to session storage..."

    @staticmethod
    def saved_to_storage(session_id: str) -> str:
        return f"Session saved — ID {session_id[:8]}..."

    # ── Errors ────────────────────────────────────────────────────────────────

    @staticmethod
    def platform_failed(platform: str, reason: str) -> str:
        return f"{platform} failed — {reason}"

    @staticmethod
    def retrying(platform: str, attempt: int) -> str:
        return f"Retrying {platform} — attempt {attempt}"

    # ── Agent commentary (card-level, not stream) ─────────────────────────────

    @staticmethod
    def build_card_commentary(
        angle_name:   str,
        angle_score:  int,
        hook_version: int,
        hook_score:   int,
        threshold:    int,
    ) -> str:
        """
        Returns the short commentary string shown on each output card.
        e.g. "Angle: Problem framing (91) · Hook v3 of 3 · Passed threshold"
        """
        passed = "Passed" if hook_score >= threshold else "Below"
        return (
            f"Angle: {angle_name} ({angle_score}) · "
            f"Hook v{hook_version} · "
            f"{passed} threshold ({threshold})"
        )

    @staticmethod
    def build_decisions_log(
        word_count:        int,
        brand_name:        str,
        rules_count:       int,
        angle_name:        str,
        angle_score:       int,
        hook_attempts:     list[tuple[int, int]],   # [(version, score), ...]
        threshold:         int,
        readability_level: str,
    ) -> list[str]:
        """
        Returns the ordered decisions list shown in the card detail view.
        Each string is one decision the agent made for this platform.
        """
        decisions = [
            f"Source: {word_count:,} words extracted",
            f"Brand voice: {brand_name} · {rules_count} rules loaded",
            f"Angle selected: {angle_name} (score {angle_score})",
        ]
        for version, score in hook_attempts:
            status = "passed ✓" if score >= threshold else "below threshold"
            decisions.append(f"Hook v{version} scored {score} — {status}")
        decisions.append(f"Readability: {readability_level}")
        return decisions
    

