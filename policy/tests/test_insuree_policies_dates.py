"""
OP-3321: the members of a policy with their own validity (policy.utils.get_validity, the
calculation rule's "validity" context, the policy dates by default), the InsureePolicy
dates set from it, and the activation never before the start date.
"""
import datetime
from unittest import mock

from django.test import TestCase

from insuree.models import InsureePolicy
from insuree.test_helpers import create_test_insuree
from policy.models import Policy
from policy.services import update_insuree_policies, update_insuree_policies_dates, policy_status_premium_paid
from policy.test_helpers import create_test_policy
from policy.utils import get_validity, member_dates
from product.test_helpers import create_test_product

START = datetime.date(2024, 1, 1)
EFFECTIVE = datetime.date(2024, 1, 5)
EXPIRY = datetime.date(2024, 12, 31)


def _rule_answers(validity):
    """the policy has a contribution plan whose calculation rule answers `validity`"""
    return mock.patch.multiple(
        "policy.utils",
        _contribution_plan=mock.Mock(return_value=mock.Mock()),
        run_calculation_rules=mock.Mock(return_value=validity),
    )


class InsureePoliciesValidityTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.product = create_test_product("IPDATES")
        cls.insuree = create_test_insuree(with_family=True, custom_props={"chf_id": "IPDATES01"})
        cls.other = create_test_insuree(with_family=False, custom_props={
            "chf_id": "IPDATES02", "family": cls.insuree.family})
        cls.policy = create_test_policy(cls.product, cls.insuree, link=True)
        Policy.objects.filter(id=cls.policy.id).update(
            start_date=START, effective_date=EFFECTIVE, expiry_date=EXPIRY)
        cls.policy.refresh_from_db()

    def _ips(self, field):
        return {ip.insuree.chf_id: getattr(ip, field)
                for ip in InsureePolicy.objects.filter(policy=self.policy, validity_to__isnull=True)}

    # --- get_validity
    def test_without_rule_every_member_with_the_policy_dates(self):
        validity = get_validity(self.policy, self.insuree.family, None, [self.insuree, self.other])
        self.assertEqual([(self.insuree, None), (self.other, None)], validity)

    def test_rule_member_without_validity_is_not_covered(self):
        with _rule_answers({self.insuree.id: {"start_date": datetime.date(2024, 3, 1)}}):
            validity = get_validity(self.policy, self.insuree.family, None, [self.insuree, self.other])
        self.assertEqual([self.insuree], [member for member, _ in validity])

    def test_rule_single_validity_applied_to_every_member(self):
        """the former structure (one validity, as for a contract) is applied to each member"""
        single = {"enroll_date": START, "start_date": datetime.date(2024, 2, 1),
                  "effective_date": datetime.date(2024, 2, 1), "expiry_date": datetime.date(2024, 10, 31)}
        with _rule_answers(single):
            validity = get_validity(self.policy, self.insuree.family, None, [self.insuree, self.other])
        self.assertEqual([self.insuree, self.other], [member for member, _ in validity])
        self.assertEqual({datetime.date(2024, 10, 31)}, {dates["expiry_date"] for _, dates in validity})

    def test_policy_is_the_calling_context_without_contribution_plan(self):
        with mock.patch("policy.utils.run_calculation_rules", return_value=None) as rules:
            get_validity(self.policy, self.insuree.family, None, [self.insuree])
        kwargs = rules.call_args.kwargs
        self.assertEqual(("validity", self.policy, self.policy),
                         (kwargs["context"], kwargs["instance"], kwargs["policy"]))
        self.assertNotIn("sender", kwargs, "run_calculation_rules sets it: passing it again raises")

    # --- member_dates: within the policy, never effective before the start
    def test_member_dates_within_the_policy(self):
        dates = member_dates(self.policy, {"start_date": datetime.date(2023, 6, 1),
                                           "expiry_date": datetime.date(2025, 6, 1)})
        self.assertEqual((START, EFFECTIVE, EXPIRY),
                         (dates["start_date"], dates["effective_date"], dates["expiry_date"]))
        later = member_dates(self.policy, {"start_date": datetime.date(2024, 3, 1)})
        self.assertEqual(datetime.date(2024, 3, 1), later["effective_date"], "effective once started")

    # --- update_insuree_policies
    def test_insuree_policies_from_the_validity(self):
        with _rule_answers({self.insuree.id: {"start_date": datetime.date(2024, 3, 1),
                                              "expiry_date": datetime.date(2024, 9, 30)}}):
            InsureePolicy.objects.create(insuree=self.other, policy=self.policy, audit_user_id=-1,
                                         start_date=START, effective_date=EFFECTIVE, expiry_date=EXPIRY)
            update_insuree_policies(self.policy, None)
        self.assertEqual({"IPDATES01": datetime.date(2024, 3, 1)}, self._ips("start_date"),
                         "IPDATES02 no longer covered: closed")
        self.assertEqual({"IPDATES01": datetime.date(2024, 9, 30)}, self._ips("expiry_date"))

    # --- update_insuree_policies_dates (called by contribution on payment)
    def test_policy_dates_given_to_every_member(self):
        InsureePolicy.objects.filter(policy=self.policy).update(start_date=None, expiry_date=None)
        update_insuree_policies_dates(self.policy)
        self.assertEqual({"IPDATES01": (START, EFFECTIVE, EXPIRY)},
                         {k: (v, self._ips("effective_date")[k], self._ips("expiry_date")[k])
                          for k, v in self._ips("start_date").items()})

    def test_reactivation_changes_the_effective_date_only(self):
        before = self._ips("expiry_date")
        update_insuree_policies_dates(self.policy, activation_date=datetime.date(2024, 3, 3))
        self.assertEqual({"IPDATES01": datetime.date(2024, 3, 3)}, self._ips("effective_date"))
        self.assertEqual(before, self._ips("expiry_date"))

    def test_reactivation_never_before_the_start(self):
        update_insuree_policies_dates(self.policy, activation_date=datetime.date(2023, 12, 1))
        self.assertEqual({"IPDATES01": START}, self._ips("effective_date"))

    def test_superseded_rows_untouched(self):
        old = InsureePolicy.objects.create(
            insuree=self.insuree, policy=self.policy, audit_user_id=-1,
            effective_date=datetime.date(2019, 1, 1), expiry_date=datetime.date(2019, 6, 1),
            validity_to=datetime.datetime(2019, 6, 1))
        update_insuree_policies_dates(self.policy, activation_date=datetime.date(2024, 3, 3))
        old.refresh_from_db()
        self.assertEqual(datetime.date(2019, 1, 1), old.effective_date)

    # --- activation of a policy dated in the future
    @mock.patch("policy.services.PolicyConfig.activation_option", 1)
    @mock.patch("policy.services.PolicyConfig.ACTIVATION_OPTION_CONTRIBUTION", 1)
    def test_future_policy_effective_from_its_start(self):
        policy = Policy(start_date=datetime.date(2030, 1, 1))
        policy_status_premium_paid(policy, datetime.date(2029, 11, 11))
        self.assertEqual(datetime.date(2030, 1, 1), policy.effective_date)
        policy_status_premium_paid(policy, datetime.date(2030, 2, 2))
        self.assertEqual(datetime.date(2030, 2, 2), policy.effective_date)


class PolicyStartFromTheMembersTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.product = create_test_product("IPSTART", custom_props={"insurance_period": 12})
        cls.insuree = create_test_insuree(with_family=True, custom_props={"chf_id": "IPSTART01"})

    def test_start_from_the_earliest_member(self):
        from policy.values import policy_values
        policy = Policy(family=self.insuree.family, product=self.product, stage=Policy.STAGE_NEW,
                        enroll_date=datetime.date(2024, 1, 10))
        with _rule_answers({self.insuree.id: {"start_date": datetime.date(2024, 4, 1)}}):
            with mock.patch("policy.values.set_value"):
                policy, _ = policy_values(policy, self.insuree.family, None, None, [self.insuree])
        self.assertEqual(datetime.date(2024, 4, 1), policy.start_date)

    def test_start_from_the_enrolment_without_rule(self):
        from policy.values import policy_values
        policy = Policy(family=self.insuree.family, product=self.product, stage=Policy.STAGE_NEW,
                        enroll_date=datetime.date(2024, 1, 10))
        with mock.patch("policy.values.set_value"):
            policy, _ = policy_values(policy, self.insuree.family, None, None, [self.insuree])
        self.assertEqual(datetime.date(2024, 1, 10), policy.start_date)
