import logging
from dataclasses import dataclass
from datetime import datetime as py_datetime, date as py_date
from django.core.cache import caches

import core
from claim.models import Claim, ClaimItem
from django import dispatch
from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
    MultipleObjectsReturned,
)
from django.db.models import Q, Count, Min, Max, Sum, F
from django.db.models.functions import Coalesce
from django.template import Template, Context
from django.utils.translation import gettext as _
from graphene.utils.str_converters import to_snake_case

from core.signals import register_service_signal
from insuree.models import Insuree, InsureePolicy
from insuree.services import create_insuree_renewal_detail
from medical.models import Service, Item
from policy.apps import PolicyConfig
from policy.utils import MonthsAdd, get_members

from .models import Policy, PolicyRenewal

logger = logging.getLogger(__name__)

cache = caches['coverage']


def reset_policy_before_update(policy):
    policy.enroll_date = None
    policy.start_date = None
    policy.expiry_date = None
    policy.value = None
    policy.product_id = None
    policy.family_id = None
    policy.officer_id = None


class PolicyRenewalService:
    def __init__(self, user):
        self.user = user

    def delete(self, policy_renewal):
        try:
            policy_renewal.delete_history()
            logger.info("Deleting the related policy renewal details, if any")
            for detail in policy_renewal.details.all():
                detail.delete_history()
            return []
        except Exception as exc:
            logger.error(f'ERROR {exc}')
            return {
                'title': policy_renewal.uuid,
                'list': [{
                    'message': _("policy_renewal.mutation.failed_to_delete_policy_renewal") % {'policy_renewal': str(policy_renewal)},
                    'detail': policy_renewal.uuid}]
            }


class PolicyService:
    def __init__(self, user):
        self.user = user

    @register_service_signal("policy_service.create_or_update")
    def update_or_create(self, data, user):
        if isinstance(data["enroll_date"], str):
            data["enroll_date"] = py_datetime.strptime(
                data["enroll_date"], "%Y-%m-%d"
            ).date()
        policy_uuid = data.get("uuid", None)
        if "enroll_date" in data and data["enroll_date"] > py_date.today():
            raise ValidationError("policy.enroll_date_in_the_future")
        if policy_uuid:
            return self.update_policy(data, user=user)
        else:
            return self.create_policy(data, user=user)

    @register_service_signal("policy_service.update")
    def update_policy(self, data, user):
        if "is_paid" in data:
            data.pop("is_paid")
        members = None
        if "members_uuid" in data:
            members_uuid = data.pop("members_uuid", None)
            members = (
                Insuree.objects.filter(uuid__in=members_uuid) if members_uuid else None
            )
        data = self._clean_mutation_info(data)
        policy_uuid = data.pop("uuid") if "uuid" in data else None
        policy = Policy.objects.get(uuid=policy_uuid)
        members = get_members(policy, policy.family, user, members)
        policy.save_history()
        reset_policy_before_update(policy)
        [setattr(policy, key, data[key]) for key in data]
        policy.save()
        update_insuree_policies(policy, user=user, members=members)
        return policy

    @register_service_signal("policy_service.create")
    def create_policy(self, data, user):
        is_paid = data.pop("is_paid", False)
        receipt = data.pop("receipt", None)
        members = None
        if "members_uuid" in data:
            members_uuid = data.pop("members_uuid", None)
            members = (
                Insuree.objects.filter(uuid__in=members_uuid) if members_uuid else None
            )
        payer_uuid = data.pop("payer_uuid", None)
        data = self._clean_mutation_info(data)
        policy = Policy.objects.create(**data)
        if receipt is not None:
            from contribution.services import (
                check_unique_premium_receipt_code_within_product,
            )

            is_invalid = check_unique_premium_receipt_code_within_product(
                code=receipt, policy_uuid=policy.uuid
            )
            if is_invalid:
                raise ValidationError("Receipt already exist for a given product.")
        else:
            receipt = self.generate_contribution_receipt(
                policy.product, policy.enroll_date
            )
        policy.save()
        update_insuree_policies(policy, user, members=members)
        if is_paid:
            from contribution.gql_mutations import premium_action

            premium_data = {
                "policy_uuid": policy.uuid,
                "amount": policy.value,
                "receipt": receipt,
                "pay_date": data["enroll_date"],
                "pay_type": "C",
            }
            if payer_uuid is not None:
                premium_data["payer_uuid"] = payer_uuid
            premium_action(premium_data, user)
        return policy

    def generate_contribution_receipt(self, product, enroll_date):
        from contribution.models import Premium

        code_length = PolicyConfig.contribution_receipt_length
        if not code_length and type(code_length) is not int:
            raise ValueError(
                "Invalid config for `generate_contribution_receipt`, expected `code_length` value."
            )
        prefix = "RE-" + str(product.code) + "-" + str(enroll_date) + "-"
        last_contribution = Premium.objects.filter(
            receipt__icontains=prefix, *core.filter_validity()
        )
        code = 0
        if last_contribution:
            code = int(last_contribution.latest("receipt").receipt[-code_length:])
        return prefix + str(code + 1).zfill(code_length)

    def _clean_mutation_info(self, data):
        if "client_mutation_id" in data:
            data.pop("client_mutation_id")
        if "client_mutation_label" in data:
            data.pop("client_mutation_label")
        return data

    def set_suspended(self, user, policy):
        try:
            policy.save_history()
            policy.status = Policy.STATUS_SUSPENDED
            policy.audit_user_id = user.id_for_audit
            policy.save()
            return []
        except Exception:
            return {
                "title": policy.uuid,
                "list": [
                    {
                        "message": _("policy.mutation.failed_to_suspend_policy")
                        % {"uuid": policy.uuid},
                        "detail": policy.uuid,
                    }
                ],
            }

    def set_deleted(self, policy):

        if policy.claim_ded_rems:
            return {
                "title": policy.uuid,
                "list": [
                    {
                        "message": _("policy.mutation.policy_is_used_in_claims")
                        % {"policy": str(policy)},
                        "detail": policy.uuid,
                    }
                ],
            }
        try:
            insuree_policies = InsureePolicy.objects.filter(policy=policy)
            for insuree_policy in insuree_policies:
                insuree_policy.delete_history()
            policy.delete_history()
            return []
        except Exception:
            return {
                "title": policy.uuid,
                "list": [
                    {
                        "message": _(
                            "policy.mutation.failed_to_change_status_of_policy"
                        )
                        % {"policy": str(policy)},
                        "detail": policy.uuid,
                    }
                ],
            }


