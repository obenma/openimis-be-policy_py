"""A failed policy mutation has to say why (OP-3195).

`errors += <dict>` iterates the dict and appends its *keys*, so every failure
contributed the two strings "title" and "list" and the message was discarded
before it left the backend. That is what produced the unexplained
["title", "list"] in the journal on release.
"""

from unittest.mock import patch

from core.test_helpers import create_test_interactive_user
from django.test import TestCase

from policy.gql_mutations import DeletePoliciesMutation, SuspendPoliciesMutation

MISSING = [
    "00000000-0000-0000-0000-0000000000a1",
    "00000000-0000-0000-0000-0000000000a2",
]


class PolicyMutationErrorShapeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="OP3195Admin")

    def test_unknown_policies_are_reported_one_entry_each(self):
        errors = DeletePoliciesMutation.async_mutate(self.user, uuids=MISSING)

        self.assertEqual([e["title"] for e in errors], MISSING)
        self.assertNotEqual(errors, ["title", "list"])

    def test_a_single_failure_collapses_to_its_message_not_to_the_keys(self):
        errors = DeletePoliciesMutation.async_mutate(self.user, uuids=MISSING[:1])

        # one error is collapsed to its `list` by the mutation
        self.assertTrue(all(isinstance(e, dict) for e in errors), errors)
        self.assertIn("message", errors[0])

    def test_suspend_reports_the_same_way(self):
        errors = SuspendPoliciesMutation.async_mutate(self.user, uuids=MISSING)

        self.assertEqual([e["title"] for e in errors], MISSING)

    def test_a_service_refusal_reaches_the_caller_intact(self):
        """set_deleted refuses a policy used in claims; the reason must survive."""
        refusal = {
            "title": "a-policy-uuid",
            "list": [{"message": "policy.mutation.policy_is_used_in_claims"}],
        }
        with patch(
            "policy.gql_mutations.Policy.objects.filter"
        ) as objects_filter, patch(
            "policy.gql_mutations.PolicyService.set_deleted", return_value=refusal
        ):
            objects_filter.return_value.first.return_value = object()
            errors = DeletePoliciesMutation.async_mutate(
                self.user, uuids=["a-policy-uuid", "another"]
            )

        self.assertEqual(errors, [refusal, refusal])
