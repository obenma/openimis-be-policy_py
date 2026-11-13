"""
Tests for backend/features/policy.feature (Redmine CSU "Evolution" issues).
Each test names the scenario it covers.
"""
import unittest
import datetime
from unittest import mock

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.test import TestCase

from core.test_helpers import create_test_interactive_user, create_right_only_user, create_test_officer
from insuree.test_helpers import create_test_insuree
from policy.apps import PolicyConfig
from policy.models import Policy
from policy.services import PolicyService, update_renewals
from policy.test_helpers import create_test_policy
from policy.validations import validate_idle_policy
from policy.values import set_expiry_date
from product.test_helpers import create_test_product
from program.test_helpers import create_test_program

HIV_MARKER_EMAIL = "newhivuser_XM7dw70J0M3N@gmail.com"


def _insuree_aged(years, months=0, ref=None, **props):
    ref = ref or datetime.date.today()
    dob = ref - relativedelta(years=years, months=months)
    return create_test_insuree(custom_props={"dob": dob, **props})


def _policy_data(product, insuree, officer, user, **extra):
    today = datetime.date.today()
    return {
        "enroll_date": today,
        "start_date": today,
        "expiry_date": today + relativedelta(years=1),
        "value": "0",
        "product_id": product.id,
        "family_id": insuree.family.id,
        "officer_id": officer.id,
        "audit_user_id": user.id_for_audit,
        **extra,
    }


def _active_policy(product, insuree, status=Policy.STATUS_ACTIVE):
    policy = create_test_policy(product, insuree)
    Policy.objects.filter(id=policy.id).update(status=status, validity_to=None)
    policy.refresh_from_db()
    return policy


class OnePolicyPerProgramTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="featpolopp")
        cls.officer = create_test_officer()
        cls.tb = create_test_program(code="TBF", name="TB feature")
        cls.hd = create_test_program(code="HDF", name="Hemodialysis feature")

    def setUp(self):
        patcher = mock.patch.object(PolicyConfig, "one_policy_per_program", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # Scenario: A second active policy of the same program is refused by the backend (@redmine-27338)
    def test_second_active_policy_same_program_refused(self):
        insuree = create_test_insuree()
        p1 = create_test_product("FTB1", custom_props={"program": self.tb})
        p2 = create_test_product("FTB2", custom_props={"program": self.tb})
        _active_policy(p1, insuree)
        with self.assertRaisesRegex(Exception, "plusieurs polices actives pour un même programme"):
            PolicyService(self.user).create_policy(_policy_data(p2, insuree, self.officer, self.user), self.user)

    # Scenario: Policies of different programs can be active at the same time (@redmine-27338)
    def test_policies_of_different_programs(self):
        insuree = create_test_insuree()
        p_hd = create_test_product("FHD1", custom_props={"program": self.hd})
        p_tb = create_test_product("FTB3", custom_props={"program": self.tb})
        _active_policy(p_hd, insuree)
        policy = PolicyService(self.user).create_policy(
            _policy_data(p_tb, insuree, self.officer, self.user), self.user)
        self.assertIsNotNone(policy.id)

    # Scenario: A new policy can be added when the same-program policy is suspended (@redmine-34235)
    def test_new_policy_when_same_program_policy_suspended(self):
        insuree = create_test_insuree()
        p1 = create_test_product("FTB4", custom_props={"program": self.tb})
        p2 = create_test_product("FTB5", custom_props={"program": self.tb})
        _active_policy(p1, insuree, status=Policy.STATUS_SUSPENDED)
        policy = PolicyService(self.user).update_or_create(
            _policy_data(p2, insuree, self.officer, self.user), self.user)
        self.assertIsNotNone(policy.id)

    # Scenario: A policy cannot be enrolled in the future (@redmine-34235)
    def test_enroll_date_in_future_refused(self):
        insuree = create_test_insuree()
        product = create_test_product("FTB6", custom_props={"program": self.tb})
        data = _policy_data(product, insuree, self.officer, self.user,
                            enroll_date=datetime.date.today() + datetime.timedelta(days=1))
        with self.assertRaisesRegex(Exception, "policy.enroll_date_in_the_future"):
            PolicyService(self.user).update_or_create(data, self.user)


class ProductAgeLimitTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="featpolage")
        cls.officer = create_test_officer()

    def _save(self, age_min, age_max, age):
        insuree = _insuree_aged(age)
        product = create_test_product("FAGE", custom_props={"age_minimal": age_min, "age_maximal": age_max})
        return PolicyService(self.user).update_or_create(
            _policy_data(product, insuree, self.officer, self.user), self.user)

    # Scenario: The beneficiary's age is checked against the product age limits (@redmine-27598 @redmine-35067)
    def test_age_checked_against_limits(self):
        cases = [
            (10, 50, 3, "n'a pas encore l'âge minimal requis"),
            (10, 50, 73, "a dépassé\\(e\\) l'âge maximal"),
            (10, 50, 10, None),
            (0, 5, 15, "a dépassé\\(e\\) l'âge maximal"),
            (18, 0, 15, "n'a pas encore l'âge minimal requis"),
        ]
        for age_min, age_max, age, error in cases:
            with self.subTest(min=age_min, max=age_max, age=age):
                if error:
                    with self.assertRaisesRegex(Exception, error):
                        self._save(age_min, age_max, age)
                else:
                    self.assertIsNotNone(self._save(age_min, age_max, age).id)

    # Scenario: An age limit of 0 means no limit (@redmine-34849 @redmine-35045)
    def test_zero_means_no_limit(self):
        for age in (52, 1):
            with self.subTest(age=age):
                self.assertIsNotNone(self._save(0, 0, age).id)


