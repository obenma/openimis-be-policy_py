"""
`policyRenewals` is a read and must check a read-named right.

It was gated by `gql_mutation_renew_policies_perms` - the right to *perform* a renewal,
not to look at the queue. `gql_query_policy_renewals_perms` names the read, aliased
onto the same id (101205) so that who can see the queue does not change today.

Pointing it at `gql_query_policies_perms` instead would have widened it to everyone who
can read a policy, which is why the alias is asserted rather than the wider right.
"""

import ast
import inspect
import re
import textwrap

from django.test import TestCase

from policy import schema as policy_schema
from policy.apps import PolicyConfig

PERM_REF = re.compile(r"PolicyConfig\.(\w*perms\w*)")


def _perms_in_resolver(name):
    # dedent: getsource on a method returns it at class indentation, which
    # ast.parse rejects on its own
    source = textwrap.dedent(inspect.getsource(getattr(policy_schema.Query, name)))
    return set(PERM_REF.findall(ast.unparse(ast.parse(source))))


class PolicyRenewalsRightNameTestCase(TestCase):
    def test_renewals_read_checks_the_query_named_right(self):
        self.assertEqual(
            _perms_in_resolver("resolve_policy_renewals"),
            {"gql_query_policy_renewals_perms"},
        )

    def test_renewals_read_no_longer_checks_the_mutation_right(self):
        self.assertNotIn(
            "gql_mutation_renew_policies_perms",
            _perms_in_resolver("resolve_policy_renewals"),
        )

    def test_the_new_right_is_configured(self):
        self.assertTrue(
            PolicyConfig.gql_query_policy_renewals_perms,
            "empty right list - `has_perms` would grant it to everyone",
        )

    def test_id_is_still_aliased_onto_the_renew_right(self):
        """Intentional: the rename must not change who can see the queue."""
        self.assertEqual(
            PolicyConfig.gql_query_policy_renewals_perms,
            PolicyConfig.gql_mutation_renew_policies_perms,
        )

    def test_it_is_not_the_general_policy_read_right(self):
        """That would have widened the queue to every policy reader."""
        self.assertNotEqual(
            PolicyConfig.gql_query_policy_renewals_perms,
            PolicyConfig.gql_query_policies_perms,
        )
