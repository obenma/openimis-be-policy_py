"""A policy is only held by claims that were submitted, processed or valuated."""

from django.core.cache import cache
from django.test import TestCase

from claim.models import Claim, ClaimDedRem
from claim.test_helpers import create_test_claim, create_test_claimitem
from insuree.test_helpers import create_test_insuree
from policy.models import Policy
from policy.services import PolicyService
from policy.test_helpers import create_test_policy
from product.test_helpers import create_test_product


class PolicyDeleteClaimsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.product = create_test_product("OP3322D")

    def setUp(self):
        cache.clear()
        self.insuree = create_test_insuree(with_family=True, is_head=True)
        self.policy = create_test_policy(self.product, self.insuree, link=True)

    def _claim(self, status, with_item=True):
        claim = create_test_claim(custom_props={"insuree": self.insuree, "status": status})
        if with_item:
            create_test_claimitem(claim, custom_props={"policy": self.policy})
        return claim

    def _delete(self):
        return PolicyService(None).set_deleted(self.policy)

    def _is_deleted(self):
        return not Policy.objects.filter(
            id=self.policy.id, validity_to__isnull=True
        ).exists()

    def test_a_policy_without_claims_is_deleted(self):
        self.assertEqual(self._delete(), [])
        self.assertTrue(self._is_deleted())

    def test_entered_and_rejected_claims_do_not_hold_the_policy(self):
        self._claim(Claim.STATUS_ENTERED)
        self._claim(Claim.STATUS_REJECTED)

        self.assertEqual(self._delete(), [])
        self.assertTrue(self._is_deleted())

    def test_submitted_processed_and_valuated_claims_hold_the_policy(self):
        for status in PolicyService.BLOCKING_CLAIM_STATUSES:
            with self.subTest(status=status):
                claim = self._claim(status)

                error = self._delete()

                self.assertEqual(error["title"], self.policy.uuid)
                self.assertFalse(self._is_deleted())
                claim.status = Claim.STATUS_REJECTED
                claim.save()

    def test_a_deleted_claim_does_not_hold_the_policy(self):
        claim = self._claim(Claim.STATUS_CHECKED)
        claim.delete_history()

        self.assertEqual(self._delete(), [])

    def test_a_deduction_record_holds_the_policy(self):
        claim = self._claim(Claim.STATUS_VALUATED, with_item=False)
        ClaimDedRem.objects.create(
            policy=self.policy, insuree=self.insuree, claim=claim, audit_user_id=-1
        )

        self.assertIsInstance(self._delete(), dict)
        self.assertFalse(self._is_deleted())
