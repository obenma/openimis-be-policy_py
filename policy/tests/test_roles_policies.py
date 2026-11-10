from core.rights_role_test_case import RightsRoleGraphQLTestCase
from core.test_helpers import (
    create_accountant_role,
    create_claim_admin_role,
    create_clerk_role,
    create_data_entry_clerk_hf_role,
    create_enrolment_officer_role,
    create_hf_bound_role_user,
    create_medical_advisor_role,
    create_medical_officer_role,
    create_monitoring_evaluation_role,
    create_raf_role,
    create_receptionist_role,
    create_role_user,
    create_test_officer,
)
from insuree.test_helpers import create_test_insuree
from location.test_helpers import (
    create_basic_test_locations,
    create_test_health_facility,
    create_test_village,
)
from policy.tests.test_rights_policies import POLICIES_QUERY
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product


class PolicyRoleTests(RightsRoleGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_basic_test_locations()
        cls.village = create_test_village()
        cls.district = cls.village.parent.parent
        cls.hf = create_test_health_facility(code="POLHF", location_id=cls.district.id)
        cls.insuree = create_test_insuree(
            with_family=True,
            is_head=True,
            custom_props={"current_village": cls.village},
            family_custom_props={"location": cls.village},
        )
        cls.product = create_test_product("PROLE")
        create_test_policy(cls.product, cls.insuree, link=True)
        cls.districts = cls.DISTRICT_CODES + [cls.district.code]
        cls.officer = create_test_officer(
            villages=[cls.village], custom_props={"code": "POLEO"}
        )
        cls.query_users = {
            "accountant": create_role_user(
                "pol_acc", create_accountant_role(), district_codes=cls.districts
            ),
            "claim_admin": create_hf_bound_role_user(
                "pol_ca",
                create_claim_admin_role(),
                health_facility=cls.hf,
                district_codes=cls.districts,
            ),
            "clerk": create_role_user(
                "pol_clk", create_clerk_role(), district_codes=cls.districts
            ),
            "enrolment_officer": create_role_user(
                "pol_eo",
                create_enrolment_officer_role(),
                district_codes=cls.districts,
                officer=cls.officer,
            ),
            "receptionist": create_role_user(
                "pol_rec", create_receptionist_role(), district_codes=cls.districts
            ),
            "medical_officer": create_role_user(
                "pol_mo", create_medical_officer_role(), district_codes=cls.districts
            ),
            "data_entry_clerk": create_hf_bound_role_user(
                "pol_dec",
                create_data_entry_clerk_hf_role(),
                health_facility=cls.hf,
                with_officer=True,
                villages=[cls.village],
                district_codes=cls.districts,
            ),
            "medical_advisor": create_role_user(
                "pol_ma", create_medical_advisor_role(), district_codes=cls.districts
            ),
            "raf": create_role_user(
                "pol_raf", create_raf_role(), district_codes=cls.districts
            ),
            "me": create_role_user(
                "pol_me", create_monitoring_evaluation_role(), district_codes=cls.districts
            ),
        }

    def test_roles_can_query_policies(self):
        for name, user in self.query_users.items():
            with self.subTest(role=name):
                self.assert_user_has_named_perms(user, ["gql_query_policies_perms"])
                self.assert_gql_ok(user, POLICIES_QUERY)

    def test_roles_have_create_policy_right(self):
        for name in ("clerk", "enrolment_officer", "data_entry_clerk"):
            with self.subTest(role=name):
                self.assert_user_has_named_perms(
                    self.query_users[name], ["gql_mutation_create_policies_perms"]
                )

    def test_roles_have_eligibility_right(self):
        for name in ("data_entry_clerk", "medical_advisor", "raf", "me", "medical_officer"):
            with self.subTest(role=name):
                self.assert_user_has_named_perms(
                    self.query_users[name], ["gql_query_eligibilities_perms"]
                )
                query = f"""
                query {{
                  policyEligibilityByInsuree(chfId: "{self.insuree.chf_id}") {{
                    prodId
                  }}
                }}
                """
                self.assert_gql_ok(self.query_users[name], query)
