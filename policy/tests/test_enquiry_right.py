"""
`policiesByInsuree` and eligibility are the enquiry, and take its right.

`ByInsureeService` filters on the CHFID and on nothing else, so any identifier resolves
to that insuree's policies wherever in the country they live. That is the enquiry -
`insuree.EnquiryDialog` mounts `FamilyOrInsureePoliciesSummary` - and the claim panel
embeds it through `policy.InsureePolicyEligibilitySummary`.

`policiesByInsuree` used to be authorised by borrowing `ClaimConfig`'s claim read
right: being able to read claims silently carried being able to read the country's
policies, one CHFID at a time. It now takes 101105, `InsureeConfig`'s enquiry right,
checked as insuree's rather than re-declared under a policy name - one right, one
permission name, in the module that owns it. Using an existing, already-seeded id
beats minting a new one: a role already entitled to enquire needs nothing new, and the
policy register (101201) is not handed over with it.

Eligibility goes the same way. It is keyed on an insuree and answers whether this
person may be served here and now: the same business act as the lookup, and no claim
is entered without that lookup first - the converse holds, the implication does not.
`EligibilityService.request` carries the pair as well, because REST and FHIR reach
eligibility without passing through the resolver.

101201 keeps authorising all of it. Nothing is taken away.
"""

import ast
import inspect
import re
import textwrap

from core.rights_role_test_case import RightsRoleGraphQLTestCase
from core.test_helpers import create_right_only_user
from insuree.test_helpers import create_test_insuree
from location.test_helpers import create_basic_test_locations, create_test_village
from policy import schema as policy_schema
from insuree.apps import InsureeConfig
from policy.apps import PolicyConfig
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product

PERM_REF = re.compile(r"(?:PolicyConfig|InsureeConfig)\.(\w*perms\w*)")

ELIGIBILITY_RESOLVERS = (
    "resolve_policy_eligibility_by_insuree",
    "resolve_policy_item_eligibility_by_insuree",
    "resolve_policy_service_eligibility_by_insuree",
)


def _resolver_source(name):
    # dedent: getsource on a method returns it at class indentation, which
    # ast.parse rejects on its own
    source = textwrap.dedent(inspect.getsource(getattr(policy_schema.Query, name)))
    # ast.unparse drops comments, so a right named in a comment cannot count
    return ast.unparse(ast.parse(source))


def _perms_in_resolver(name):
    return set(PERM_REF.findall(_resolver_source(name)))


class PolicyEnquiryRightTestCase(RightsRoleGraphQLTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_basic_test_locations()
        cls.village = create_test_village({"code": "ENQPOL"})
        cls.insuree = create_test_insuree(
            with_family=True,
            is_head=True,
            custom_props={"current_village": cls.village},
            family_custom_props={"location": cls.village},
        )
        cls.product = create_test_product("PENQ")
        cls.policy = create_test_policy(cls.product, cls.insuree, link=True)
        cls.districts = cls.DISTRICT_CODES + [cls.village.parent.parent.code]
        cls.by_insuree = f"""
        query {{
          policiesByInsuree(chfId: "{cls.insuree.chf_id}") {{
            edges {{ node {{ policyUuid }} }}
          }}
        }}
        """
        cls.eligibility = f"""
        query {{
          policyEligibilityByInsuree(chfId: "{cls.insuree.chf_id}") {{
            prodId
          }}
        }}
        """

    def _user(self, name, perms):
        return create_right_only_user(name, perms, district_codes=self.districts)

    def test_the_enquiry_right_is_the_insuree_enquiry_id(self):
        self.assertEqual(InsureeConfig.gql_query_insuree_inquire_perms, ["101105"])

    def test_policy_does_not_re_declare_the_enquiry_right(self):
        """One right, one permission name: policy references insuree's, it has no own."""
        from policy.apps import DJANGO_PERMS

        ids = {pair[1] for actions in DJANGO_PERMS.values() for pair in actions.values()}
        self.assertNotIn(101105, ids)

    def test_the_enquiry_right_is_not_the_policy_register(self):
        """
        It must not drag 101201 along, or revoking it would take the policy module
        away with it.
        """
        enquiry = set(InsureeConfig.gql_query_insuree_inquire_perms)
        for attr in dir(PolicyConfig):
            if not attr.endswith("_perms"):
                continue
            value = getattr(PolicyConfig, attr, None)
            if not isinstance(value, (list, tuple)):
                continue
            with self.subTest(other=attr):
                self.assertEqual(enquiry & set(value), set())

    def test_by_insuree_checks_policy_rights_only(self):
        self.assertEqual(
            _perms_in_resolver("resolve_policies_by_insuree"),
            {
                "gql_query_policies_by_insuree_perms",
                "gql_query_insuree_inquire_perms",
            },
        )

    def test_by_insuree_no_longer_borrows_a_claim_right(self):
        self.assertNotIn("ClaimConfig", _resolver_source("resolve_policies_by_insuree"))

    def test_eligibility_resolvers_accept_the_enquiry_right(self):
        for name in ELIGIBILITY_RESOLVERS:
            with self.subTest(resolver=name):
                self.assertEqual(
                    _perms_in_resolver(name),
                    {
                        "gql_query_eligibilities_perms",
                        "gql_query_insuree_inquire_perms",
                    },
                )

    def test_the_eligibility_service_carries_the_same_pair(self):
        """REST and FHIR never see the resolver, so the service is the real gate."""
        import ast as _ast
        import inspect as _inspect
        import textwrap as _textwrap

        from policy.services import EligibilityService

        source = _ast.unparse(
            _ast.parse(_textwrap.dedent(_inspect.getsource(EligibilityService.request)))
        )
        self.assertEqual(
            set(PERM_REF.findall(source)),
            {"gql_query_eligibilities_perms", "gql_query_insuree_inquire_perms"},
        )

    def test_enquiry_right_alone_resolves_the_lookup(self):
        user = self._user("enq_pol_only", ["gql_query_insuree_inquire_perms"])
        self.assert_user_lacks_named_perms(
            user, ["gql_query_policies_by_insuree_perms"]
        )
        self.assert_gql_ok(user, self.by_insuree)

    def test_enquiry_right_alone_reaches_eligibility(self):
        user = self._user("enq_pol_elig", ["gql_query_insuree_inquire_perms"])
        self.assert_user_lacks_named_perms(user, ["gql_query_eligibilities_perms"])
        self.assert_gql_ok(user, self.eligibility)

    def test_policy_register_right_still_resolves_both(self):
        user = self._user("enq_pol_reg", ["gql_query_policies_by_insuree_perms"])
        self.assert_gql_ok(user, self.by_insuree)
        self.assert_gql_ok(user, self.eligibility)

    def test_neither_right_is_refused(self):
        user = self._user("enq_pol_none", [])
        self.assert_gql_unauthorized(user, self.by_insuree)
        self.assert_gql_unauthorized(user, self.eligibility)
