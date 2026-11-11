"""
update_insuree_policies_dates: the member dates the contribution module sets on payment,
owned by the policy module (OP-3321).
"""
import datetime

from django.test import TestCase

from insuree.models import InsureePolicy
from insuree.test_helpers import create_test_insuree
from policy.models import Policy
from policy.services import update_insuree_policies_dates
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product


class UpdateInsureePoliciesDatesTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.product = create_test_product("IPDATES")
        cls.insuree = create_test_insuree(with_family=True, custom_props={"chf_id": "IPDATES01"})
        cls.policy = create_test_policy(cls.product, cls.insuree, link=True)

    def _ip(self):
        return InsureePolicy.objects.get(policy=self.policy, validity_to__isnull=True)

    def test_policy_dates_given_to_every_member(self):
        Policy.objects.filter(id=self.policy.id).update(
            start_date=datetime.date(2024, 1, 1), effective_date=datetime.date(2024, 1, 5),
            expiry_date=datetime.date(2024, 12, 31))
        self.policy.refresh_from_db()
        update_insuree_policies_dates(self.policy)
        ip = self._ip()
        self.assertEqual(
            (datetime.date(2024, 1, 1), datetime.date(2024, 1, 5), datetime.date(2024, 12, 31)),
            (ip.start_date, ip.effective_date, ip.expiry_date))

    def test_reactivation_changes_the_effective_date_only(self):
        before = self._ip()
        update_insuree_policies_dates(self.policy, activation_date=datetime.date(2024, 3, 3))
        ip = self._ip()
        self.assertEqual(datetime.date(2024, 3, 3), ip.effective_date)
        self.assertEqual((before.start_date, before.expiry_date), (ip.start_date, ip.expiry_date))

    def test_superseded_rows_untouched(self):
        old = InsureePolicy.objects.create(
            insuree=self.insuree, policy=self.policy, audit_user_id=-1,
            effective_date=datetime.date(2019, 1, 1), expiry_date=datetime.date(2019, 6, 1),
            validity_to=datetime.datetime(2019, 6, 1))
        update_insuree_policies_dates(self.policy, activation_date=datetime.date(2024, 3, 3))
        old.refresh_from_db()
        self.assertEqual(datetime.date(2019, 1, 1), old.effective_date)
