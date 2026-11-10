from core.schema import (
    OrderedDjangoFilterConnectionField,
    signal_mutation_module_validate,
)
import graphene
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.db.models import Q
from .services import ByInsureeRequest, ByInsureeService
from .services import ByFamilyRequest, ByFamilyService
from .services import EligibilityRequest, EligibilityService
from .apps import PolicyConfig
from django.utils.translation import gettext as _
import graphene_django_optimizer as gql_optimizer
from graphene_django.filter import DjangoFilterConnectionField
from core.models import Officer
from .models import PolicyMutation, Policy, PolicyRenewal
from product.models import Product
from insuree.apps import InsureeConfig
from insuree.models import Family, Insuree, InsureePolicy
from django.db.models import OuterRef, Subquery, F, Count
from location.apps import LocationConfig

# We do need all queries and mutations in the namespace here.
from .gql_queries import (
    EligibilityGQLType,
    PolicyByFamilyOrInsureeGQLType,
    PolicyAndWarningsGQLType,
    PolicyGQLType,
    OfficerGQLType,
    PolicyRenewalGQLType,
    PolicyByFamilyOrInsureeConnection,
)  # lgtm [py/polluting-import]
from .gql_mutations import (
    CreatePolicyMutation,
    UpdatePolicyMutation,
    DeletePoliciesMutation,
    RenewPolicyMutation,
    SuspendPoliciesMutation
)

from .values import policy_values
from contribution_plan.models import ContributionPlan


