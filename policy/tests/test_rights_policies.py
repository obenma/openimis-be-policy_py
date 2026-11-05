import uuid

from core.rights_role_test_case import RightsRoleGraphQLTestCase
from core.test_helpers import create_right_only_user, create_test_officer
from insuree.test_helpers import create_test_insuree
from location.test_helpers import create_basic_test_locations, create_test_village
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product


POLICIES_QUERY = """
query {
  policies(first: 5) {
    edges { node { uuid status } }
  }
}
"""


class PolicyRightsTests(RightsRoleGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_basic_test_locations()
        cls.village = create_test_village()
        cls.insuree = create_test_insuree(
            with_family=True,
            is_head=True,
            custom_props={"current_village": cls.village},
            family_custom_props={"location": cls.village},
        )
        cls.product = create_test_product("PRIGHT")
        cls.policy = create_test_policy(cls.product, cls.insuree, link=True)
        cls.officer = create_test_officer(
            villages=[cls.village], custom_props={"code": "PRIGHTEO"}
        )
        cls.districts = cls.DISTRICT_CODES + [cls.village.parent.parent.code]

    def _user(self, name, perms):
        return create_right_only_user(name, perms, district_codes=self.districts)

    def test_query_policies_right(self):
        allowed = self._user("r_pol_q", ["gql_query_policies_perms"])
        denied = self._user("r_pol_q_no", [])
        self.assert_gql_ok(allowed, POLICIES_QUERY)
        self.assert_gql_unauthorized(denied, POLICIES_QUERY)

    def test_query_policies_by_insuree_and_family(self):
        allowed = self._user(
            "r_pol_by",
            [
                "gql_query_policies_perms",
                "gql_query_policies_by_insuree_perms",
                "gql_query_policies_by_family_perms",
            ],
        )
        denied = self._user("r_pol_by_no", [])
        by_insuree = f"""
        query {{
          policiesByInsuree(chfId: "{self.insuree.chf_id}") {{
            edges {{ node {{ policyUuid }} }}
          }}
        }}
        """
        by_family = f"""
        query {{
          policiesByFamily(familyUuid: "{self.insuree.family.uuid}") {{
            edges {{ node {{ policyUuid }} }}
          }}
        }}
        """
        self.assert_gql_ok(allowed, by_insuree)
        self.assert_gql_ok(allowed, by_family)
        self.assert_gql_unauthorized(denied, by_insuree)
        self.assert_gql_unauthorized(denied, by_family)

    def test_query_eligibilities_right(self):
        allowed = self._user("r_eli_q", ["gql_query_eligibilities_perms"])
        denied = self._user("r_eli_q_no", [])
        query = f"""
        query {{
          policyEligibilityByInsuree(chfId: "{self.insuree.chf_id}") {{
            prodId
          }}
        }}
        """
        self.assert_gql_ok(allowed, query)
        self.assert_gql_unauthorized(denied, query)

    def _create_policy_mutation(self, mutation_uuid):
        return f"""
        mutation {{
          createPolicy(input: {{
            clientMutationId: "{mutation_uuid}"
            clientMutationLabel: "rights create policy"
            enrollDate: "2024-04-07"
            startDate: "2024-06-01"
            expiryDate: "2025-05-31"
            value: "10000.00"
            productId: {self.product.id}
            familyId: {self.insuree.family.id}
            officerId: {self.officer.id}
          }}) {{ clientMutationId internalId }}
        }}
        """

    def test_create_policies_right(self):
        allowed = self._user("r_pol_c", ["gql_mutation_create_policies_perms"])
        denied = self._user("r_pol_c_no", [])
        mid = str(uuid.uuid4())
        self.assert_mutation_ok(allowed, self._create_policy_mutation(mid), mid)
        mid_no = str(uuid.uuid4())
        self.assert_mutation_unauthorized(
            denied, self._create_policy_mutation(mid_no), mid_no
        )

    def test_edit_renew_suspend_delete_policy_rights(self):
        denied = self._user("r_pol_mut_no", [])
        for perm, field in (
            ("gql_mutation_edit_policies_perms", "updatePolicy"),
            ("gql_mutation_suspend_policies_perms", "suspendPolicies"),
            ("gql_mutation_delete_policies_perms", "deletePolicies"),
            ("gql_mutation_renew_policies_perms", "renewPolicy"),
        ):
            with self.subTest(field=field):
                allowed = self._user(f"r_{field}"[:24], [perm])
                self.assert_user_has_named_perms(allowed, [perm])
                self.assert_user_lacks_named_perms(denied, [perm])
                mid_n = str(uuid.uuid4())
                if field in ("updatePolicy", "renewPolicy"):
                    mutation_no = f"""
                    mutation {{
                      {field}(input: {{
                        clientMutationId: "{mid_n}"
                        clientMutationLabel: "rights {field} no"
                        uuid: "{self.policy.uuid}"
                        enrollDate: "2024-04-07"
                        startDate: "2024-06-01"
                        expiryDate: "2025-05-31"
                        value: "10000.00"
                        productId: {self.product.id}
                        familyId: {self.insuree.family.id}
                        officerId: {self.officer.id}
                      }}) {{ clientMutationId internalId }}
                    }}
                    """
                else:
                    mutation_no = f"""
                    mutation {{
                      {field}(input: {{
                        clientMutationId: "{mid_n}"
                        clientMutationLabel: "rights {field} no"
                        uuids: ["{self.policy.uuid}"]
                      }}) {{ clientMutationId internalId }}
                    }}
                    """
                self.assert_mutation_unauthorized(denied, mutation_no, mid_n)
