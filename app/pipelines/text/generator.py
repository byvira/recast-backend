print(">>> generator.py reloaded")


"""
Text content generator — single platform generation with full brand enforcement.

Context layers injected in order:
  1. brand_context       — voice, identity, audience, openers, closers
  2. tone_override_text  — tone override (empty if brand mode)
  3. goal_context        — content goal instruction
  4. content_brief       — sharpest angle from pre-analysis
  5. SPECIFICITY         — anti-generic instruction with few-shot examples
  6. ENGAGEMENT_PATTERNS — proven hook patterns
  7. approved_copy       — must-use openers, closers, required phrases
  8. retry_block         — specific rewrite feedback on retry
  9. platform_rules      — format and length requirements
 10. hashtag + cta       — extras toggles
 11. banned_instruction  — structural word replacements
 12. source content      — the actual input
"""

import logging
import re
from typing import Dict, List, Tuple

from app.models.text import AgentTask, AgentResult, Platform
from app.shared.llm import GroqModel, call_llm, call_llm_structured

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SPECIFICITY INSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────
SPECIFICITY_INSTRUCTION = """
OUTPUT STANDARD — NON-NEGOTIABLE:

BANNED OPENINGS — never start with any of these:
✗ "In today's world" / "In today's fast-paced world"
✗ "Are you tired of" / "Have you ever wondered"
✗ "We all know" / "It's no secret" / "Everyone knows"
✗ "I am excited to share" / "Thrilled to announce"
✗ "As a [profession]" / "As someone who"

BANNED CLOSINGS — never end with any of these:
✗ "So, are you ready to" / "The choice is yours"
✗ "What are you waiting for" / "Don't hesitate to"
✗ "The future is now" / "Join us on this journey"
✗ "You've got this" / "I believe in you"
✗ "Let's connect" / "Feel free to reach out"

NO FABRICATED STATISTICS — THIS IS A HARD RULE:
✗ NEVER invent percentages, survey results, or third-party stats
✗ NEVER write "X% of marketers", "studies show", "research says" unless
  that exact stat appears in the source content provided
✗ NEVER approximate a stat ("roughly 70%", "nearly half") — if you don't
  have the real number, don't use a number
✓ Use the brand's own story and real outcomes instead:
    - "I managed content for 11 brands simultaneously"
    - "I built the system. Now I teach it."
    - "The creators still posting two years in — they all batch."
✓ Specificity from lived experience beats invented research every time
✓ One real story > five fabricated statistics

GOOD VS BAD EXAMPLES:

BAD:  "Are you tired of budgeting apps that don't work?"
GOOD: "The average person downloads 4 money apps before giving up entirely."

BAD:  "In today's world, content creation is more challenging than ever."
GOOD: "I wrote 47 LinkedIn posts last quarter. 3 got traction. Here's what they had in common."

BAD:  "So, are you ready to transform your finances?"
GOOD: "Try this on Sunday. Open Fold. Close it in 4 minutes. Tell me if anything surprises you."

BAD:  "We all know that consistency is key to success."
GOOD: "The creators still posting two years in all share one trait — they batch."

BAD:  "70% of marketers lack a content strategy." (fabricated)
GOOD: "I managed content for 11 brands at once. I either built a system or burned out."

BAD:  "Many people struggle with content consistency."
GOOD: "Three weeks of daily posting. Six weeks of silence. Then the guilt cycle restarts."

RULE: Every sentence must earn its place with a specific detail from THIS brand's story.
- Use numbers from the brand's own experience — not invented research
- Use named outcomes from real results — not "3x qualified leads" unless it's in the source
- Use real moments — not "recently", say "last Tuesday" or use the brand story
- Delete any sentence that could be published by any brand in any industry
"""


# ─────────────────────────────────────────────────────────────────────────────
# ENGAGEMENT PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