class Query(graphene.ObjectType):
    policy_values = graphene.Field(
        PolicyAndWarningsGQLType,
        prev_uuid=graphene.String(required=False),
        stage=graphene.String(required=True),
        enrollDate=graphene.DateTime(required=True),
        product_id=graphene.Int(required=False),
        family_id=graphene.Int(required=True),
        contribution_plan_uuid=graphene.UUID(required=False),
    )
    policies = OrderedDjangoFilterConnectionField(
        PolicyGQLType,
        region_id=graphene.Int(),
        district_id=graphene.Int(),
        balance_lte=graphene.Float(),
        balance_gte=graphene.Float(),
        showHistory=graphene.Boolean(),
        showInactive=graphene.Boolean(),
        confirmationType=graphene.String(),
        orderBy=graphene.List(of_type=graphene.String),
    )

    # Note:
    # A Policy is bound to a Family...
    # but an insuree of the family is only covered by the family policy
    # if there is a (valid) InsureePolicy
    policies_by_insuree = graphene.relay.ConnectionField(
        PolicyByFamilyOrInsureeConnection,
        chf_id=graphene.String(required=True),
        active_or_last_expired_only=graphene.Boolean(),
        show_history=graphene.Boolean(),
        order_by=graphene.String(),
        target_date=graphene.Date(),
    )
    policies_by_family = graphene.relay.ConnectionField(
        PolicyByFamilyOrInsureeConnection,
        family_uuid=graphene.String(required=True),
        active_or_last_expired_only=graphene.Boolean(),
        show_history=graphene.Boolean(),
        order_by=graphene.String(),
        target_date=graphene.Date(),
    )
    # TODO: refactoring
    # Eligibility is calculated for a Policy... which is bound to a Family (not an Insuree)
    # YET: family member may not be covered by the policy!!
    # This requires to refactor the EligibilityService
    policy_eligibility_by_insuree = graphene.Field(
        EligibilityGQLType, chfId=graphene.String(required=True)
    )
    policy_item_eligibility_by_insuree = graphene.Field(
        EligibilityGQLType,
        chfId=graphene.String(required=True),
        itemCode=graphene.String(required=True),
    )
    policy_service_eligibility_by_insuree = graphene.Field(
        EligibilityGQLType,
        chfId=graphene.String(required=True),
        serviceCode=graphene.String(required=True),
    )
    policy_officers = DjangoFilterConnectionField(
        OfficerGQLType,
        search=graphene.String(),
        district=graphene.String(),
        region=graphene.String(),
    )

    policy_renewals = DjangoFilterConnectionField(PolicyRenewalGQLType)

    def resolve_policy_renewals(self, info, **kwargs):
        # A read gated by a read-named right; same id as the renew mutation for now,
        # see PolicyConfig. Non-admins are additionally narrowed to their own officer
        # just below.
        if not info.context.user.has_perms(PolicyConfig.gql_query_policy_renewals_perms):
            raise PermissionDenied(_("unauthorized"))
        user = info.context.user
        filters = Q(validity_to__isnull=True)
        if hasattr(user, "is_imis_admin") and not user.is_imis_admin:
            enrollment_officer = user.officer
            filters &= Q(new_officer=enrollment_officer)
        return PolicyRenewal.objects.filter(filters)

    def resolve_policy_values(self, info, **kwargs):
        if not info.context.user.has_perms(PolicyConfig.gql_query_policies_perms):
            raise PermissionDenied(_("unauthorized"))
        cp_uuid = None
        product_id = None
        if "contribution_plan_uuid" in kwargs:
            cp_uuid = str(kwargs.get("contribution_plan_uuid"))
            contribution_plan = ContributionPlan.objects.filter(uuid=cp_uuid).first()
            if not contribution_plan:
                raise ValueError(f"Contribution plan {cp_uuid} not found")

            if not contribution_plan.benefit_plan_type.name == "product":
                raise ValueError(
                    f"Contribution plan {cp_uuid} is not attached to a product"
                )
            product_id = contribution_plan.benefit_plan
        elif "product_id" in kwargs:
            product_id = kwargs.get("product_id")
        else:
            raise ValueError("Product or contribution plan is mandatory")

        product = None
        if product_id:
            product = (
                Product.objects.filter(
                    Q(validity_to__isnull=True),
                    Q(id=product_id) | Q(legacy_id=product_id),
                    Q(validity_from__date__lte=kwargs.get("enrollDate"))
                    | Q(date_from__lte=kwargs.get("enrollDate")),
                )
                .order_by("-validity_from")
                .first()
            )

        if not product:
            raise ValueError(f"product {product_id} not found")
        policy = PolicyGQLType(
            stage=kwargs.get("stage"),
            enroll_date=kwargs.get("enrollDate"),
            start_date=kwargs.get("enrollDate"),
            product=product,
            contribution_plan=cp_uuid,
        )
        prefetch = Prefetch(
            "members",
            queryset=Insuree.objects.filter(validity_to__isnull=True).order_by(
                "validity_from"
            ),
        )
        family = Family.objects.prefetch_related(prefetch).get(
            id=kwargs.get("family_id")
        )
        prev_policy = None
        if "prev_uuid" in kwargs:
            prev_policy = Policy.objects.get(uuid=kwargs.get("prev_uuid"))
        policy, warnings = policy_values(policy, family, prev_policy, info.context.user)
        return PolicyAndWarningsGQLType(policy=policy, warnings=warnings)

    def resolve_policies(self, info, **kwargs):
        if not info.context.user.has_perms(PolicyConfig.gql_query_policies_perms):
            raise PermissionDenied(_("unauthorized"))
        query = Policy.objects
        if not kwargs.get("showHistory", False):
            query = query.filter(*Policy.filter_validity(**kwargs))
        if kwargs.get("showInactive", False):
            family_count = (
                Insuree.objects.values("family_id")
                .filter(validity_to__isnull=True)
                .annotate(m_count=Count("id"))
            )
            family_sq = family_count.filter(family_id=OuterRef("family_id"))
            covered_count = (
                InsureePolicy.objects.values("policy_id")
                .filter(validity_to__isnull=True)
                .annotate(i_count=Count("id"))
            )
            covered_sq = covered_count.filter(policy_id=OuterRef("id"))
            query = query.annotate(
                inactive_count=Subquery(family_sq.values("m_count"))
                - Subquery(covered_sq.values("i_count"))
            )
            query = query.filter(inactive_count__gt=0)
        if (
            kwargs.get("balance_lte")
            or kwargs.get("balance_gte")
            or kwargs.get("sum_premiums", False)
        ):
            query = query.annotate(sum_premiums=Policy.get_query_sum_premium())
        if kwargs.get("balance_lte") or kwargs.get("balance_gte"):
            query = query.annotate(balance=F("value") - F("sum_premiums"))
        if kwargs.get("balance_lte"):
            query = query.filter(balance__lte=kwargs.get("balance_lte"))
        if kwargs.get("balance_gte"):
            query = query.filter(balance__gte=kwargs.get("balance_gte"))
        if kwargs.get("confirmationType"):
            query = query.filter(
                family__confirmation_type=kwargs.get("confirmationType")
            )
        location_id = (
            kwargs.get("district_id")
            if kwargs.get("district_id")
            else kwargs.get("region_id")
        )
        if location_id:
            location_level = 2 if kwargs.get("district_id") else 1
            f = "_id"
            for i in range(len(LocationConfig.location_types) - location_level):
                f = "__parent" + f
            f = PolicyConfig.policy_location_via + "__location" + f
            query = query.filter(Q(**{f: location_id}))
        return gql_optimizer.query(query.all(), info)

    @staticmethod
    def _to_policy_by_family_or_insuree_item(item):
        return PolicyByFamilyOrInsureeGQLType(
            policy_id=item.policy_id,
            policy_uuid=item.policy_uuid,
            policy_value=item.policy_value,
            product_code=item.product_code,
            product_name=item.product_name,
            contribution_plan_code=item.contribution_plan_code,
            contribution_plan_name=item.contribution_plan_name,
            start_date=item.start_date,
            enroll_date=item.enroll_date,
            effective_date=item.effective_date,
            expiry_date=item.expiry_date,
            officer_code=item.officer_code,
            officer_name=item.officer_name,
            status=item.status,
            ded=item.ded,
            ded_in_patient=item.ded_in_patient,
            ded_out_patient=item.ded_out_patient,
            ceiling=item.ceiling,
            ceiling_in_patient=item.ceiling_in_patient,
            ceiling_out_patient=item.ceiling_out_patient,
            balance=item.balance,
            validity_from=item.validity_from,
            validity_to=item.validity_to,
            max_installments=item.max_installments,
        )

    def resolve_policies_by_insuree(self, info, **kwargs):
        # A nationwide lookup: `ByInsureeService` filters on the CHFID and on nothing
        # else, so any identifier resolves to that insuree's policies wherever they
        # live. That is the enquiry, and the claim panel embeds it through
        # `policy.InsureePolicyEligibilitySummary`. It used to be authorised by
        # borrowing the claim read right, so being able to read claims silently
        # carried being able to read the country's policies, one CHFID at a time.
        #
        # The enquiry right is insuree's and is checked as insuree's: it is one right
        # with one name, and giving policy a second name for the same id would be more
        # of the aliasing that makes a grant impossible to read off an id.
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_policies_by_insuree_perms
        ) and not info.context.user.has_perms(
            InsureeConfig.gql_query_insuree_inquire_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        req = ByInsureeRequest(
            chf_id=kwargs.get("chf_id"),
            active_or_last_expired_only=kwargs.get(
                "active_or_last_expired_only", False
            ),
            show_history=kwargs.get("show_history", False),
            order_by=kwargs.get("order_by", None),
            target_date=kwargs.get("target_date", None),
        )
        res = ByInsureeService(user=info.context.user).request(req)
        return [Query._to_policy_by_family_or_insuree_item(x) for x in res.items]

    def resolve_policies_by_family(self, info, **kwargs):
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_policies_by_family_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        req = ByFamilyRequest(
            family_uuid=kwargs.get("family_uuid"),
            active_or_last_expired_only=kwargs.get(
                "active_or_last_expired_only", False
            ),
            show_history=kwargs.get("show_history", False),
            order_by=kwargs.get("order_by", None),
            target_date=kwargs.get("target_date", None),
        )
        res = ByFamilyService(user=info.context.user).request(req)
        return [Query._to_policy_by_family_or_insuree_item(x) for x in res.items]

    @staticmethod
    def _resolve_policy_eligibility_by_insuree(user, req):
        res = EligibilityService(user=user).request(req)
        return EligibilityGQLType(
            prod_id=res.prod_id,
            total_admissions_left=res.total_admissions_left,
            total_visits_left=res.total_visits_left,
            total_consultations_left=res.total_consultations_left,
            total_surgeries_left=res.total_surgeries_left,
            total_deliveries_left=res.total_deliveries_left,
            total_antenatal_left=res.total_antenatal_left,
            consultation_amount_left=res.consultation_amount_left,
            surgery_amount_left=res.surgery_amount_left,
            delivery_amount_left=res.delivery_amount_left,
            hospitalization_amount_left=res.hospitalization_amount_left,
            antenatal_amount_left=res.antenatal_amount_left,
            min_date_service=res.min_date_service,
            min_date_item=res.min_date_item,
            service_left=res.service_left,
            item_left=res.item_left,
            is_item_ok=res.is_item_ok,
            is_service_ok=res.is_service_ok,
        )

    def resolve_policy_eligibility_by_insuree(self, info, **kwargs):
        # Eligibility is keyed on an insuree and answers one question: may this person
        # be served here, now. That is the same business act as looking them up, and
        # nobody enters a claim without first enquiring - the converse happens, the
        # implication does not. So the enquiry right reaches it, alongside 101201.
        # `EligibilityService.request` carries the same pair, because REST and FHIR
        # reach eligibility without passing through this resolver.
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_eligibilities_perms
        ) and not info.context.user.has_perms(
            InsureeConfig.gql_query_insuree_inquire_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        req = EligibilityRequest(chf_id=kwargs.get("chfId"))
        return Query._resolve_policy_eligibility_by_insuree(
            user=info.context.user, req=req
        )

    def resolve_policy_item_eligibility_by_insuree(self, info, **kwargs):
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_eligibilities_perms
        ) and not info.context.user.has_perms(
            InsureeConfig.gql_query_insuree_inquire_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        req = EligibilityRequest(
            chf_id=kwargs.get("chfId"), item_code=kwargs.get("itemCode")
        )
        return Query._resolve_policy_eligibility_by_insuree(
            user=info.context.user, req=req
        )

    def resolve_policy_service_eligibility_by_insuree(self, info, **kwargs):
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_eligibilities_perms
        ) and not info.context.user.has_perms(
            InsureeConfig.gql_query_insuree_inquire_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        req = EligibilityRequest(
            chf_id=kwargs.get("chfId"), service_code=kwargs.get("serviceCode")
        )
        return Query._resolve_policy_eligibility_by_insuree(
            user=info.context.user, req=req
        )

    def resolve_policy_officers(
        self, info, search=None, district=None, region=None, **kwargs
    ):
        if not info.context.user.has_perms(
            PolicyConfig.gql_query_policy_officers_perms
        ):
            raise PermissionDenied(_("unauthorized"))
        queryset = Officer.objects
        location_id = district if district else region

        if location_id is not None:
            location = int(location_id)
            queryset = queryset.filter(
                Q(
                    officer_villages__location__isnull=False,
                    officer_villages__location__id=location,
                )  # villages
                | Q(
                    officer_villages__location__parent_id__isnull=False,
                    officer_villages__location__parent_id=location,
                )  # municipalities
                | Q(
                    officer_villages__location__parent__parent_id__isnull=False,
                    officer_villages__location__parent__parent_id=location,
                )  # districts
                | Q(
                    officer_villages__location__parent__parent__parent_id__isnull=False,
                    officer_villages__location__parent__parent__parent_id=location,
                )  # regions
            ).distinct()

        if search is not None:
            queryset = queryset.filter(
                Q(code__icontains=search)
                | Q(last_name__icontains=search)
                | Q(other_names__icontains=search)
            )

        return queryset


class Mutation(graphene.ObjectType):
    create_policy = CreatePolicyMutation.Field()
    update_policy = UpdatePolicyMutation.Field()
    delete_policies = DeletePoliciesMutation.Field()
    renew_policy = RenewPolicyMutation.Field()
    suspend_policies = SuspendPoliciesMutation.Field()


def on_policy_mutation(sender, **kwargs):
    uuids = kwargs["data"].get("uuids", [])
    if not uuids:
        uuid = kwargs["data"].get("policy_uuid", None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    impacted_policies = Policy.objects.filter(uuid__in=uuids).all()
    for policy in impacted_policies:
        PolicyMutation.objects.create(
            policy=policy, mutation_id=kwargs["mutation_log_id"]
        )
    return []


def bind_signals():
    signal_mutation_module_validate["policy"].connect(on_policy_mutation)