@core.comparable
class ByInsureeRequest(object):

    def __init__(
        self,
        chf_id,
        active_or_last_expired_only=False,
        show_history=False,
        order_by=None,
        target_date=None,
    ):
        self.chf_id = chf_id
        self.active_or_last_expired_only = active_or_last_expired_only
        self.show_history = show_history
        self.order_by = order_by
        self.target_date = target_date

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


@core.comparable
class ByFamilyOrInsureeResponseItem(object):

    def __init__(
        self,
        policy_id,
        policy_uuid,
        policy_value,
        product_code,
        product_name,
        start_date,
        enroll_date,
        effective_date,
        expiry_date,
        officer_code,
        officer_name,
        status,
        ded,
        ded_in_patient,
        ded_out_patient,
        ceiling,
        ceiling_in_patient,
        ceiling_out_patient,
        balance,
        validity_from,
        validity_to,
        max_installments,
        contribution_plan_code=None,
        contribution_plan_name=None,
    ):
        self.policy_id = policy_id
        self.policy_uuid = policy_uuid
        self.policy_value = policy_value
        self.product_code = product_code
        self.product_name = product_name
        self.start_date = start_date
        self.enroll_date = enroll_date
        self.effective_date = effective_date
        self.expiry_date = expiry_date
        self.officer_code = officer_code
        self.officer_name = officer_name
        self.status = status
        self.ded = ded
        self.ded_in_patient = ded_in_patient
        self.ded_out_patient = ded_out_patient
        self.ceiling = ceiling
        self.ceiling_in_patient = ceiling_in_patient
        self.ceiling_out_patient = ceiling_out_patient
        self.balance = balance
        self.validity_from = validity_from
        self.validity_to = validity_to
        self.max_installments = max_installments
        self.contribution_plan_code = contribution_plan_code
        self.contribution_plan_name = contribution_plan_name

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


@core.comparable
class ByInsureeResponse(object):

    def __init__(self, by_insuree_request, items):
        self.by_insuree_request = by_insuree_request
        self.items = items

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


