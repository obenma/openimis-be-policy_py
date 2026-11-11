"""Delete and suspend only reach policies the caller may see (OP-3322)."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from core.test_helpers import create_right_only_user
from insuree.test_helpers import create_test_insuree
from location.models import Location
from location.test_helpers import create_basic_test_locations
from policy.gql_mutations import DeletePoliciesMutation, SuspendPoliciesMutation
from policy.models import Policy
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product


@override_settings(ROW_SECURITY=True)
class PolicyMutationScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_basic_test_locations()
        product = create_test_product("OP3322P")
        cls.policies = {}
        for key, village_code in (("a", "R1D1M1V1"), ("b", "R2D1M1V1")):
            village = Location.objects.get(code=village_code, validity_to__isnull=True)
            insuree = create_test_insuree(
                with_family=True,
                is_head=True,
                custom_props={"current_village": village},
                family_custom_props={"location": village},
            )
            cls.policies[key] = create_test_policy(product, insuree, link=True)
        cls.user = create_right_only_user(
            "op3322pol",
            ["gql_mutation_delete_policies_perms", "gql_mutation_suspend_policies_perms"],
            district_codes=["R1D1"],
        )
        cache.clear()

    def setUp(self):
        # Model instances are cached; a rolled-back change must not be read back.
        cache.clear()

    def _current(self, key):
        return Policy.objects.get(id=self.policies[key].id)

    def _delete(self, key):
        with patch(
            "policy.gql_mutations.PolicyService.set_deleted", return_value=[]
        ) as set_deleted:
            errors = DeletePoliciesMutation.async_mutate(
                self.user, uuids=[self.policies[key].uuid]
            )
        return errors, [call.args[0].id for call in set_deleted.call_args_list]

    def test_delete_ignores_a_policy_outside_the_users_districts(self):
        errors, deleted = self._delete("b")

        self.assertTrue(errors, "an out-of-scope policy must be reported as not found")
        self.assertEqual(deleted, [])

    def test_delete_still_reaches_a_policy_in_the_users_districts(self):
        errors, deleted = self._delete("a")

        self.assertEqual(errors, [])
        self.assertEqual(deleted, [self.policies["a"].id])

    def test_suspend_ignores_a_policy_outside_the_users_districts(self):
        status = self._current("b").status
        errors = SuspendPoliciesMutation.async_mutate(
            self.user, uuids=[self.policies["b"].uuid]
        )

        self.assertTrue(errors)
        self.assertEqual(self._current("b").status, status)
