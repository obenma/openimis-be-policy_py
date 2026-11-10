import logging

import graphene
from django.db import transaction

from policy.services import PolicyService, PolicyRenewalService

from .apps import PolicyConfig
from core.schema import OpenIMISMutation
from .models import Policy, PolicyMutation, PolicyRenewal
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied
from django.utils.translation import gettext as _
from .validations import validate_idle_policy

logger = logging.getLogger(__name__)


class PolicyInputType(OpenIMISMutation.Input):
    # several fields (such as status, stage,...) are managed "internally"
    # and only initialized/updated via dedicated mutations (renew , cancel,...)
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    enroll_date = graphene.Date(required=True)
    start_date = graphene.Date(required=True)
    expiry_date = graphene.Date(required=True)
    value = graphene.Decimal(max_digits=18, decimal_places=2, required=True)
    product_id = graphene.Int(required=True)
    family_id = graphene.Int(required=True)
    officer_id = graphene.Int(required=True)
    is_paid = graphene.Boolean(required=False)
    receipt = graphene.String(required=False)
    payer_uuid = graphene.String(required=False)
    contribution_plan_id = graphene.UUID(required=False)


class CreateRenewOrUpdatePolicyMutation(OpenIMISMutation):
    @classmethod
    def do_mutate(cls, perms, user, **data):
        if type(user) is AnonymousUser or not user.id:
            raise ValidationError(_("mutation.authentication_required"))
        if not user.has_perms(perms):
            raise PermissionDenied(_("unauthorized"))
        client_mutation_id = data.get("client_mutation_id")
        errors = validate_idle_policy(data)
        if len(errors):
            return errors
        data["audit_user_id"] = user.id_for_audit
        from core.utils import TimeUtils

        data["validity_from"] = TimeUtils.now()
        policy = PolicyService(user).update_or_create(data, user)
        logger.info(f"After policy create_or_update: {policy.uuid}")
        if data["stage"] == Policy.STAGE_RENEWED:
            logger.info("Deleting the optional PolicyRenewals after renewing")
            previous_policy = (Policy.objects.filter(validity_to__isnull=True,
                                                     family_id=data["family_id"],
                                                     product_id=data["product_id"],
                                                     status__in=[Policy.STATUS_EXPIRED, Policy.STATUS_ACTIVE])
                                             .order_by("-id")
                                             .first())
            if not previous_policy:
                logger.error("Can't find the policy that was renewed - not deleting the PolicyRenewals")
            policy_renewals = PolicyRenewal.objects.filter(policy=previous_policy, validity_to__isnull=True)
            logger.info(f"Total PolicyRenewals found: {policy_renewals.count()}")
            [PolicyRenewalService(user).delete(policy_renewal) for policy_renewal in policy_renewals]
        PolicyMutation.object_mutated(
            user, client_mutation_id=client_mutation_id, policy=policy
        )

        return None


class CreatePolicyMutation(CreateRenewOrUpdatePolicyMutation):
    _mutation_module = "policy"
    _mutation_class = "CreatePolicyMutation"

    class Input(PolicyInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            with transaction.atomic():
                data["status"] = Policy.STATUS_IDLE
                data["stage"] = Policy.STAGE_NEW
                return cls.do_mutate(
                    PolicyConfig.gql_mutation_create_policies_perms, user, **data
                )
        except Exception as exc:
            return [
                {
                    "message": _("policy.mutation.failed_to_create_policy"),
                    "detail": str(exc),
                    "exc": exc,
                }
            ]


class UpdatePolicyMutation(CreateRenewOrUpdatePolicyMutation):
    _mutation_module = "policy"
    _mutation_class = "UpdatePolicyMutation"

    class Input(PolicyInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            with transaction.atomic():
                return cls.do_mutate(
                    PolicyConfig.gql_mutation_edit_policies_perms, user, **data
                )
        except Exception as exc:
            return [
                {
                    "message": _("policy.mutation.failed_to_update_policy"),
                    "detail": str(exc),
                    "exc": exc,
                }
            ]


class RenewPolicyMutation(CreateRenewOrUpdatePolicyMutation):
    _mutation_module = "policy"
    _mutation_class = "RenewPolicyMutation"

    class Input(PolicyInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            with transaction.atomic():
                # ensure we don't update the existing one, but recreate a new one!
                if "policy_uuid" in data:
                    data["prev_policy"] = data.pop("policy_uuid")
                data["status"] = Policy.STATUS_IDLE
                data["stage"] = Policy.STAGE_RENEWED
                return cls.do_mutate(
                    PolicyConfig.gql_mutation_renew_policies_perms, user, **data
                )
        except Exception as exc:
            return [
                {
                    "message": _("policy.mutation.failed_to_renew_policy"),
                    "detail": str(exc),
                    "exc": exc,
                }
            ]


class SuspendPoliciesMutation(OpenIMISMutation):
    _mutation_module = "policy"
    _mutation_class = "SuspendPolicyMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            with transaction.atomic():
                if type(user) is AnonymousUser or not user.id:
                    raise ValidationError(_("mutation.authentication_required"))
                if not user.has_perms(PolicyConfig.gql_mutation_suspend_policies_perms):
                    raise PermissionDenied(_("unauthorized"))
                errors = []
                for policy_uuid in data["uuids"]:
                    policy = Policy.objects.filter(uuid=policy_uuid).first()
                    if policy is None:
                        # `errors += <dict>` iterates the dict and appends its
                        # *keys*, so every failure reached the journal as
                        # ["title", "list"] and the reason was lost (OP-3195).
                        errors.append({
                            "title": policy_uuid,
                            "list": [
                                {
                                    "message": _("policy.mutation.id_does_not_exist")
                                    % {"id": policy_uuid}
                                }
                            ],
                        })
                        continue
                    failure = PolicyService(user).set_suspended(user, policy)
                    if failure:
                        errors.append(failure)
                if len(errors) == 1:
                    errors = errors[0]["list"]
                return errors
        except Exception as exc:
            return [
                {
                    "message": _("policy.mutation.failed_to_suspend_policy"),
                    "detail": str(exc),
                    "exc": exc,
                }
            ]


class DeletePoliciesMutation(OpenIMISMutation):
    _mutation_module = "policy"
    _mutation_class = "DeletePoliciesMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            with transaction.atomic():
                if not user.has_perms(PolicyConfig.gql_mutation_delete_policies_perms):
                    raise PermissionDenied(_("unauthorized"))
                errors = []
                for policy_uuid in data["uuids"]:
                    policy = Policy.objects.filter(uuid=policy_uuid).first()
                    if policy is None:
                        errors.append({
                            "title": policy_uuid,
                            "list": [
                                {
                                    "message": _("policy.validation.id_does_not_exist")
                                    % {"id": policy_uuid}
                                }
                            ],
                        })
                        continue
                    failure = PolicyService(user).set_deleted(policy)
                    if failure:
                        errors.append(failure)
                if len(errors) == 1:
                    errors = errors[0]["list"]
                return errors
        except Exception as exc:
            return [
                {
                    "message": _("policy.mutation.failed_to_delete_policies"),
                    "detail": str(exc),
                    "exc": exc,
                }
            ]