class FilteredPoliciesService(object):
    def __init__(self, user):
        self.user = user

    @staticmethod
    def _to_item(row):
        ceiling = None
        ceiling_ip = None
        ceiling_op = None
        if row.product.max_treatment:
            ceiling = row.product.max_treatment
        if row.product.max_ip_treatment:
            ceiling_ip = row.product.max_ip_treatment
        if row.product.max_op_treatment:
            ceiling_op = row.product.max_ip_treatment
        if row.product.max_insuree:
            ceiling = row.product.max_insuree - (
                row.total_rem_g if row.total_rem_g else 0
            )
        else:
            if row.product.max_ip_insuree:
                ceiling_ip = row.product.max_ip_insuree - (
                    row.total_rem_ip if row.total_rem_ip else 0
                )
            if row.product.max_op_insuree:
                ceiling_op = row.product.max_op_insuree - (
                    row.total_rem_op if row.total_rem_op else 0
                )

        members_count = row.family.members.count()
        threshold = row.product.threshold if row.product.threshold else 0
        total_rem_g = row.total_rem_g if row.total_rem_g else 0
        total_rem_ip = row.total_rem_ip if row.total_rem_ip else 0
        total_rem_op = row.total_rem_op if row.total_rem_op else 0
        extra_member = (
            row.product.max_policy_extra_member
            if row.product.max_policy_extra_member
            else 0
        )
        extra_member_ip = (
            row.product.max_policy_extra_member_ip
            if row.product.max_policy_extra_member_ip
            else 0
        )
        extra_member_op = (
            row.product.max_policy_extra_member_op
            if row.product.max_policy_extra_member_op
            else 0
        )

        if row.product.max_policy:
            max_policy = row.product.max_policy
            if members_count > threshold:
                max_policy += (members_count - threshold) * extra_member
            ceiling = max_policy - total_rem_g
        else:
            ceiling_ip = 0
            if row.product.max_ip_policy:
                max_ip_policy = row.product.max_ip_policy
                if members_count > threshold:
                    max_ip_policy += (members_count - threshold) * extra_member_ip
                ceiling_ip = max_ip_policy - total_rem_ip

            ceiling_op = 0
            if row.product.max_op_policy:
                max_op_policy = row.product.max_op_policy
                if members_count > threshold:
                    max_op_policy += (members_count - threshold) * extra_member_op
                ceiling_op = max_op_policy - total_rem_op

        balance = row.value
        if row.total_ded_g:
            balance -= row.total_ded_g

        contribution_plan_code = (
            row.contribution_plan.code if row.contribution_plan else None
        )
        contribution_plan_name = (
            row.contribution_plan.name if row.contribution_plan else None
        )
        return ByFamilyOrInsureeResponseItem(
            policy_id=row.id,
            policy_uuid=row.uuid,
            policy_value=row.value,
            product_code=row.product.code,
            product_name=row.product.name,
            start_date=row.start_date,
            enroll_date=row.enroll_date,
            effective_date=row.effective_date,
            expiry_date=row.expiry_date,
            officer_code=row.officer.code if row.officer else None,
            officer_name=row.officer.name() if row.officer else None,
            status=row.status,
            ded=row.total_ded_g,
            ded_in_patient=row.total_ded_ip,
            ded_out_patient=row.total_ded_op,
            ceiling=ceiling,
            ceiling_in_patient=ceiling_ip,
            ceiling_out_patient=ceiling_op,
            balance=balance,
            validity_from=row.validity_from,
            validity_to=row.validity_to,
            max_installments=row.product.max_installments,
            contribution_plan_code=contribution_plan_code,
            contribution_plan_name=contribution_plan_name,
        )

    @staticmethod
    def _filter_active_or_last_expired_only(policies):
        selected = {}
        waiting_statuses = {Policy.STATUS_IDLE, Policy.STATUS_READY}
        single_pick_statuses = {
            Policy.STATUS_ACTIVE,
            Policy.STATUS_SUSPENDED,
            Policy.STATUS_EXPIRED,
        }

        for policy in policies:
            # Keep all not-yet-active policies.
            if policy.status in waiting_statuses:
                selected["waiting-%s" % policy.uuid] = policy
                continue

            # Keep only the latest one per product and status type.
            if policy.status in single_pick_statuses:
                key = "product-%s-status-%s" % (policy.product.code, policy.status)
                if key not in selected:
                    selected[key] = policy

        return selected.values()

    def build_query(self, req):
        # TODO: prevent direct dependency on claim_ded structure?
        res = (
            Policy.objects.prefetch_related("product")
            .prefetch_related("officer")
            .annotate(total_ded_g=Sum("claim_ded_rems__ded_g"))
            .annotate(total_ded_ip=Sum("claim_ded_rems__ded_ip"))
            .annotate(total_ded_op=Sum("claim_ded_rems__ded_op"))
            .annotate(total_rem_g=Sum("claim_ded_rems__rem_g"))
            .annotate(total_rem_op=Sum("claim_ded_rems__rem_op"))
            .annotate(total_rem_ip=Sum("claim_ded_rems__rem_ip"))
            .annotate(total_rem_consult=Sum("claim_ded_rems__rem_consult"))
            .annotate(total_rem_surgery=Sum("claim_ded_rems__rem_surgery"))
            .annotate(total_rem_delivery=Sum("claim_ded_rems__rem_delivery"))
            .annotate(
                total_rem_hospitalization=Sum("claim_ded_rems__rem_hospitalization")
            )
            .annotate(total_rem_antenatal=Sum("claim_ded_rems__rem_antenatal"))
        )
        res.query.group_by = ["id"]
        if hasattr(req, "chf_id"):
            res = res.filter(insuree_policies__insuree__chf_id=req.chf_id)
        elif hasattr(req, "insuree_uuid"):
            res = res.filter(insuree_policies__insuree__uuid=req.insuree_uuid)
        if not req.show_history:
            if req.target_date:
                res = res.filter(
                    *core.filter_validity(),
                    expiry_date__gt=req.target_date,
                    effective_date__lte=req.target_date,
                )
            else:
                res = res.filter(*core.filter_validity())
        if req.active_or_last_expired_only:
            # Keep deterministic ordering before python-side filtering.
            res = (
                res.annotate(not_null_expiry_date=Coalesce("expiry_date", py_date.max))
                .annotate(not_null_validity_to=Coalesce("validity_to", py_datetime.max))
                .order_by(
                    "product__code",
                    "-not_null_expiry_date",
                    "-not_null_validity_to",
                    "-validity_from",
                    "status",
                )
            )
        return res


class ByInsureeService(FilteredPoliciesService):
    def __init__(self, user):
        super(ByInsureeService, self).__init__(user)

    def request(self, by_insuree_request):
        res = self.build_query(by_insuree_request)
        res = res.filter(insuree_policies__insuree__chf_id=by_insuree_request.chf_id)
        if by_insuree_request.active_or_last_expired_only:
            res = self._filter_active_or_last_expired_only(res)
        items = [FilteredPoliciesService._to_item(x) for x in res]
        # possible improvement: sort via the ORM
        # ... but beware of the active_or_last_expired_only filtering!
        order_attr = to_snake_case(
            by_insuree_request.order_by
            if by_insuree_request.order_by
            else "expiry_date"
        )
        desc = False
        if order_attr.startswith("-"):
            order_attr = order_attr[1:]
            desc = True
        items = sorted(items, key=lambda x: getattr(x, order_attr), reverse=desc)
        return ByInsureeResponse(by_insuree_request=by_insuree_request, items=items)


@core.comparable
class ByFamilyRequest(object):

    def __init__(
        self,
        family_uuid,
        active_or_last_expired_only=False,
        show_history=False,
        order_by=None,
        target_date=None,
    ):
        self.family_uuid = family_uuid
        self.active_or_last_expired_only = active_or_last_expired_only
        self.show_history = show_history
        self.order_by = order_by
        self.target_date = target_date

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


@core.comparable
class ByFamilyResponse(object):

    def __init__(self, by_family_request, items):
        self.by_family_request = by_family_request
        self.items = items

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