ENGAGEMENT_PATTERNS = """
HIGH-PERFORMING HOOK PATTERNS — use exactly one per piece:

1. Contrarian stat:    "Everyone believes X. The data says Y."
2. Specific outcome:   "[Number] [result] in [timeframe]. One thing changed."
3. Pattern interrupt:  "I [did X] for [timeframe]. Here is what nobody talks about."
4. Failed assumption:  "I thought [common belief]. Then [specific event] proved me wrong."
5. Uncomfortable truth: "[Uncomfortable observation]. Not because [common excuse]. Because [real reason]."

BODY RULES:
- Every paragraph = 1-2 sentences maximum
- Every claim needs a concrete example or number within the same sentence
- No adjective without evidence:
    ✗ "powerful tool" → ✓ "tool that cut review time from 6 hours to 14 minutes"
    ✗ "significant results" → ✓ "40% drop in churn within 30 days"
- No vague timelines:
    ✗ "recently" → ✓ "last Tuesday"
    ✗ "often" → ✓ "in 7 out of 10 cases"

CLOSING RULES:
- End with a specific action, specific question, or specific next step
- Never end with motivation ("you can do this!")
- End with utility:
    ✓ "Try this with your next post. Tell me what breaks."
    ✓ "Open Fold this Sunday. Notice the one number that surprises you."
    ✓ "What is the first thing you would cut if you saw your spending snapshot?"
"""


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM RULES
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_RULES = {
    Platform.LINKEDIN: """
PLATFORM: LinkedIn post
- Hook-led first line — use one of the 5 engagement patterns above
- Never start with "I am excited to share", "Thrilled to announce",
  "Are you tired of", "In today's world", "We all know"
- 180-300 words total — MINIMUM 180 WORDS — count your words before submitting
- If your draft is under 180 words: add a specific example, number, or story detail
- Paragraphs of 1-2 sentences maximum
- Line break after every paragraph — no walls of text
- Include at least 2 specific numbers or concrete outcomes in the body
- End with a specific question or specific action — not "what do you think?"
- Maximum 3 hashtags at the very end on a new line if hashtags enabled
- First person. Professional but not stiff
- No bullet points unless listing genuinely parallel items
""",

    Platform.TWITTER: """
PLATFORM: Twitter/X single tweet
- 200-280 characters total — MINIMUM 200 characters — count before submitting
- One complete punchy idea — not just a question standing alone
- If you use a question it must be preceded by a statement that earns it
- Hook first — specific, bold, or contrarian
- No filler words — every word earns its place
- No hashtags unless they add genuine context (max 1)
- Example of a strong tweet (208 chars):
    "The average person downloads 4 money apps. Abandons all 4.
     The problem was never the app. It was the friction.
     One Sunday review changes that."
""",

    Platform.TWITTER_THREAD: """
PLATFORM: Twitter/X Thread
- 7 to 10 tweets numbered as 1/ 2/ 3/ etc
- Tweet 1: Standalone hook — reads as complete thought without the rest
- Tweets 2 through N-1: Each tweet = one key point, standalone but connected
- Final tweet: Strong concluding statement or clear CTA
- Every tweet hard under 280 characters
- Every tweet at least 100 characters — no one-liners
- No hashtags mid-thread. Maximum 2 hashtags on final tweet only if enabled
""",

    Platform.INSTAGRAM: """
PLATFORM: Instagram caption
- 120-180 words total — MINIMUM 120 WORDS — count your words before submitting
- If your draft is under 120 words: add a concrete moment, specific number, or story beat
- First line is the hook — Instagram shows only first line in feed
  Must stop the scroll in under 125 characters
- Middle: 2-3 short paragraphs of storytelling or insight
- End with a direct question or clear CTA
- If hashtags enabled: 8-10 relevant hashtags after two blank lines at end
- No hashtags in the main caption body
- Conversational and warm — never corporate
""",

    Platform.FACEBOOK: """
PLATFORM: Facebook post
- Community-friendly, conversational tone
- 180-300 words total — MINIMUM 180 WORDS
- Storytelling angle preferred over promotional
- Include at least one concrete example or specific number
- End with a question to drive comments
- No hashtags needed
""",

    Platform.BLOG: """
PLATFORM: Blog post
- Output in markdown format
- Title as H1 at the top (# Title)
- 3-5 H2 subheadings (## Subheading)
- 800-1200 words total — MINIMUM 800 WORDS
- Strong intro paragraph establishing the problem or hook
- Each section with a clear topic sentence
- At least 3 specific examples or data points across the body
- Conclusion with key takeaway and CTA if enabled
- Every sentence earns its place — no filler paragraphs
""",

    Platform.NEWSLETTER: """
PLATFORM: Newsletter section
- Plain text output — no markdown headers or formatting
- 300-500 words total — MINIMUM 300 WORDS — count your words before submitting
- If your draft is under 300 words: add a concrete example, real number, or specific outcome
- One main idea per section
- Short paragraphs of 2-3 sentences
- Personal anecdote or concrete example if it fits the brand voice
- Clear takeaway or next step at the end of the section
- End with a specific action the reader can take this week
""",

    Platform.YOUTUBE: """
PLATFORM: YouTube video description
- First 125 characters must be the hook — this is the search preview text
- 150-300 words total — MINIMUM 150 WORDS
- Include a chapters placeholder: 00:00 Intro [ADD TIMESTAMPS]
- 5-8 relevant tags listed at the end after Tags:
- CTA for subscribe and links using placeholder [LINK]
- Use the primary keyword naturally in the first paragraph
""",
}


