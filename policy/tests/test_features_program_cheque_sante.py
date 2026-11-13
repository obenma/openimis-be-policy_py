"""
Tests for backend/features/program-cheque-sante.feature, policy module part.
"""
import datetime

from dateutil.relativedelta import relativedelta
from django.test import TestCase

from core.test_helpers import create_test_interactive_user, create_test_officer
from cs.models import ChequeImport, ChequeImportLine
from insuree.test_helpers import create_test_insuree
from policy.models import Policy
from policy.services import PolicyService
from product.test_helpers import create_test_product


class ChequeAttributionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="featcheque")
        cls.officer = create_test_officer()
        cls.product = create_test_product("FCST1")
        cls.cheque_import = ChequeImport.objects.create(user=cls.user.i_user)

    def _cheque(self, number, status):
        return ChequeImportLine.objects.create(
            chequeImportId=self.cheque_import, chequeImportLineCode=number, chequeImportLineStatus=status)

    def _create(self, number):
        insuree = create_test_insuree()
        today = datetime.date.today()
        data = {
            "enroll_date": today,
            "start_date": today,
            "expiry_date": today + relativedelta(years=1),
            "value": "0",
            "product_id": self.product.id,
            "family_id": insuree.family.id,
            "officer_id": self.officer.id,
            "audit_user_id": self.user.id_for_audit,
            "policy_number": number,
        }
        return PolicyService(self.user).update_or_create(data, self.user)

    # Scenario: An already used cheque is refused with "déjà utilisé" (@redmine-27168 @redmine-35627)
    def test_used_cheque_refused(self):
        self._cheque("CC240523", "Used")
        with self.assertRaises(Exception) as cm:
            self._create("CC240523")
        self.assertEqual(str(cm.exception), "Chèque CC240523 déjà utilisé")
        self.assertNotIn("invalide", str(cm.exception))

    # Scenario: An unknown cheque is refused (@redmine-27168)
    def test_unknown_cheque_refused(self):
        with self.assertRaises(Exception) as cm:
            self._create("ZZ999999")
        self.assertEqual(str(cm.exception), "Le chèque ZZ999999 n'existe pas dans le système")

    # Scenario: Attaching a new cheque to a policy marks it as used (@redmine-35627 @redmine-28691)
    def test_new_cheque_becomes_used(self):
        cheque = self._cheque("CC240524", "New")
        policy = self._create("CC240524")
        self.assertIsNotNone(policy.id)
        cheque.refresh_from_db()
        self.assertEqual(cheque.chequeImportLineStatus, "Used")

    # Scenario: Cheque numbers have no fixed length or character type (@redmine-28637 @redmine-28698)
    def test_cheque_numbers_any_length(self):
        for number in ("504995", "5451601", "CC240525", "CC2405250"):
            with self.subTest(number=number):
                self._cheque(number, "New")
                policy = self._create(number)
                self.assertEqual(policy.policy_number, number)


class PregnancyAgeOnPolicyTest(TestCase):
    # Scenario: The policy stores the pregnancy age in weeks (@redmine-34207 @redmine-35074)
    def test_policy_stores_pregnancy_age(self):
        from policy.test_helpers import create_test_policy
        policy = create_test_policy(create_test_product("FCSPA"), create_test_insuree(),
                                    custom_props={"pregnancy_age": 12})
        self.assertEqual(Policy.objects.get(id=policy.id).pregnancy_age, 12)
