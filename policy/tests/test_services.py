from unittest import mock, skip
import datetime
from django.core.exceptions import PermissionDenied
from insuree.apps import InsureeConfig
from policy.apps import PolicyConfig
from claim.test_helpers import (
    create_test_claim,
    create_test_claimservice,
    create_test_claimitem,
)
from claim.services import processing_claim
from core.test_helpers import create_test_officer, create_test_interactive_user
from django.test import TestCase
from insuree.test_helpers import create_test_photo
from medical.test_helpers import create_test_item, create_test_service
from medical_pricelist.test_helpers import (
    add_service_to_hf_pricelist,
    add_item_to_hf_pricelist,
)
from policy.test_helpers import create_test_policy2, create_test_insuree_for_policy
from product.test_helpers import (
    create_test_product,
    create_test_product_service,
    create_test_product_item,
)
from policy.services import (
    policy_renewal_sms,
    insert_renewals,
    update_renewals,
    signal_eligibility_service_before,
    EligibilityRequest,
    EligibilityService,
    EligibilityResponse,
    NativeEligibilityService,
    PolicyService,
)
from policy.models import PolicyRenewal, Policy


class EligibilityServiceTestCase(TestCase):
    def setUp(self) -> None:
        super(EligibilityServiceTestCase, self).setUp()
        self.user = mock.Mock(is_anonymous=False)
        self.user.has_perms = mock.MagicMock(return_value=True)
        self.user.id_for_audit = -1

    def test_eligibility_request_permission_denied(self):
        with mock.patch("django.db.backends.utils.CursorWrapper") as mock_cursor:
            mock_cursor.return_value.description = None
            mock_user = mock.Mock(is_anonymous=False)
            mock_user.has_perms = mock.MagicMock(return_value=False)
            req = EligibilityRequest(chf_id="a")
            service = EligibilityService(mock_user)
            with self.assertRaises(PermissionDenied):
                service.request(req)
            # Both rights are asked for, and denied: the eligibility right and the
            # insuree enquiry right, which reaches eligibility for the same business
            # act. `assert_called_with` only sees the last call, so check each.
            mock_user.has_perms.assert_any_call(
                PolicyConfig.gql_query_eligibilities_perms
            )
            mock_user.has_perms.assert_any_call(
                InsureeConfig.gql_query_insuree_inquire_perms
            )

    @skip(
        "this test hangs on psql, the mock destroys normal queries happening inside EligibilityRequest"
    )
    def test_eligibility_request_all_good(self):
        with mock.patch("django.db.backends.utils.CursorWrapper") as mock_cursor:
            return_values = [
                list(range(1, 13)),
                [
                    datetime.date(2020, 1, 9),
                    datetime.date(2020, 1, 10),
                    20,
                    21,
                    True,
                    True,
                ],
            ]
            # required for all modules tests
            mock_cursor.return_value.fetchone.side_effect = return_values
            # required for policy module tests
            mock_cursor.return_value.__enter__.return_value.fetchone.side_effect = (
                return_values
            )
            mock_user = mock.Mock(is_anonymous=False)
            insuree, family = create_test_insuree_for_policy(
                custom_props={"chf_id": "tier1234"}
            )
            product = create_test_product("ELI1")
            create_test_policy2(product, insuree)
            mock_user.has_perm = mock.MagicMock(return_value=True)
            req = EligibilityRequest(chf_id="tier1234")
            service = EligibilityService(mock_user)
            res = service.request(req)

            expected = EligibilityResponse(
                eligibility_request=req,
                prod_id=product.id,
                total_admissions_left=None,
                total_visits_left=None,
                total_consultations_left=None,
                total_surgeries_left=None,
                total_deliveries_left=None,
                total_antenatal_left=None,
                consultation_amount_left=None,
                surgery_amount_left=None,
                delivery_amount_left=None,
                hospitalization_amount_left=None,
                antenatal_amount_left=None,
                min_date_service=None,
                min_date_item=None,
                service_left=None,
                item_left=None,
                is_item_ok=True,
                is_service_ok=True,
            )
            self.assertEqual(expected, res)

    def eligibility_serv(self, category):
        insuree, family = create_test_insuree_for_policy(
            custom_props={"chf_id": "elgsp" + category}
        )
        product = create_test_product("ELI1")
        (policy, insuree_policy) = create_test_policy2(product, insuree)
        service = create_test_service(category)
        add_service_to_hf_pricelist(service)
        create_test_product_service(
            product, service, custom_props={"limit_no_adult": 20}
        )
        claim = create_test_claim(custom_props={"insuree_id": insuree.id})
        create_test_claimservice(
            claim, custom_props={"service_id": service.id}, product=product
        )
        errors = processing_claim(claim, self.user, True)
        self.assertEqual(len(errors), 0)

        native_el_svc = NativeEligibilityService(self.user)
        req = EligibilityRequest(chf_id=insuree.chf_id, service_code=service.code)
        expected_resposnse = EligibilityResponse(
            antenatal_amount_left=None,
            consultation_amount_left=None,
            delivery_amount_left=None,
            eligibility_request=req,
            final=False,
            hospitalization_amount_left=None,
            is_item_ok=True,
            is_service_ok=True,
            item_left=None,
            min_date_item=datetime.date(2019, 3, 1),
            min_date_service=None,
            prod_id=product.id,
            service_left=13.00,
            surgery_amount_left=None,
            total_visits_left=None,
            total_antenatal_left=None,
            total_surgeries_left=None,
            total_admissions_left=None,
            total_deliveries_left=None,
            total_consultations_left=None,
        )
        # self.settings(), not `settings.ROW_SECURITY = False`: a bare
        # assignment is never undone, so it stayed False for every test the
        # process ran afterwards -- including the row security tests in claim,
        # which the manifest runs after policy and which then passed vacuously.
        with self.settings(ROW_SECURITY=False):
            native_response = native_el_svc.request(req, EligibilityResponse(req))
        self.assertIsNotNone(native_response)
        self.assertEqual(native_response, expected_resposnse)

    def test_eligibility_item(self):
        insuree, family = create_test_insuree_for_policy()
        product = create_test_product("ELI1")
        (policy, insuree_policy) = create_test_policy2(product, insuree)
        item = create_test_item("A")

        create_test_product_item(
            product, item, custom_props={"limit_no_adult": 12}
        )
        claim = create_test_claim(custom_props={"insuree_id": insuree.id})
        add_item_to_hf_pricelist(item, claim.health_facility)
        create_test_claimitem(
            claim, "A", custom_props={"item_id": item.id}, product=product
        )
        errors = processing_claim(claim, self.user, True)
        self.assertEqual(len(errors), 0)

        native_el_svc = NativeEligibilityService(self.user)
        req = EligibilityRequest(chf_id=insuree.chf_id, item_code=item.code)
        expected_resposnse = EligibilityResponse(
            antenatal_amount_left=None,
            consultation_amount_left=None,
            delivery_amount_left=None,
            eligibility_request=req,
            final=False,
            hospitalization_amount_left=None,
            is_item_ok=True,
            is_service_ok=True,
            item_left=5.00,
            min_date_item=datetime.date(2019, 3, 1),
            min_date_service=None,
            prod_id=product.id,
            service_left=None,
            surgery_amount_left=None,
            total_visits_left=None,
            total_antenatal_left=None,
            total_surgeries_left=None,
            total_admissions_left=None,
            total_deliveries_left=None,
            total_consultations_left=None,
        )
        with self.settings(ROW_SECURITY=False):
            native_response = EligibilityResponse(req)
            native_response = native_el_svc.request(req, native_response)
        self.assertIsNotNone(native_response)
        self.assertEqual(native_response, expected_resposnse)

    def test_eligibility_by_insuree(self):
        insuree, family = create_test_insuree_for_policy()
        product = create_test_product("ELI1")
        (policy, insuree_policy) = create_test_policy2(product, insuree)
        item = create_test_item("A")

        create_test_product_item(
            product, item, custom_props={"limit_no_adult": 12}
        )
        claim = create_test_claim(custom_props={"insuree_id": insuree.id})
        add_item_to_hf_pricelist(item, claim.health_facility)
        create_test_claimitem(
            claim, "A", custom_props={"item_id": item.id}, product=product
        )

        errors = processing_claim(claim, self.user, True)
        self.assertEqual(len(errors), 0)

        native_el_svc = NativeEligibilityService(self.user)
        req = EligibilityRequest(chf_id=insuree.chf_id, item_code=item.code)
        expected_resposnse = EligibilityResponse(
            antenatal_amount_left=None,
            consultation_amount_left=None,
            delivery_amount_left=None,
            eligibility_request=req,
            final=False,
            hospitalization_amount_left=None,
            is_item_ok=True,
            is_service_ok=True,
            item_left=5.00,
            min_date_item=datetime.date(2019, 3, 1),
            min_date_service=None,
            prod_id=product.id,
            service_left=None,
            surgery_amount_left=None,
            total_visits_left=None,
            total_antenatal_left=None,
            total_surgeries_left=None,
            total_admissions_left=None,
            total_deliveries_left=None,
            total_consultations_left=None,
        )
        with self.settings(ROW_SECURITY=False):
            native_response = EligibilityResponse(req)
            native_response = native_el_svc.request(req, native_response)
        self.assertIsNotNone(native_response)
        self.assertEqual(native_response, expected_resposnse)
        result = PolicyService(self.user).set_deleted(policy)
        self.assertNotEqual(
            result, [], "the policy cannot be deleted as it has some DedRem on it"
        )

    def test_eligibility_signal(self):

        insuree, family = create_test_insuree_for_policy()
        # spl = create_test_service_pricelist(location_id=family.location.parent.id)
        # ipl = create_test_item_pricelist(location_id=family.location.parent.id)
        # hf =create_test_health_facility(code= 'tst-18', location_id=family.location.parent.id,  custom_props={'id':18, 'items_pricelist': ipl, 'services_pricelist': spl })

        product = create_test_product("ELI1")
        (policy, insuree_policy) = create_test_policy2(product, insuree)
        item = create_test_item("A")

        create_test_product_item(
            product, item, custom_props={"limit_no_adult": 12}
        )
        claim = create_test_claim(
            custom_props={"insuree_id": insuree.id, "date_to": None}
        )
        add_item_to_hf_pricelist(item, claim.health_facility)
        create_test_claimitem(
            claim, "A", custom_props={"item_id": item.id}, product=product
        )
        errors = processing_claim(claim, self.user, True)
        self.assertEqual(len(errors), 0)

        def signal_before(sender, **kwargs):
            kwargs["response"].final = True
            kwargs["response"].total_admissions_left = 444719
            return kwargs["response"]

        signal_eligibility_service_before.connect(signal_before)

        el_svc = EligibilityService(self.user)
        req = EligibilityRequest(chf_id=insuree.chf_id, item_code=item.code)

        with self.settings(ROW_SECURITY=False):
            response = el_svc.request(req)
        self.assertIsNotNone(response)
        self.assertEqual(response.total_admissions_left, 444719)

        signal_eligibility_service_before.disconnect(signal_before)