# ─────────────────────────────────────────────────────────────────────────────
# HASHTAG RULES
# ─────────────────────────────────────────────────────────────────────────────

HASHTAG_RULES = {
    Platform.LINKEDIN: "Add exactly 3 relevant hashtags at the very end on a new line. Specific not generic.",
    Platform.INSTAGRAM: "Add 8-10 relevant hashtags after two blank lines at the end. Mix broad and niche tags.",
    Platform.TWITTER: "Add 1 relevant hashtag only if it adds genuine context. Otherwise omit entirely.",
    Platform.TWITTER_THREAD: "Maximum 2 hashtags on the final tweet only.",
    Platform.FACEBOOK: "No hashtags.",
    Platform.BLOG: "No inline hashtags. SEO is handled separately.",
    Platform.NEWSLETTER: "No hashtags.",
    Platform.YOUTUBE: "Tags are in the SEO package. No hashtags in the description body.",
}


# ─────────────────────────────────────────────────────────────────────────────
# CTA RULES
# ─────────────────────────────────────────────────────────────────────────────

CTA_RULES = {
    Platform.LINKEDIN: "End with one clear CTA — ask a specific question, invite a comment, or direct to an action. Make it specific to the content above, not generic.",
    Platform.TWITTER: "End with a hook question if space permits (stay under 280 chars total).",
    Platform.TWITTER_THREAD: "Final tweet must include a clear CTA — follow, comment, share, or link.",
    Platform.INSTAGRAM: "End the caption with a direct CTA — leave a comment, tag someone, or save this post.",
    Platform.FACEBOOK: "End with a question to drive comments.",
    Platform.BLOG: "End the article with a CTA — subscribe, read next, or contact.",
    Platform.NEWSLETTER: "End with one specific action for the reader to take this week.",
    Platform.YOUTUBE: "Include subscribe CTA and next action in the description.",
}


# ─────────────────────────────────────────────────────────────────────────────
# APPROVED COPY INJECTION
# ─────────────────────────────────────────────────────────────────────────────

def build_approved_copy_instruction(task: AgentTask) -> str:
    instruction = ""

    openers = task.metadata.get("approved_openers", [])
    if openers:
        instruction += "\nAPPROVED OPENER — pick EXACTLY ONE. Use it as your very first line.\n"
        for i, opener in enumerate(openers, 1):
            instruction += f"  {i}. {opener}\n"
        instruction += "RULE: Only ONE opener in the entire content. The other two must NOT appear.\n"
        instruction += "Adapt the style to fit your angle but keep the core idea.\n"

    closers = task.metadata.get("approved_closers", [])
    if closers:
        instruction += "\nAPPROVED CLOSER — pick EXACTLY ONE. Use it as your very last line.\n"
        for i, closer in enumerate(closers, 1):
            instruction += f"  {i}. {closer}\n"
        instruction += "RULE: Only ONE closer in the entire content. The other two must NOT appear anywhere.\n"
        instruction += "Adapt the style to fit your angle but keep the core idea.\n"

    phrases = task.metadata.get("required_phrases", [])
    if phrases:
        valid_phrases = [p for p in phrases if p.get("text", "").strip()]
        if valid_phrases:
            instruction += "\nREQUIRED PHRASES — weave these naturally into the content:\n"
            for phrase_obj in valid_phrases:
                text = phrase_obj.get("text", "").strip()
                placement = phrase_obj.get("placement", "any")
                if text:
                    instruction += f"  - '{text}' (placement: {placement})\n"
            instruction += "\n"

    return instruction