class ByFamilyService(FilteredPoliciesService):
    def __init__(self, user):
        super(ByFamilyService, self).__init__(user)

    def request(self, by_family_request):
        res = self.build_query(by_family_request)
        res = res.filter(family__uuid=by_family_request.family_uuid)
        # .distinct('product__code') >> DISTINCT ON fields not supported by MS-SQL
        if by_family_request.active_or_last_expired_only:
            res = self._filter_active_or_last_expired_only(res)
        items = tuple(map(lambda x: FilteredPoliciesService._to_item(x), res))
        return ByFamilyResponse(by_family_request=by_family_request, items=items)


@core.comparable
class ByPolicyRequest(object):

    def __init__(
        self,
        policy_uuid,
        chf_id=None,
        insuree_uuid=None,
        active_or_last_expired_only=False,
        show_history=False,
        order_by=None,
        target_date=None,
    ):
        if chf_id is None and insuree_uuid is None:
            raise ValueError(_("policy.service.eligibility.insuree_id_or_uuid_missing"))
        self.policy_uuid = policy_uuid
        self.chf_id = chf_id
        self.insuree_uuid = insuree_uuid
        self.active_or_last_expired_only = active_or_last_expired_only
        self.show_history = show_history
        self.order_by = order_by
        self.target_date = target_date

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


@core.comparable
class ByPolicyResponse(object):

    def __init__(self, by_policy_request, items):
        self.by_policy_request = by_policy_request
        self.items = items

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


class ByPolicyService(FilteredPoliciesService):
    def __init__(self, user):
        super(ByPolicyService, self).__init__(user)

    def request(self, by_policy_request):
        res = self.build_query(by_policy_request)
        res = res.filter(uuid=by_policy_request.policy_uuid)
        # .distinct('product__code') >> DISTINCT ON fields not supported by MS-SQL
        if by_policy_request.active_or_last_expired_only:
            res = self._filter_active_or_last_expired_only(res)
        items = tuple(map(lambda x: FilteredPoliciesService._to_item(x), res))
        return ByPolicyResponse(by_policy_request=by_policy_request, items=items)


# --- ELIGIBILITY --
# TODO: should become "BY FAMILY":
# Eligibility is calculated from a Policy
# ... which is bound to a Family (same remark as ByInsureeService)
# -------------------
@core.comparable
class EligibilityRequest(object):

    def __init__(self, chf_id=None, service_code=None, item_code=None,
                 policy_uuid=None, insuree_uuid=None):
        if chf_id is None and insuree_uuid is None:
            raise ValueError(_("policy.service.eligibility.insuree_id_or_uuid_missing"))
        self.chf_id = chf_id
        self.service_code = service_code
        self.item_code = item_code
        self.policy_uuid = policy_uuid
        self.insuree_uuid = insuree_uuid

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__


class EligibilityResponse(object):

    def __init__(
        self,
        eligibility_request,
        prod_id=None,
        total_admissions_left=0,
        total_visits_left=0,
        total_consultations_left=0,
        total_surgeries_left=0,
        total_deliveries_left=0,
        total_antenatal_left=0,
        consultation_amount_left=0,
        surgery_amount_left=0,
        delivery_amount_left=0,
        hospitalization_amount_left=0,
        antenatal_amount_left=0,
        min_date_service=None,
        min_date_item=None,
        service_left=0,
        item_left=0,
        is_item_ok=False,
        is_service_ok=False,
        final=False,
    ):
        self.eligibility_request = eligibility_request
        self.prod_id = prod_id
        self.total_admissions_left = total_admissions_left
        self.total_visits_left = total_visits_left
        self.total_consultations_left = total_consultations_left
        self.total_surgeries_left = total_surgeries_left
        self.total_deliveries_left = total_deliveries_left
        self.total_antenatal_left = total_antenatal_left
        self.consultation_amount_left = consultation_amount_left
        self.surgery_amount_left = surgery_amount_left
        self.delivery_amount_left = delivery_amount_left
        self.hospitalization_amount_left = hospitalization_amount_left
        self.antenatal_amount_left = antenatal_amount_left
        self.min_date_service = min_date_service
        self.min_date_item = min_date_item
        self.service_left = service_left
        self.item_left = item_left
        self.is_item_ok = is_item_ok
        self.is_service_ok = is_service_ok
        self.final = final

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False

        def str_none(x):
            str(x) if x else x

        # Comparison should take into account the date vs AdDate
        return (
            self.eligibility_request == other.eligibility_request
            and self.prod_id == other.prod_id
            and self.total_admissions_left == other.total_admissions_left
            and self.total_visits_left == other.total_visits_left
            and self.total_consultations_left == other.total_consultations_left
            and self.total_surgeries_left == other.total_surgeries_left
            and self.total_deliveries_left == other.total_deliveries_left
            and self.total_antenatal_left == other.total_antenatal_left
            and self.consultation_amount_left == other.consultation_amount_left
            and self.surgery_amount_left == other.surgery_amount_left
            and self.delivery_amount_left == other.delivery_amount_left
            and self.hospitalization_amount_left == other.hospitalization_amount_left
            and self.antenatal_amount_left == other.antenatal_amount_left
            and str_none(self.min_date_service) == str_none(other.min_date_service)
            and str_none(self.min_date_item) == str_none(other.min_date_item)
            and self.service_left == other.service_left
            and self.item_left == other.item_left
            and self.is_item_ok == other.is_item_ok
            and self.is_service_ok == other.is_service_ok
        )

    def __str__(self):
        return (
            f"Eligibility for {self.eligibility_request} gave product {self.prod_id} "
            f"with item/svc ok {self.is_item_ok}/{self.is_service_ok} "
            f" left: {self.item_left}/{self.service_left}"
        )

    def __repr__(self):
        return self.__str__()


signal_eligibility_service_before = dispatch.Signal(["user", "request", "response"])
signal_eligibility_service_after = dispatch.Signal(["user", "request", "response"])