class ExpiryCappedAtMaxAgeTest(TestCase):
    START = datetime.date(2026, 1, 15)

    def _expiry(self, age_max, years, months=0):
        insuree = _insuree_aged(years, months, ref=self.START)
        product = create_test_product("FEXP", custom_props={"age_maximal": age_max, "insurance_period": 12})
        policy = Policy(product=product, family=insuree.family, start_date=self.START,
                        enroll_date=self.START, stage=Policy.STAGE_NEW)
        set_expiry_date(policy, insuree.family, self.START)
        return policy.expiry_date, insuree

    # Scenario: The beneficiary reaches the maximum age during the coverage (@redmine-34378)
    def test_expiry_capped_at_max_age_birthday(self):
        expiry, insuree = self._expiry(46, 45, 6)
        dob = insuree.dob if isinstance(insuree.dob, datetime.date) else \
            datetime.datetime.strptime(str(insuree.dob), "%Y-%m-%d").date()
        self.assertEqual(expiry, dob + relativedelta(years=46))

    # Scenario: The beneficiary stays under the maximum age during the coverage (@redmine-34378)
    def test_expiry_full_period_under_max_age(self):
        expiry, _ = self._expiry(46, 30)
        self.assertEqual(expiry, datetime.date(2027, 1, 14))

    # Scenario: A product without maximum age keeps the product insurance period (@redmine-35045)
    def test_expiry_without_max_age(self):
        expiry, _ = self._expiry(0, 80)
        self.assertEqual(expiry, datetime.date(2027, 1, 14))
        self.assertNotEqual(expiry, self.START)


class HivUserFeesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vih = create_test_program(code="VIHF", name="VIH")

    def _validate(self, code, email=None):
        props = {"email": email} if email else {}
        insuree = create_test_insuree(custom_props=props)
        product = create_test_product(code, custom_props={"program": self.vih})
        return validate_idle_policy({"product_id": product.id, "family_id": insuree.family.id})

    # Scenario: Only the HIV-negative product can be attributed to a beneficiary without HIV (@redmine-28672)
    def test_only_negative_product_for_general_public(self):
        self.assertEqual(self._validate("CSU-UF"), [])
        errors = self._validate("CSU-VIHN")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["detail"],
                         "Cannot create a positive HIV policy for a patient that does not have HIV")

    # Scenario: An HIV patient cannot get the HIV-negative policy (@redmine-28672)
    def test_hiv_patient_cannot_get_negative_policy(self):
        errors = self._validate("CSU-UF", email=HIV_MARKER_EMAIL)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["detail"],
                         "Cannot create a Negative HIV policy for a patient that effectively has HIV")