# ─────────────────────────────────────────────────────────────────────────────
# BANNED WORDS — STRUCTURAL REPLACEMENT INSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

def build_banned_words_instruction(
    banned_words: List[str],
    preferred_synonyms: List[Dict],
) -> str:
    """
    Build structural replacement instruction — shows what to use INSTEAD of banned words.
    Much more effective than just "don't use X" — gives the LLM an alternative.
    Skips synonyms with empty original field.
    """
    if not banned_words:
        return ""

    # Build replacement map from preferred_synonyms
    # Only include synonyms with non-empty original field
    replacements = {}
    for syn in preferred_synonyms:
        original = (syn.get("original") or "").strip().lower()
        replacement = (syn.get("replacement") or "").strip()
        if original and replacement:
            replacements[original] = replacement

    instruction = "\nBANNED WORDS — STRUCTURAL REPLACEMENTS REQUIRED:\n"
    for word in banned_words:
        word_lower = word.strip().lower()
        if word_lower in replacements:
            instruction += f"  ✗ NEVER '{word}' → ✓ ALWAYS use '{replacements[word_lower]}'\n"
        else:
            instruction += f"  ✗ NEVER '{word}' → ✓ REPHRASE using brand vocabulary\n"

    instruction += "\nIf you are about to write a banned word — STOP. Use the replacement instead.\n"
    instruction += "Content containing banned words will be automatically rejected and rewritten.\n"

    return instruction