class EligibilityService(object):
    def __init__(self, user):
        self.user = user
        self.service = NativeEligibilityService(user)

    def request(self, request):
        if not self.user or not self.user.has_perms(
            PolicyConfig.gql_query_eligibilities_perms
        ):
            raise PermissionDenied()

        # The response is passed along in signals and functions. Setting the final parameter will stop
        response = EligibilityResponse(eligibility_request=request)
        responses = signal_eligibility_service_before.send(
            self.__class__, user=self.user, request=request, response=response
        )
        response = EligibilityService._get_final_response(responses, "before", response)

        if not response.final and not PolicyConfig.default_eligibility_disabled:
            response = self.service.request(request, response)

        if not response.final:
            responses = signal_eligibility_service_after.send(
                self.__class__, user=self.user, request=request, response=response
            )
            response = EligibilityService._get_final_response(
                responses, "after", response
            )

        return response

    @classmethod
    def _get_final_response(cls, responses, sig_name, default_response):
        if responses is None or len(responses) == 0:
            return default_response
        final_responses = [r for f, r in responses if r.final]
        if len(final_responses) > 0:
            if len(final_responses) > 1:
                logger.warning(
                    "Eligibility service got more than one final *%s* signal response: %s",
                    sig_name,
                    [f for f, r in responses if r.final],
                )
            return final_responses[0]
        else:
            return responses[-1]