class ForceExpirationTest(TestCase):
    GAP = "forced expiration (#37437, policy PR 152-154) is not in this checkout: it is on origin/dev-csu"

    @classmethod
    def setUpTestData(cls):
        cls.product = create_test_product("FFEX")

    def _policy(self):
        return _active_policy(self.product, create_test_insuree())

    # Scenario: The force expiration right exists (@redmine-37437 @redmine-36614)
    def test_right_101208_declared(self):
        from policy.schema import Mutation
        self.assertEqual(getattr(PolicyConfig, "gql_mutation_force_expiration_perms", None), ["101208"], self.GAP)
        self.assertIn("force_policies_expiration", Mutation._meta.fields, self.GAP)

    # Scenario: Forcing the expiration sets status 8 and stage F on the execution date (@redmine-37437)
    def test_force_expiration_sets_status_and_stage(self):
        policy = self._policy()
        mutation = self._mutation()
        user = create_right_only_user("featfexok", [101208])
        errors = mutation.async_mutate(user, uuids=[str(policy.uuid)])
        self.assertFalse(errors)
        policy.refresh_from_db()
        self.assertEqual(policy.status, Policy.STATUS_EXPIRED)
        self.assertEqual(policy.stage, "F")
        self.assertEqual(policy.expiry_date, datetime.date.today())
        self.assertTrue(Policy.objects.filter(legacy_id=policy.id).exists())

    # Scenario: A user without right 101208 cannot force the expiration (@redmine-37437)
    def test_force_expiration_needs_right(self):
        policy = self._policy()
        mutation = self._mutation()
        user = create_right_only_user("featfexko", [101201])
        errors = mutation.async_mutate(user, uuids=[str(policy.uuid)])
        self.assertTrue(errors)
        policy.refresh_from_db()
        self.assertEqual(policy.status, Policy.STATUS_ACTIVE)

    def _mutation(self):
        from policy import gql_mutations
        mutation = getattr(gql_mutations, "ForcePoliciesExpirationMutation", None)
        self.assertIsNotNone(mutation, self.GAP)
        return mutation


class RenewalTaskTest(TestCase):
    # Scenario: update_renewals leaves policies already expired untouched (@redmine-34024)
    def test_update_renewals_skips_expired(self):
        product = create_test_product("FREN")
        insuree = create_test_insuree()
        expired = _active_policy(product, insuree, status=Policy.STATUS_EXPIRED)
        active = _active_policy(product, insuree)
        past = datetime.date(2020, 1, 1)
        Policy.objects.filter(id__in=[expired.id, active.id]).update(expiry_date=past)
        to_expire = Policy.objects.filter(validity_to__isnull=True, expiry_date__lt=datetime.date.today()) \
            .exclude(status=Policy.STATUS_EXPIRED).count()

        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            updated = update_renewals()
        self.assertEqual(updated, to_expire)
        active.refresh_from_db()
        self.assertEqual(active.status, Policy.STATUS_EXPIRED)
        update_sql = [q["sql"] for q in ctx.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]
        self.assertEqual(len(update_sql), 1)
        self.assertIn("NOT", update_sql[0].upper())

    # Scenario: The policy renewal task runs at night (@redmine-34023)
    def test_renewal_job_scheduled_at_0205(self):
        jobs = [j for j in settings.SCHEDULER_JOBS if j.get("method") == "policy.tasks.get_policies_for_renewal"]
        self.assertEqual(len(jobs), 1)
        kwargs = jobs[0]["kwargs"]
        self.assertEqual((kwargs.get("hour"), kwargs.get("minute")), (2, 5),
                         "openIMIS settings still schedule the renewal batch at %s:%s (be_py PR 60/61 not here)"
                         % (kwargs.get("hour"), kwargs.get("minute")))


class PregnancyAntiFraudTest(TestCase):
    # Scenario: Selectable trimesters depend on the pregnancy age (@redmine-35891)
    @unittest.skip("open: #35891")
    def test_trimesters_follow_pregnancy_age(self):
        pass

    # Scenario: A new pregnancy stays billable (@redmine-35891)
    @unittest.skip("open: #35891")
    def test_new_pregnancy_billable(self):
        pass