# ─────────────────────────────────────────────────────────────────────────────
# POST-GENERATION VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
def validate_content(
    content: str,
    platform: Platform,
    banned_words: List[str],
    required_phrases: List[Dict],
    approved_openers: List[str],
    approved_closers: List[str],
) -> Tuple[bool, List[str]]:
    """
    Validate generated content against all brand rules.
    Hard violations → quality_passed = False → triggers retry in graph.
    Advisory violations → prefixed with "Advisory:" → logged but do not block.

    Returns (is_valid, list_of_issues)
    """
    hard_issues = []
    advisory_issues = []
    content_lower = content.lower()
    word_count = len(content.split())
    char_count = len(content)

    # ── Hard gate 1 — banned words (word boundary match) ─────────────────
    for word in banned_words:
        pattern = r'\b' + re.escape(word.strip().lower()) + r'\b'
        if re.search(pattern, content_lower):
            hard_issues.append(f"Banned word found: '{word}'")

    # ── Hard gate 2 — minimum length ──────────────────────────────────────
    min_words = {
        Platform.LINKEDIN: 150,
        Platform.TWITTER: 0,       # char-based
        Platform.TWITTER_THREAD: 200,
        Platform.INSTAGRAM: 100,
        Platform.FACEBOOK: 150,
        Platform.BLOG: 600,
        Platform.NEWSLETTER: 250,
        Platform.YOUTUBE: 100,
    }
    min_chars = {
        Platform.TWITTER: 200,
    }

    if platform in min_words and min_words[platform] > 0:
        if word_count < min_words[platform]:
            hard_issues.append(
                f"Content too short: {word_count} words, minimum {min_words[platform]} for {platform.value}"
            )

    if platform in min_chars:
        if char_count < min_chars[platform]:
            hard_issues.append(
                f"Content too short for Twitter: {char_count} chars (minimum 200)"
            )

    # ── Hard gate 3 — Twitter char limit ──────────────────────────────────
    if platform == Platform.TWITTER and char_count > 280:
        hard_issues.append(f"Twitter character limit exceeded: {char_count}/280")

    # ── Hard gate 4 — generic openings ────────────────────────────────────
    generic_openings = [
        "in today's world",
        "in today's fast-paced world",
        "are you tired of",
        "have you ever wondered",
        "we all know",
        "it's no secret",
        "i am excited to share",
        "thrilled to announce",
        "as someone who",
    ]
    first_200 = content_lower[:200]
    for generic in generic_openings:
        if first_200.startswith(generic) or first_200.startswith(f"\n{generic}"):
            hard_issues.append(f"Generic opening detected: '{generic}'")
            break

    # ── Hard gate 5 — generic closings ────────────────────────────────────
    generic_closings = [
        "so, are you ready to",
        "the choice is yours",
        "what are you waiting for",
        "don't hesitate to",
        "join us on this journey",
        "let's connect",
        "feel free to reach out",
    ]
    last_200 = content_lower[-200:]
    for generic in generic_closings:
        if generic in last_200:
            hard_issues.append(f"Generic closing detected: '{generic}'")
            break

    # ── Hard gate 6 — weasel words ────────────────────────────────────────
    weasel_words = [
        "many", "several", "often", "recently", "soon",
        "significant", "substantial", "various", "numerous",
    ]
    found_weasels = []
    for weasel in weasel_words:
        pattern = r'\b' + re.escape(weasel) + r'\b'
        if re.search(pattern, content_lower):
            found_weasels.append(weasel)
    if found_weasels:
        hard_issues.append(
            f"Weasel words found — replace with specific details: {', '.join(found_weasels)}"
        )

    # ── Hard gate 7 — required phrases present ────────────────────────────
    for phrase_obj in required_phrases:
        phrase = (phrase_obj.get("text") or "").strip()
        if phrase and phrase.lower() not in content_lower:
            hard_issues.append(f"Required brand phrase missing: '{phrase}'")

    # ── Advisory — approved opener used ───────────────────────────────────
    if approved_openers:
        first_line = content.split('\n')[0].strip().lower()
        used_approved = any(
            opener.lower()[:40] in first_line
            for opener in approved_openers
        )
        if not used_approved:
            advisory_issues.append(
                f"Advisory: Approved opener not used. First line: '{content.split(chr(10))[0].strip()[:80]}'"
            )

    # ── Advisory — approved closer used ───────────────────────────────────
    if approved_closers:
        lines = [l.strip() for l in content.strip().split('\n') if l.strip()]
        last_lines = lines[-3:] if len(lines) >= 3 else lines
        last_block = ' '.join(last_lines).lower()
        last_block_clean = re.sub(r'#\w+', '', last_block).strip()

        used_approved = any(
            closer.lower()[:40] in last_block_clean
            for closer in approved_closers
        )
        if not used_approved:
            advisory_issues.append(
                f"Advisory: Approved closer not used. Last line: '{lines[-1][:80]}'"
            )

    all_issues = hard_issues + advisory_issues
    is_valid = len(hard_issues) == 0

    return is_valid, all_issues

# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATOR
# ─────────────────────────────────────────────────────────────────────────────