class NativeEligibilityService(object):
    def __init__(self, user):
        self.user = user

    def get_eligibility(self, insuree, item_or_service, model, req, now):
        if insuree.is_adult():
            waiting_period_field = (
                f"policy__product__{item_or_service}s__waiting_period_adult"
            )
            limit_field = f"policy__product__{item_or_service}s__limit_no_adult"
        else:
            waiting_period_field = (
                f"policy__product__{item_or_service}s__waiting_period_child"
            )
            limit_field = f"policy__product__{item_or_service}s__limit_no_child"

        item_or_service_code = req.service_code
        if item_or_service == "item":
            item_or_service_code = req.item_code

        # try to get the service/item with the exact code
        try:
            item_or_service_obj = model.get_queryset(None, self.user).get(
                code=item_or_service_code
            )
        except model.DoesNotExist:
            item_or_service_obj = model.get_queryset(None, self.user).filter(
                code__iexact=item_or_service_code
            ).first()
            if item_or_service_obj is None:
                raise model.DoesNotExist(f"{model.__name__} has no match for code {item_or_service_code}")
        except MultipleObjectsReturned:
            raise MultipleObjectsReturned(f"{model.__name__} has multiple match for code {item_or_service_code}")
        # Beware that MonthAdd() is in Gregorian calendar, not Nepalese or anything else
        queryset_item_or_service = (
            InsureePolicy.objects.filter(
                policy__status=Policy.STATUS_ACTIVE,
                insuree=insuree,
                *core.filter_validity(prefix=""),
                *core.filter_validity(prefix="policy__"),
                *core.filter_validity(prefix=f"policy__product__{item_or_service}s__"),
                *core.filter_validity(prefix="policy__"),
                **{
                    f"policy__product__{item_or_service}s__{item_or_service}_id": item_or_service_obj.id
                },
            )
            .filter(
                Q(
                    *core.filter_validity(prefix="insuree__claim__"),
                    **{
                        f"insuree__claim__{item_or_service}s__{item_or_service}_id": item_or_service_obj.id
                    },
                )
                & Q(
                    *core.filter_validity(
                        prefix=f"insuree__claim__{item_or_service}s__"
                    )
                )
                & (
                    Q(
                        **{
                            f"insuree__claim__{item_or_service}s__status": ClaimItem.STATUS_PASSED
                        }
                    )
                    | Q(**{f"insuree__claim__{item_or_service}s__status__isnull": True})
                )
                & (
                    Q(insuree__claim__status__gt=Claim.STATUS_ENTERED)
                    | Q(insuree__claim__status__isnull=True)
                )
            )
            .values(
                "effective_date",
                "policy__product_id",
                waiting_period=F(waiting_period_field),
                limit_no=F(limit_field),
            )
            .annotate(
                min_date=MonthsAdd(
                    "effective_date", Coalesce(F(waiting_period_field), 0)
                )
            )
            .annotate(
                count=Sum(
                    Coalesce(
                        f"insuree__claim__{item_or_service}s__qty_approved",
                        f"insuree__claim__{item_or_service}s__qty_provided",
                    )
                )
            )
            .annotate(left=F("limit_no") - F("count"))
        )

        min_date_qs = queryset_item_or_service.aggregate(
            min_date_lte=Min("min_date", filter=Q(min_date__lte=now)),
            min_date_all=Min("min_date"),
        )
        min_date_item = core.datetime.date.from_ad_date(
            min_date_qs["min_date_lte"]
            if min_date_qs["min_date_lte"]
            else min_date_qs["min_date_all"]
        )

        if (
            queryset_item_or_service.filter(min_date__lte=now)
            .filter(left__isnull=True)
            .order_by("-validity_from")
            .first()
        ):
            items_or_services_left = None
        else:
            items_or_services_left = queryset_item_or_service.filter(
                Q(min_date__isnull=True) | Q(min_date__lte=now)
            ).aggregate(Max("left"))["left__max"]

        return item_or_service_obj, min_date_item, items_or_services_left

    def request(self, req, response):
        def get_total_filter(category):
            return Q(
                insuree__claim__status__gt=Claim.STATUS_ENTERED,
                insuree__claim__category=category,
                *core.filter_validity(prefix="insuree__"),
                *core.filter_validity(prefix="insuree__claim__"),
                *core.filter_validity(prefix="insuree__claim__services__"),
            ) & (  # Not sure this one is necessary
                Q(insuree__claim__services__rejection_reason=0)
                | Q(insuree__claim__services__rejection_reason__isnull=True)
            )
        insuree_filter = {'chf_id': req.chf_id} if req.chf_id is not None else {'uuid': req.insuree_uuid}
        policy_filter = {'policy__uuid': req.policy_uuid} if req.policy_uuid is not None else {}
        insuree = Insuree.get_queryset(None, self.user).filter(**insuree_filter).get(
            *core.filter_validity()
        )  # Will throw an exception if not found
        now = core.datetime.datetime.now()
        eligibility = response

        if req.service_code:
            service, min_date_service, services_left = self.get_eligibility(
                insuree, "service", Service, req, now
            )
        else:
            service = None
            services_left = None
            min_date_service = None
        eligibility.min_date_service = min_date_service
        eligibility.service_left = services_left

        if req.item_code:
            item, min_date_item, items_left = self.get_eligibility(
                insuree, "item", Item, req, now
            )
        else:
            items_left = None
            min_date_item = None
        eligibility.min_date_item = min_date_item
        eligibility.item_left = items_left

        # InsPol -> Policy -> Product -> dedrem
        cached_data = cache.get(
            f"eligibility_{insuree.family_id or insuree.id}"
        )
        if cached_data and str(insuree.id) in cached_data:
            result = cached_data[str(insuree.id)]
        else:
            result = (
                InsureePolicy.objects.filter(
                    insuree=insuree,
                    *core.filter_validity(prefix="policy__product__"),
                    *core.filter_validity(prefix="policy__"),
                    **policy_filter,
                )
                .values(
                    "policy__product_id",
                    "policy__product__max_no_surgery",
                    "policy__product__max_amount_surgery",
                    "policy__product__max_amount_consultation",
                    "policy__product__max_amount_surgery",
                    "policy__product__max_amount_delivery",
                    "policy__product__max_amount_antenatal",
                    "policy__product__max_amount_hospitalization",
                )
                .annotate(
                    total_admissions=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_HOSPITALIZATION),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_admissions_left=F("policy__product__max_no_hospitalization")
                    - F("total_admissions")
                )
                .annotate(
                    total_consultations=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_CONSULTATION),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_consultations_left=F("policy__product__max_no_consultation")
                    - F("total_consultations")
                )
                .annotate(
                    total_surgeries=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_SURGERY),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_surgeries_left=F("policy__product__max_no_surgery")
                    - F("total_surgeries")
                )
                .annotate(
                    total_deliveries=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_DELIVERY),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_deliveries_left=F("policy__product__max_no_delivery")
                    - F("total_deliveries")
                )
                .annotate(
                    total_antenatal=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_ANTENATAL),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_antenatal_left=F("policy__product__max_no_antenatal")
                    - F("total_antenatal")
                )
                .annotate(
                    total_visits=Coalesce(
                        Count(
                            "insuree__claim",
                            filter=get_total_filter(Service.CATEGORY_VISIT),
                            distinct=True,
                        ),
                        0,
                    )
                )
                .annotate(
                    total_visits_left=F("policy__product__max_no_visits")
                    - F("total_visits")
                )
                .order_by("-expiry_date")
                .first()
            )
            if not cached_data:
                cached_data = {}
                cached_data[str(insuree.id)] = result
            cache.set(
                f"eligibility_{insuree.family_id or insuree.id}",
                result,
                None,
            )

        if result is None:
            eligibility.total_admissions_left = 0
            eligibility.total_consultations_left = 0
            eligibility.total_surgeries_left = 0
            eligibility.total_deliveries_left = 0
            eligibility.total_antenatal_left = 0
            eligibility.total_visits_left = 0
            eligibility.surgery_amount_left = 0
            eligibility.consultation_amount_left = 0
            eligibility.delivery_amount_left = 0
            eligibility.antenatal_amount_left = 0
            eligibility.hospitalization_amount_left = 0
            eligibility.is_item_ok = False
            eligibility.is_service_ok = False
            return eligibility

        eligibility.prod_id = result["policy__product_id"]
        total_admissions_left = (
            result["total_admissions_left"]
            if result["total_admissions_left"] is None
            or result["total_admissions_left"] >= 0
            else 0
        )
        total_consultations_left = (
            result["total_consultations_left"]
            if result["total_consultations_left"] is None
            or result["total_consultations_left"] >= 0
            else 0
        )
        total_surgeries_left = (
            result["total_surgeries_left"]
            if result["total_surgeries_left"] is None
            or result["total_surgeries_left"] >= 0
            else 0
        )
        total_deliveries_left = (
            result["total_deliveries_left"]
            if result["total_deliveries_left"] is None
            or result["total_deliveries_left"] >= 0
            else 0
        )
        total_antenatal_left = (
            result["total_antenatal_left"]
            if result["total_antenatal_left"] is None
            or result["total_antenatal_left"] >= 0
            else 0
        )
        total_visits_left = (
            result["total_visits_left"]
            if result["total_visits_left"] is None or result["total_visits_left"] >= 0
            else 0
        )

        eligibility.surgery_amount_left = result["policy__product__max_amount_surgery"]
        eligibility.consultation_amount_left = result[
            "policy__product__max_amount_consultation"
        ]
        eligibility.delivery_amount_left = result[
            "policy__product__max_amount_delivery"
        ]
        eligibility.antenatal_amount_left = result[
            "policy__product__max_amount_antenatal"
        ]
        eligibility.hospitalization_amount_left = result[
            "policy__product__max_amount_hospitalization"
        ]

        if service:
            if service.category == Service.CATEGORY_SURGERY:
                if (
                    total_surgeries_left == 0
                    or services_left == 0
                    or (min_date_service and min_date_service > now)
                    or (
                        result["policy__product__max_amount_surgery"] is not None
                        and result["policy__product__max_amount_surgery"] <= 0
                    )
                ):
                    eligibility.is_service_ok = False
                else:
                    eligibility.is_service_ok = True
            elif service.category == Service.CATEGORY_CONSULTATION:
                if (
                    total_consultations_left == 0
                    or services_left == 0
                    or (min_date_service and min_date_service > now)
                    or (
                        result["policy__product__max_amount_consultation"] is not None
                        and result["policy__product__max_amount_consultation"] <= 0
                    )
                ):
                    eligibility.is_service_ok = False
                else:
                    eligibility.is_service_ok = True
            elif service.category == Service.CATEGORY_DELIVERY:
                if (
                    total_deliveries_left == 0
                    or services_left == 0
                    or (min_date_service and min_date_service > now)
                    or (
                        result["policy__product__max_amount_delivery"] is not None
                        and result["policy__product__max_amount_delivery"] <= 0
                    )
                ):
                    eligibility.is_service_ok = False
                else:
                    eligibility.is_service_ok = True
            # Original code had a Service.CATEGORY_OTHER but with the same process as the else
            else:
                if services_left == 0 or (min_date_service and min_date_service > now):
                    eligibility.is_service_ok = False
                else:
                    eligibility.is_service_ok = True
        else:
            # It is a bit weird to return is_service_ok=True when there is no service but that's how it used to work
            eligibility.is_service_ok = True

        if items_left == 0 or (min_date_item and min_date_item > now):
            eligibility.is_item_ok = False
        else:
            eligibility.is_item_ok = True

        # The process above uses the None type but the stored procedure service sets these to 0
        eligibility.total_admissions_left = total_admissions_left
        eligibility.total_consultations_left = total_consultations_left
        eligibility.total_surgeries_left = total_surgeries_left
        eligibility.total_deliveries_left = total_deliveries_left
        eligibility.total_antenatal_left = total_antenatal_left
        eligibility.total_visits_left = total_visits_left
        return eligibility


