"""Policies, and what is scoped through them, stay in the user's districts (audit N02)."""

import json

from django.core.cache import cache
from django.test import override_settings

from contribution.models import Premium
from contribution.test_helpers import create_test_premium
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase, BaseTestContext
from core.test_helpers import create_right_only_user
from insuree.test_helpers import create_test_insuree
from location.models import Location
from location.test_helpers import create_basic_test_locations
from policy.models import Policy
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product

POLICIES_QUERY = 'query { policies(showHistory: %s) { edges { node { uuid } } } }'
BY_FAMILY_QUERY = 'query { policiesByFamily(familyUuid: "%s") { edges { node { policyUuid } } } }'


@override_settings(ROW_SECURITY=True)
class PolicyRowSecurityTests(openIMISGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_basic_test_locations()
        product = create_test_product("N02PROD")
        cls.policies = {}
        cls.families = {}
        for key, village_code in (("a", "R1D1M1V1"), ("b", "R2D1M1V1")):
            village = Location.objects.get(code=village_code, validity_to__isnull=True)
            insuree = create_test_insuree(
                with_family=True,
                is_head=True,
                custom_props={"current_village": village},
                family_custom_props={"location": village},
            )
            policy = create_test_policy(product, insuree, link=True)
            create_test_premium(policy.id, with_payer=False)
            cls.policies[key] = policy
            cls.families[key] = insuree.family
        # One superseded version each, for showHistory.
        cls.history_uuids = {}
        for key, policy in cls.policies.items():
            history_id = policy.save_history()
            cls.history_uuids[key] = Policy.objects.get(id=history_id).uuid

        cls.user_a = create_right_only_user(
            "n02usera",
            ["gql_query_policies_perms", "gql_query_policies_by_family_perms"],
            district_codes=["R1D1"],
        )
        cache.clear()

    def _gql(self, query):
        token = BaseTestContext(user=self.user_a).get_jwt()
        response = self.query(query, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})
        content = json.loads(response.content)
        self.assertIsNone(content.get("errors"))
        return content["data"]

    def _ours(self, uuids):
        ours = {p.uuid.upper() for p in self.policies.values()}
        ours |= {u.upper() for u in self.history_uuids.values()}
        return {u.upper() for u in uuids} & ours

    def test_policies_keep_own_district(self):
        edges = self._gql(POLICIES_QUERY % "false")["policies"]["edges"]
        self.assertEqual(self._ours(e["node"]["uuid"] for e in edges), {self.policies["a"].uuid.upper()})

    def test_policy_history_keeps_own_district(self):
        edges = self._gql(POLICIES_QUERY % "true")["policies"]["edges"]
        self.assertEqual(
            self._ours(e["node"]["uuid"] for e in edges),
            {self.policies["a"].uuid.upper(), self.history_uuids["a"].upper()},
        )

    def test_policies_by_family_keep_own_district(self):
        for key, expected in (("a", 1), ("b", 0)):
            data = self._gql(BY_FAMILY_QUERY % self.families[key].uuid)
            self.assertEqual(len(data["policiesByFamily"]["edges"]), expected, key)

    def test_children_follow_the_policy(self):
        premiums = Premium.get_queryset(Premium.objects.all(), self.user_a).filter(
            policy__in=self.policies.values()
        )
        self.assertEqual({p.policy_id for p in premiums}, {self.policies["a"].id})
