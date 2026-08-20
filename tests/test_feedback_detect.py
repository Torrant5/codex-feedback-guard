import unittest

import conftest_paths  # noqa: F401

from exi import feedback_detect as fbd


class ExtractPromptTest(unittest.TestCase):
    def test_common_shapes(self):
        self.assertEqual(fbd.extract_prompt({"prompt": "  hello "}), "hello")
        self.assertEqual(fbd.extract_prompt({"user_prompt": "hi"}), "hi")
        self.assertEqual(fbd.extract_prompt({"message": "yo"}), "yo")
        self.assertEqual(fbd.extract_prompt({"input": "q"}), "q")
        self.assertEqual(fbd.extract_prompt({"text": "t"}), "t")

    def test_nested_object_and_list(self):
        self.assertEqual(fbd.extract_prompt({"prompt": {"text": "deep"}}), "deep")
        self.assertEqual(fbd.extract_prompt({"input": [{"text": "a"}, {"text": "b"}]}), "a b")

    def test_messages_last_user_wins(self):
        payload = {
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ]
        }
        self.assertEqual(fbd.extract_prompt(payload), "second")

    def test_empty_and_bad(self):
        self.assertEqual(fbd.extract_prompt({}), "")
        self.assertEqual(fbd.extract_prompt("nope"), "")
        self.assertEqual(fbd.extract_prompt({"prompt": ""}), "")

    def test_length_capped(self):
        big = "x" * (fbd.MAX_PROMPT_CHARS + 500)
        self.assertEqual(len(fbd.extract_prompt({"prompt": big})), fbd.MAX_PROMPT_CHARS)


class HashIdentityTest(unittest.TestCase):
    def test_normalize_stable_across_spacing_case(self):
        self.assertEqual(
            fbd.prompt_hash("Stop  Doing\tThat"),
            fbd.prompt_hash("stop doing that"),
        )

    def test_distinct_prompts_distinct_hash(self):
        self.assertNotEqual(fbd.prompt_hash("stop doing that"), fbd.prompt_hash("never do this"))

    def test_candidate_id_depends_on_session_turn_hash(self):
        h = fbd.prompt_hash("x")
        a = fbd.candidate_id("s1", "t1", h)
        b = fbd.candidate_id("s1", "t2", h)
        c = fbd.candidate_id("s2", "t1", h)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, fbd.candidate_id("s1", "t1", h))  # deterministic

    def test_human_evidence_id_namespaced_by_hash(self):
        h = fbd.prompt_hash("x")
        self.assertEqual(fbd.human_evidence_id(h), f"hp:{h}")


class DetectorPositiveTest(unittest.TestCase):
    POSITIVE = [
        "二度と勝手にコミットしないで",
        "前にも言ったよね、やめて",
        "そんな面倒なことできない",
        "また同じミスをしている",
        "I already told you not to do that",
        "Stop doing that, I said don't",
        "don't do that again please",
        "how many times do I have to say this",
    ]

    def test_positive_cases_flagged_with_cues(self):
        for p in self.POSITIVE:
            is_fb, cues = fbd.detect_feedback(p)
            self.assertTrue(is_fb, f"should flag: {p!r}")
            self.assertTrue(cues, f"cues should be non-empty for {p!r}")

    def test_two_distinct_weak_cues_flag(self):
        # "again" (repetition) + "why do you" (complaint) => two categories.
        is_fb, cues = fbd.detect_feedback("why do you keep breaking it again")
        self.assertTrue(is_fb)
        self.assertGreaterEqual(len(cues), 2)


class DetectorNegativeTest(unittest.TestCase):
    NEGATIVE = [
        "How do I configure the deploy pipeline?",
        "What is the difference between these two functions?",
        "また明日話しましょう",  # 'again tomorrow' — single weak cue only
        "Please add a test for the parser.",
        "この関数の使い方を教えてください",
        "run the build again",  # single weak cue ('again') — not enough
        "",
    ]

    def test_negative_cases_not_flagged(self):
        for p in self.NEGATIVE:
            is_fb, _ = fbd.detect_feedback(p)
            self.assertFalse(is_fb, f"should NOT flag: {p!r}")

    def test_cue_inside_code_block_ignored(self):
        # A strong cue only inside a fenced code block must not flag.
        prompt = "here is a string:\n```\nprint('never do that')\n```\nlooks fine?"
        is_fb, _ = fbd.detect_feedback(prompt)
        self.assertFalse(is_fb)

    def test_cue_inside_inline_code_ignored(self):
        is_fb, _ = fbd.detect_feedback("the variable is `stop doing that`")
        self.assertFalse(is_fb)

    def test_cues_are_category_labels_not_prompt_text(self):
        # Privacy: cues must be our fixed vocabulary, never lifted user text.
        _, cues = fbd.detect_feedback("二度とやめて")
        for c in cues:
            self.assertRegex(c, r"^[a-z-]+$")


if __name__ == "__main__":
    unittest.main()