def insert_renewals(
    date_from=None,
    date_to=None,
    officer_id=None,
    reminding_interval=None,
    location_id=None,
    location_levels=4,
):
    if reminding_interval is None:
        reminding_interval = PolicyConfig.policy_renewal_interval
    now = core.datetime.datetime.now()
    policies = Policy.objects.filter(
        status__in=[Policy.STATUS_EXPIRED, Policy.STATUS_ACTIVE],
        validity_to__isnull=True,
    )
    if reminding_interval:
        policies = policies.filter(
            expiry_date__lte=now + core.datetimedelta(days=reminding_interval)
        )
    if location_id:
        # TODO support the various levels
        policies = policies.filter(
            Q(family__location_id=location_id)  # Village
            | Q(family__location__parent_id=location_id)  # Ward
            | Q(family__location__parent__parent_id=location_id)  # District
            | Q(family__location__parent__parent__parent_id=location_id)  # Region
        )
    if officer_id:
        policies = policies.filter(officer_id=officer_id)
    if date_from:
        policies = policies.filter(expiry_date__gte=date_from)
    if date_to:
        policies = policies.filter(expiry_date__lte=date_to)

    policies = policies.prefetch_related("product")

    for policy in policies:
        renewal_warning = 0
        renewal_date = policy.expiry_date + core.datetimedelta(days=1)
        product = policy.product  # will be updated if there is a conversion product
        officer = policy.officer
        # Get product code or substitution
        if not product.conversion_product_id:
            previous_products = []
            # Could also add a len(previous_products) < 20 but this avoids loops in the conversion_products
            while product not in previous_products and product.conversion_product:
                previous_products.append(product)
                product = product.conversion_product
            if product in previous_products:
                logger.error(
                    "The product %s has a substitution chain with a loop: %s, continuing with %s",
                    policy.product_id,
                    [p.id for p in previous_products],
                    product.id,
                )

        # TODO allow this kind of comparison where the left side is a datetime
        # if datetime.datetime(product.date_from) <= renewal_date <= product.date_to:
        # noinspection PyChainedComparisons
        if renewal_date >= product.date_from and renewal_date <= product.date_to:
            renewal_warning |= 1

        # This is from the original code but is actually not possible as we have an inner join on it
        if not policy.officer_id:
            renewal_warning |= 2
        else:
            if officer:
                previous_officers = []
                while officer not in previous_officers and officer.substitution_officer:
                    previous_officers.append(officer)
                    officer = officer.substitution_officer
                if officer in previous_officers:
                    logger.error(
                        "The product %s has a substitution chain with a loop: %s, continuing with %s",
                        policy.officer_id,
                        [o.id for o in previous_officers],
                        officer.id,
                    )
            if officer.works_to and renewal_date > officer.works_to:
                renewal_warning |= 4

        # Check if the policy has another following policy
        following_policies = (
            Policy.objects.filter(family_id=policy.family_id)
            .filter(Q(product_id=policy.product_id) | Q(product_id=product.id))
            .filter(start_date__gte=renewal_date)
        )
        if not following_policies.first():
            policy_renewal, policy_renewal_created = (
                PolicyRenewal.objects.get_or_create(
                    policy=policy,
                    validity_to=None,
                    defaults=dict(
                        renewal_prompt_date=now,
                        renewal_date=renewal_date,
                        new_officer=officer,
                        phone_number=officer.phone,
                        sms_status=0,
                        insuree=policy.family.head_insuree,
                        policy=policy,
                        new_product=product,
                        renewal_warnings=renewal_warning,
                        validity_from=now,
                        audit_user_id=0,
                    ),
                )
            )
            if policy_renewal_created:
                create_insuree_renewal_detail(
                    policy_renewal
                )  # The insuree module can create additional renewal data