class RenewalsTestCase(TestCase):
    item_1 = None

    def setUp(self) -> None:
        super(RenewalsTestCase, self).setUp()

        self.user = create_test_interactive_user(username='test_batch_run')

        self.item_1 = create_test_item("D")

    def test_insert_renewals(self):
        # Given
        from core import datetime, datetimedelta

        insuree, family = create_test_insuree_for_policy()
        product = create_test_product("VISIT")
        officer = create_test_officer()

        (policy_not_expiring, inspolicy_not_expiring) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={"expiry_date": "2099-01-01", "officer": officer},
        )
        (policy_expiring, inspolicy_expiring) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={
                "expiry_date": datetime.datetime.now() + datetimedelta(days=5),
                "officer": officer,
            },
        )

        # when
        insert_renewals(officer_id=officer.id)

        # then
        renewals = PolicyRenewal.objects.filter(insuree=insuree)
        expected_renewal = renewals.filter(policy=policy_expiring).first()
        self.assertIsNotNone(expected_renewal)

        should_not_renew = renewals.filter(policy=policy_not_expiring).first()
        self.assertIsNone(should_not_renew)

    def test_update_renewals(self):
        # Given
        from core import datetime, datetimedelta

        insuree, family = create_test_insuree_for_policy()
        product = create_test_product("VISIT")
        officer = create_test_officer()

        (policy_expiring, inspolicy_expiring) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={"expiry_date": "2019-01-01", "officer": officer},
        )
        (policy_not_expired_yet, inspolicy_not_expired_yet) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={
                "expiry_date": datetime.datetime.now() + datetimedelta(days=5),
                "officer": officer,
            },
        )

        # when
        update_renewals()

        # then
        policy_expiring.refresh_from_db()
        policy_not_expired_yet.refresh_from_db()

        self.assertEqual(policy_expiring.status, Policy.STATUS_EXPIRED)
        self.assertEqual(policy_not_expired_yet.status, Policy.STATUS_ACTIVE)

    def test_renewals_sms(self):
        # Given
        from core import datetime, datetimedelta

        insuree, family = create_test_insuree_for_policy(
            custom_props={
                "chf_id": "TESTCHFSMS",
                "last_name": "Test Last",
                "phone": "+33644444719",
            }
        )
        product = create_test_product("VISIT")
        officer = create_test_officer(
            custom_props={"phone": "+32444444444", "phone_communication": True},
            villages=[family.location],
        )

        (policy_expiring, _) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={"expiry_date": "2019-01-01", "officer": officer},
        )
        (policy_not_expired_yet, _) = create_test_policy2(
            product=product,
            insuree=insuree,
            custom_props={
                "expiry_date": datetime.datetime.now() + datetimedelta(days=5),
                "officer": officer,
            },
        )

        family_template = "FAMSMS;{{renewal.insuree.chf_id}};{{renewal.insuree.last_name}};{{renewal.new_product.name}}"

        insert_renewals(officer_id=officer.id)

        # when
        sms_queue = policy_renewal_sms(family_template)

        # then
        policy_expiring.refresh_from_db()
        policy_not_expired_yet.refresh_from_db()

        self.assertTrue(len(sms_queue) > 0)
        insuree_sms = [sms for sms in sms_queue if sms.phone == "+33644444719"]
        self.assertEqual(len(insuree_sms), 1)
        self.assertEqual(
            insuree_sms[0].sms_message,
            f"FAMSMS;{insuree.chf_id};Test Last;Test product VISIT",
        )

        officer_sms = [sms for sms in sms_queue if sms.phone == "+32444444444"]
        self.assertEqual(len(officer_sms), 1)
        self.assertIn(insuree.chf_id, officer_sms[0].sms_message)
        self.assertIn(family.location.name, officer_sms[0].sms_message)
        self.assertIn(family.location.parent.name, officer_sms[0].sms_message)
        self.assertIn(family.location.parent.parent.name, officer_sms[0].sms_message)
        self.assertIn("Test product VISIT", officer_sms[0].sms_message)

    def test_insert_renewal_details(self):
        # Given
        from core import datetime, datetimedelta

        insuree_newpic, family_newpic = create_test_insuree_for_policy(
            custom_props={
                "photo_date": datetime.datetime.now() - datetimedelta(days=30)
            }
        )
        insuree_oldpic, family_oldpic = create_test_insuree_for_policy(
            custom_props={
                "photo_date": "2010-01-01",
                "chf_id": "CHFMARK",
                "last_name": "Test Last",
            }
        )  # 5 years by default
        product = create_test_product("VISIT")
        officer = create_test_officer(
            custom_props={"phone": "+32444444444", "phone_communication": True}
        )
        create_test_photo(insuree_newpic.id, officer.id)
        create_test_photo(insuree_oldpic.id, officer.id)

        (policy_new_pic, inspolicy_new_pic) = create_test_policy2(
            product=product,
            insuree=insuree_newpic,
            custom_props={
                "expiry_date": datetime.datetime.now() + datetimedelta(days=5),
                "officer": officer,
            },
        )
        (policy_old_pic, inspolicy_old_pic) = create_test_policy2(
            product=product,
            insuree=insuree_oldpic,
            custom_props={
                "expiry_date": datetime.datetime.now() + datetimedelta(days=5),
                "officer": officer,
            },
        )

        # when
        insert_renewals(officer_id=officer.id)

        # then
        renewals_new = PolicyRenewal.objects.filter(insuree=insuree_newpic)
        expected_renewal = renewals_new.filter(policy=policy_new_pic).first()
        self.assertIsNotNone(expected_renewal)
        self.assertIsNone(expected_renewal.details.first())

        renewals_old = PolicyRenewal.objects.filter(insuree=insuree_oldpic)
        expected_renewal_old = renewals_old.filter(policy=policy_old_pic).first()
        self.assertIsNotNone(expected_renewal_old)
        detail = expected_renewal_old.details.first()
        self.assertIsNotNone(detail)

        # ALSO WHEN
        sms_queue = policy_renewal_sms("UNUSED")  # Uses the default template
        self.assertEqual(len(sms_queue), 2)
        old_sms = [
            sms.sms_message
            for sms in sms_queue
            if insuree_oldpic.chf_id in sms.sms_message
        ]
        self.assertEqual(len(old_sms), 1)
        self.assertTrue(
            f"HOF\n{insuree_oldpic.chf_id}\nTest Last First Second\n\n" in old_sms[0]
        )
