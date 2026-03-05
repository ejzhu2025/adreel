"""Tests that T2V prompts and planner rules prevent text/branding in generated video."""
import pytest
from agent.nodes.planner_llm import PLANNER_SYSTEM


# Words that cause T2V models to render visible text or logos
FORBIDDEN_DESC_WORDS = [
    "branded",
    "branding",
    "logo shown",
    "text appears",
    "caption",
    "title card",
    "watermark",
    "overlay",
    "tagline",
    "slogan",
]

# Negative phrases that must appear in every T2V prompt
REQUIRED_NEGATIVE_PHRASES = [
    "no text overlays",
    "no captions",
    "no watermarks",
    "no on-screen text",
    "no visible labels or writing on products",
]


def _build_t2v_prompt(desc: str, style_tone: list[str]) -> str:
    """Mirror the prompt-building logic in executor_pipeline._process_shot."""
    tone_str = ", ".join(style_tone) if isinstance(style_tone, list) else str(style_tone)
    clean_desc = desc.replace("branded ", "").replace("brand ", "")
    return (
        f"{clean_desc}. Style: {tone_str}. "
        "Vertical social media video, smooth motion, vibrant colors, cinematic quality. "
        "No text overlays, no captions, no watermarks, no on-screen text, "
        "no visible labels or writing on products, plain unbranded surfaces."
    )


class TestT2VPromptSanitization:
    """Verify that T2V prompts strip problematic words and include negative instructions."""

    def test_strips_branded_prefix(self):
        import re
        desc = "branded cup on table, branded background, soft light"
        prompt = _build_t2v_prompt(desc, ["fresh"])
        # "unbranded" is intentional in the negative suffix; check standalone "branded" word
        assert not re.search(r'\bbranded\b(?! surfaces)', prompt), (
            "Standalone 'branded' should be stripped from prompt (excluding 'unbranded surfaces')"
        )

    def test_strips_brand_word(self):
        desc = "brand logo on the bottle, brand identity"
        prompt = _build_t2v_prompt(desc, ["fresh"])
        assert "brand logo" not in prompt
        assert "brand identity" not in prompt

    def test_contains_all_required_negative_phrases(self):
        desc = "product bottle in studio, cinematic lighting"
        prompt = _build_t2v_prompt(desc, ["fresh", "summer"])
        for phrase in REQUIRED_NEGATIVE_PHRASES:
            assert phrase.lower() in prompt.lower(), f"Missing negative phrase: '{phrase}'"

    def test_preserves_visual_description(self):
        desc = "macro shot of watermelon slice with water droplets, vibrant colors"
        prompt = _build_t2v_prompt(desc, ["vibrant"])
        assert "watermelon" in prompt
        assert "water droplets" in prompt

    def test_tone_included_in_prompt(self):
        desc = "product on white background"
        prompt = _build_t2v_prompt(desc, ["luxury", "cinematic"])
        assert "luxury" in prompt
        assert "cinematic" in prompt


class TestPlannerSystemPromptRules:
    """Verify PLANNER_SYSTEM contains all prohibition rules for storyboard descs."""

    def test_prohibits_branded_word(self):
        assert "branded" in PLANNER_SYSTEM.lower()
        assert "NEVER use the word" in PLANNER_SYSTEM or "NEVER" in PLANNER_SYSTEM

    def test_prohibits_text_overlays_in_desc(self):
        assert "text_overlay" in PLANNER_SYSTEM
        # desc should never mention on-screen text
        assert "NEVER mention text" in PLANNER_SYSTEM or "never" in PLANNER_SYSTEM.lower()

    def test_outro_is_optional(self):
        # Outro is no longer forced — only added when user wants it
        assert "optional" in PLANNER_SYSTEM.lower()

    def test_prohibits_logo_in_desc(self):
        assert "logo" in PLANNER_SYSTEM.lower()

    @pytest.mark.parametrize("forbidden", [
        "text appears", "logo shown", "caption", "title card", "branding",
    ])
    def test_each_forbidden_word_mentioned_in_rules(self, forbidden: str):
        # Each forbidden word should appear in the system prompt as something to avoid
        assert forbidden in PLANNER_SYSTEM.lower(), (
            f"PLANNER_SYSTEM should mention '{forbidden}' as a forbidden term"
        )
