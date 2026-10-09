"""The live-eval checks for Salesforce use and customer-facing answers.

Every record here is made up; real questions and answers never enter the
repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "triple_stamp_eval_under_test", ROOT / "tools/triple_stamp_eval.py"
)
assert spec is not None and spec.loader is not None
harness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = harness
spec.loader.exec_module(harness)

RECORD = "https://example.lightning.force.com/lightning/r/UseCase__c/a0X000000000001AAA/view"
AUDIT = {
    "internal_sources_consulted": [
        {"system": "glean", "url_or_record_id": f"{RECORD} a0X000000000001AAA"}
    ]
}


def _question(**spec: object):
    return harness.Question({"question": "Example Co use cases?", **spec}, 0)


class AudienceCheckTests(unittest.TestCase):
    def test_account_question_needs_salesforce_evidence_and_a_link(self) -> None:
        question = _question(expect_salesforce=True)
        linked = f"Example Co has one active use case ([record]({RECORD}))."

        self.assertEqual(
            harness._audience_checks(question, linked, [AUDIT]),
            {"salesforce_evidence": True, "salesforce_cited": True},
        )
        self.assertEqual(
            harness._audience_checks(question, "Example Co has one use case.", [{}]),
            {"salesforce_evidence": False, "salesforce_cited": False},
        )

    def test_customer_answer_must_not_carry_internal_links(self) -> None:
        question = _question(audience="customer", expect_salesforce=True)
        public = "See the [limits](https://docs.databricks.com/aws/en/oltp/)."

        self.assertEqual(
            harness._audience_checks(question, public, [AUDIT]),
            {"salesforce_evidence": True, "no_internal_links": True},
        )
        for internal in (
            f"Per the [use case]({RECORD}).",
            "See [the thread](https://example.slack.com/archives/C0000000/p1).",
            "Tracked in [ES-1](https://example.atlassian.net/browse/ES-1).",
            "Background in [go/example](go/example).",
        ):
            with self.subTest(internal=internal):
                self.assertFalse(
                    harness._audience_checks(question, internal, [])["no_internal_links"]
                )

    def test_research_on_a_shared_subject_is_not_a_swap(self) -> None:
        pilot = "What's the latest on the Example Co pilot, and what's blocking it?"
        email = (
            "Write an email to Example Co explaining what is generally "
            "available today versus in preview for their pipeline."
        )
        # The pilot research uses the email's generic words, but it covers its
        # own question's words, so it is on topic.
        own = "The pilot is blocking on sizing. Generally available today; preview later."
        self.assertTrue(harness._on_topic(own, pilot, [email]))
        # Live, the pilot chat's own research said "blockers", never
        # "blocking", and used the email's GA and preview words throughout.
        inflected = (
            "Example Co's pilot has two open blockers. Generally available today: "
            "synced tables. In preview: the change feed for their pipeline."
        )
        self.assertTrue(harness._on_topic(inflected, pilot, [email]))
        # A real swap covers none of the pilot question's own words.
        swapped = "Email draft explaining what is generally available today in the pipeline."
        self.assertFalse(harness._on_topic(swapped, pilot, [email]))
        self.assertIsNone(harness._on_topic("10", "5+5=?", ["2+2=?"]))

    def test_usage_needs_an_observed_and_cited_call_with_an_add_on(self) -> None:
        audit = json.dumps(
            {
                "verdict": "PASS",
                "internal_sources_consulted": [
                    {"system": "usage", "url_or_record_id": "https://example.test/explore"}
                ],
            }
        )
        observed = audit + "\n[System-observed Opus internal MCP coverage: other=usage=1[mcp__usage__customer_usage]. ]"
        question = _question(expect_usage=True)
        spend = _question(expect_usage=True, expect_usage_figures=True)

        def checks(q: object, answer: str, audits: list[str]) -> dict[str, bool]:
            return harness._usage_checks(q, answer, audits, [], True)

        self.assertEqual(
            checks(spend, "Spend was $150,000 in September.", [observed]),
            {"usage_evidence": True, "usage_in_answer": True},
        )
        # A pilot-status answer may use usage as context without dollars.
        self.assertEqual(checks(question, "The pilot is blocked on sizing.", [observed]), {"usage_evidence": True})
        # Cited but never observed, or observed but never cited, does not count.
        self.assertFalse(checks(question, "", [audit])["usage_evidence"])
        uncited = json.dumps({"verdict": "PASS"}) + "\nother=usage=1[x]"
        self.assertFalse(checks(question, "", [uncited])["usage_evidence"])
        customer = _question(expect_usage=True, audience="customer")
        self.assertEqual(checks(customer, "No figures.", [observed]), {"usage_evidence": True})
        self.assertEqual(checks(_question(), "$5", [observed]), {})

    def test_without_an_add_on_the_requester_never_reads_about_the_usage_tool(self) -> None:
        question = _question(expect_usage=True)
        # Opus's packet may record the absence; it is evidence, not noise.
        absent = json.dumps({"verdict": "PASS", "attacks": ["mcp__usage__customer_usage is not available."]})
        self.assertEqual(
            harness._usage_checks(question, "Consumption figures were not in the records.", [absent], [], False),
            {"usage_quiet": True},
        )
        self.assertFalse(
            harness._usage_checks(question, "", [absent], ["The usage tool was unavailable."], False)["usage_quiet"]
        )
        self.assertFalse(
            harness._usage_checks(question, "No usage add-on is installed.", [absent], [], False)["usage_quiet"]
        )

    def test_send_ready_ignores_a_customers_own_validation(self) -> None:
        # Live, a customer email advised keeping a system "in place during validation".
        self.assertIsNone(harness._PROCESS_NOTE.search("Keep the message bus in place during validation."))
        self.assertIsNotNone(harness._PROCESS_NOTE.search("SAFE was unavailable during validation."))
        self.assertIsNotNone(harness._PROCESS_NOTE.search("Checked during the review stage."))

    def test_progress_may_follow_the_wait_for_a_free_slot(self) -> None:
        step = "Step 1 of 3: researching public sources (cursor-cycle-1)"
        waited = "Waiting for a free slot: other questions are running.\n\n" + step
        self.assertTrue(harness._progress_readable([waited, "5 + 5 = 10."]))
        self.assertTrue(harness._progress_readable([step]))
        self.assertFalse(harness._progress_readable(["Waiting for a free slot: x.\n\n"]))
        self.assertFalse(harness._progress_readable(["Researching. " + step]))
        self.assertTrue(
            {"question_queued", "question_admitted"} <= harness._EXPECTED_ACTIONS
        )

    def test_policy_text_shown_to_the_reader_fails_the_round(self) -> None:
        # 2026-10-09: failed chats were saved as the response rule's own words.
        denied = (
            "[Denied by policy: Supervisor output is forbidden until a "
            "persisted Codex STAMP proves the byte-exact shippable_answer.]"
        )
        self.assertFalse(harness._no_policy_text(["Step 1 of 3: x", denied]))
        self.assertTrue(harness._no_policy_text(["Step 1 of 3: x", "5 + 5 = 10."]))

    def test_ordinary_questions_get_no_extra_checks(self) -> None:
        self.assertEqual(harness._audience_checks(_question(), "5 + 5 = 10.", []), {})
        # A phrase such as "go/no-go" in prose is not an internal link.
        self.assertTrue(
            harness._audience_checks(
                _question(audience="customer"), "The go/no-go call is next week.", []
            )["no_internal_links"]
        )


if __name__ == "__main__":
    unittest.main()