async def generate_for_platform(task: AgentTask) -> AgentResult:
    """
    Generate content for a single platform with full brand enforcement.

    Flow:
      1. Build prompt with all 12 context layers
      2. Call Gemini structured output — parse JSON
      3. Fallback to Groq plain text if structured fails
      4. Validate output against brand rules
      5. Return AgentResult with quality data attached
    """
    platform_rules = PLATFORM_RULES.get(task.platform, "")

    # ── Read pre-built context strings ────────────────────────────────────
    tone_override_text = task.metadata.get("tone_override_text", "")
    goal_context = task.metadata.get("goal_context", "")
    content_brief = task.metadata.get("content_brief", "")
    retry_feedback = task.metadata.get("retry_feedback", "")
    retry_count = task.metadata.get("retry_count", 0)

    # ── Brand enforcement data ────────────────────────────────────────────
    banned_words = task.metadata.get("banned_words", [])
    preferred_synonyms = task.metadata.get("preferred_synonyms", [])
    approved_openers = task.metadata.get("approved_openers", [])
    approved_closers = task.metadata.get("approved_closers", [])
    required_phrases = task.metadata.get("required_phrases", [])

    # ── Build instruction blocks ──────────────────────────────────────────
    approved_copy_instruction = build_approved_copy_instruction(task)
    banned_instruction = build_banned_words_instruction(banned_words, preferred_synonyms)

    hashtag_instruction = (
        HASHTAG_RULES.get(task.platform, "")
        if task.metadata.get("hashtags", True)
        else "Do NOT include any hashtags anywhere in the content."
    )

    cta_instruction = (
        CTA_RULES.get(task.platform, "")
        if task.metadata.get("auto_cta", False)
        else ""
    )

    retry_block = ""
    if retry_feedback and retry_count > 0:
        retry_block = (
            f"\n⚠️ PREVIOUS ATTEMPT FAILED — FIX THESE ISSUES:\n"
            f"{retry_feedback}\n"
            f"Every issue above must be resolved in this generation.\n"
        )

    # ── Build full prompt ─────────────────────────────────────────────────
    prompt = f"""
{task.brand_context}
{tone_override_text}
{goal_context}
{content_brief}
{SPECIFICITY_INSTRUCTION}
{ENGAGEMENT_PATTERNS}
{approved_copy_instruction}
{retry_block}
{platform_rules}

ADDITIONAL RULES:
{hashtag_instruction}
{cta_instruction}
{banned_instruction}

SOURCE CONTENT:
{task.content}

Generate content for {task.platform.value}.

CRITICAL CHECKLIST BEFORE OUTPUTTING:
□ Does it start with an approved opener or engagement pattern hook?
□ Does it end with an approved closer (not a generic motivational line)?
□ Does it contain zero banned words?
□ Does it include all required phrases in the right placements?
□ Does it meet the minimum length for this platform?
□ Does every sentence contain a specific detail — not a generic observation?

Do not explain. Output only the final content.

Return valid JSON in exactly this format:
{{
  "content": "the full generated content here",
  "word_count": 0,
  "char_count": 0,
  "platform": "{task.platform.value}"
}}
"""

    # ── Call LLM — structured output ──────────────────────────────────────
    result = await call_llm_structured(prompt)

    if not result or "content" not in result:
        logger.warning(
            "Structured output failed for %s — falling back to Groq plain text",
            task.platform,
        )
        # Fallback uses the same full prompt — just removes the JSON instruction
        fallback_prompt = prompt.replace(
            'Return valid JSON in exactly this format:\n{\n  "content": "the full generated content here",\n  "word_count": 0,\n  "char_count": 0,\n  "platform": "' + task.platform.value + '"\n}',
            "Output only the final content. No JSON. No explanation."
        )
        plain = await call_llm(fallback_prompt, model=GroqModel.BALANCED)
        content = plain.strip()
        result = {
            "content": content,
            "word_count": len(content.split()),
            "char_count": len(content),
            "platform": task.platform.value,
        }

    # ── Recompute counts — never trust LLM's own count ───────────────────
    content_str = result.get("content", "")
    result["word_count"] = len(content_str.split())
    result["char_count"] = len(content_str)

    # ── Post-generation validation ────────────────────────────────────────
    is_valid, issues = validate_content(
        content=content_str,
        platform=task.platform,
        banned_words=banned_words,
        required_phrases=required_phrases,
        approved_openers=approved_openers,
        approved_closers=approved_closers,
    )

    result["quality_passed"] = is_valid
    result["quality_issues"] = issues
    result["flagged_for_review"] = not is_valid

    if not is_valid:
        hard_issues = [i for i in issues if not i.startswith("Advisory:")]
        logger.warning(
            "Validation failed for %s — hard issues: %s",
            task.platform,
            hard_issues,
        )
    else:
        advisory_issues = [i for i in issues if i.startswith("Advisory:")]
        if advisory_issues:
            logger.info(
                "Validation passed with advisories for %s: %s",
                task.platform,
                advisory_issues,
            )

    return AgentResult(
        agent="text",
        platform=task.platform,
        output=result,
        success=True,
    )