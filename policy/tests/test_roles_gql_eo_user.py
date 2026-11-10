"""Policy visibility for a GQL-created Enrolment Officer vs the CA+HF override."""

import json
import uuid

from django.core.cache import cache

from core.rights_role_test_case import RightsRoleGraphQLTestCase
from core.test_helpers import (
    create_claim_admin_role,
    create_data_entry_clerk_hf_role,
    create_enrolment_officer_role,
    create_test_interactive_user,
)
from insuree.test_helpers import create_test_insuree
from location.models import Location
from location.test_helpers import (
    create_basic_test_locations,
    create_test_health_facility,
    create_test_village,
)
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product

POLICIES_QUERY = """
query {
  policies(first: 50) {
    edges { node { uuid } }
  }
}
"""


class GqlEoUserPolicyRoleTests(RightsRoleGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_basic_test_locations()
        cls.admin = create_test_interactive_user(username="gqlpoadm")
        cls.eo_village = create_test_village(custom_props={"code": "PEOV1"})
        cls.other_village = create_test_village(custom_props={"code": "PEOV2"})
        cls.eo_district = cls.eo_village.parent.parent
        cls.other_district = cls.other_village.parent.parent
        cls.hf_district = Location.objects.get(code="R2D1", validity_to__isnull=True)
        cls.hf = create_test_health_facility(
            code="GQLPOHF", location_id=cls.hf_district.id
        )
        cls.product = create_test_product("GQLEOP")
        cls.eo_insuree = create_test_insuree(
            with_family=True,
            is_head=True,
            custom_props={"current_village": cls.eo_village},
            family_custom_props={"location": cls.eo_village},
        )
        cls.other_insuree = create_test_insuree(
            with_family=True,
            is_head=True,
            custom_props={"current_village": cls.other_village},
            family_custom_props={"location": cls.other_village},
        )
        cls.eo_policy = create_test_policy(
            cls.product, cls.eo_insuree, link=True
        )
        cls.other_policy = create_test_policy(
            cls.product, cls.other_insuree, link=True
        )

    def _eo_user(self, username):
        cache.clear()
        return self.create_gql_user_with_hf(
            self.admin,
            username=username,
            health_facility=None,
            roles=[create_enrolment_officer_role()],
            districts=[self.other_district],
            villages=[self.eo_village],
            include_officer=True,
            include_claim_admin=False,
        )

    def _ca_eo_user(self, username):
        cache.clear()
        return self.create_gql_user_with_hf(
            self.admin,
            username=username,
            health_facility=self.hf,
            roles=[create_claim_admin_role(), create_data_entry_clerk_hf_role()],
            districts=[self.eo_district],
            villages=[self.eo_village],
            include_officer=True,
            include_claim_admin=True,
        )

    def _policy_uuids(self, user):
        response = self.assert_gql_ok(user, POLICIES_QUERY)
        return [
            e["node"]["uuid"]
            for e in json.loads(response.content)["data"]["policies"]["edges"]
        ]

    def test_gql_eo_user_has_policy_rights(self):
        user = self._eo_user("peo1")
        self.assert_user_has_named_perms(user, ["gql_query_policies_perms"])
        self.assert_user_has_named_perms(user, ["gql_mutation_create_policies_perms"])

    def test_gql_eo_user_sees_only_own_village_policies(self):
        """A policy is as visible as its family; an EO's families are its villages' (N02)."""
        user = self._eo_user("peo2")
        uuids = self._policy_uuids(user)
        self.assertIn(str(self.eo_policy.uuid), uuids)
        self.assertNotIn(str(self.other_policy.uuid), uuids)

    def test_gql_eo_user_can_create_policy_for_own_village_family(self):
        user = self._eo_user("peo3")
        mid = str(uuid.uuid4())
        mutation = f"""
        mutation {{
          createPolicy(input: {{
            clientMutationId: "{mid}"
            clientMutationLabel: "eo create policy"
            enrollDate: "2024-04-07"
            startDate: "2024-06-01"
            expiryDate: "2025-05-31"
            value: "10000.00"
            productId: {self.product.id}
            familyId: {self.eo_insuree.family.id}
            officerId: {user.officer.id}
          }}) {{ clientMutationId internalId }}
        }}
        """
        self.assert_mutation_ok(user, mutation, mid)

    def test_gql_eo_plus_ca_hf_scopes_to_the_facility_district(self):
        """The claim administrator's facility overrides the officer villages (N02)."""
        user = self._ca_eo_user("peoa1")
        uuids = self._policy_uuids(user)
        self.assertNotIn(str(self.eo_policy.uuid), uuids)
        self.assertNotIn(str(self.other_policy.uuid), uuids)