def update_renewals():
    now = core.datetime.datetime.now()
    updated_policies = Policy.objects.filter(
        validity_to__isnull=True, expiry_date__lt=now
    ).update(status=Policy.STATUS_EXPIRED)
    logger.debug("update_renewals set %s policies to expired status", updated_policies)
    return updated_policies


@dataclass
class SmsQueueItem:
    index: int
    phone: str
    sms_message: str


def policy_renewal_sms(
    family_message_template, range_from=None, range_to=None, sms_header_template=None
):
    if sms_header_template is None:
        sms_header_template = """--Renewal--
{{renewal.renewal_date}}
{{renewal.insuree.chf_id}}
{{renewal.last_name}} {{renewal.other_names}}
{{district_name|default_if_none:"district?"}}
{{ward_name|default_if_none:"ward?"}}
{{village_name|default_if_none:"village?"}}
{{renewal.new_product.code}}-{{renewal.new_product.name}}
{% for detail in renewal.details.all %}{% if detail.insuree.is_head_of_family %}
HOF{% endif %}
{{detail.insuree.chf_id}}
{{detail.insuree.last_name}} {{detail.insuree.other_names}}
{% endfor %}
"""
    sms_header = Template(sms_header_template)
    family_message = Template(family_message_template)
    now = core.datetime.datetime.now()
    sms_queue = []
    i_count = 0  # TODO: remove and make this method a generator

    if not range_from:
        range_from = now
    if not range_to:
        range_to = now

    renewals = (
        PolicyRenewal.objects.filter(phone_number__isnull=False)
        .filter(renewal_prompt_date__gte=range_from)
        .filter(renewal_prompt_date__lte=range_to)
        .prefetch_related("insuree")
        .prefetch_related("new_officer")
        .prefetch_related("new_product")
        .prefetch_related("details")
        .prefetch_related("details__insuree")
    )

    for renewal in renewals:
        # Leaving the original code in comment to show it used to be handled. It is now delegated to the Django
        # template for the SMS, we just provide the list of renewal details
        #
        # first get the photo renewal string
        # for detail in renewal.details.filter(validity_to__isnull=True):
        #     if detail.insuree.chf_id == renewal.insuree.chf_id:  # not sure it's equivalent to checking the id
        #         head_photo_renewal = True
        #     else:
        #         sms_photos += f"\n{detail.insuree.chf_id}\n{detail.insuree.last_name} {detail.insuree.other_names}"
        # if len(sms_photos) > 0 or head_photo_renewal:
        #     head_text = "\nHOF" if head_photo_renewal else ""
        #     sms_photos += f"--Photos--{head_text}{sms_photos}" # added to sms_header

        village = renewal.policy.family.location
        sms_header_context = Context(
            dict(
                renewal=renewal,
                district_name=(
                    village.parent.parent if village and village.parent else None
                ),
                ward_name=village.parent if village else None,
                village_name=village,
            )
        )
        sms_header_text = sms_header.render(sms_header_context)
        sms_message = sms_header_text

        if renewal.new_officer.phone_communication:
            sms_queue.append(
                SmsQueueItem(i_count, renewal.new_officer.phone, sms_message)
            )
            i_count += 1

        # Create SMS for the family
        if family_message and renewal.insuree.phone:
            expiry_date = renewal.renewal_date - core.datetimedelta(days=1)
            new_family_message = family_message.render(
                Context(
                    dict(
                        insuree=renewal.insuree,
                        renewal=renewal,
                        expiry_date=expiry_date,
                    )
                )
            )

            if new_family_message:
                sms_queue.append(
                    SmsQueueItem(i_count, renewal.insuree.phone, new_family_message)
                )
                i_count += 1

    return sms_queue


def update_insuree_policies(policy, user, members=None):
    members = get_members(policy, policy.family, user, members)
    for member in members:
        existing_ip = InsureePolicy.objects.filter(
            *InsureePolicy.filter_validity(), insuree=member, policy=policy
        ).first()
        if existing_ip:
            existing_ip.save_history()
        ip, ip_created = InsureePolicy.objects.filter(
            *InsureePolicy.filter_validity()
        ).update_or_create(
            insuree=member,
            policy=policy,
            defaults=dict(
                enrollment_date=policy.enroll_date,
                start_date=policy.start_date,
                effective_date=policy.effective_date,
                expiry_date=policy.expiry_date,
                offline=policy.offline,
                audit_user_id=(
                    getattr(user, "id_for_audit", getattr(user, "id", user if isinstance(user, int) else -1))
                ),
            ),
        )
        if ip_created:
            logger.debug(
                "Created InsureePolicy(%s) %s - %s", ip.id, member.chf_id, policy.uuid
            )
        else:
            logger.debug(
                "Updated InsureePolicy(%s) %s - %s", ip.id, member.chf_id, policy.uuid
            )


def policy_status_premium_paid(policy, effective_date):
    if PolicyConfig.activation_option == PolicyConfig.ACTIVATION_OPTION_CONTRIBUTION:
        policy.effective_date = effective_date
        policy.status = Policy.STATUS_ACTIVE
    else:
        policy.status = Policy.STATUS_READY


def policy_status_payment_matched(policy):
    if (
        PolicyConfig.activation_option == PolicyConfig.ACTIVATION_OPTION_PAYMENT
        and policy.status == Policy.STATUS_IDLE
    ):
        policy.status = Policy.STATUS_ACTIVE
