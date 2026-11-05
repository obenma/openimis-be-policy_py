import graphene
from graphene_django import DjangoObjectType
from django.utils.translation import gettext as _
from .apps import PolicyConfig
from .models import Policy, PolicyRenewal
from core import (
    prefix_filterset,
    ExtendedConnection,
    ExtendedRelayConnection,
)
from core.schema import OfficerGQLType
from product.schema import ProductGQLType
from django.core.exceptions import PermissionDenied
from core.gql import ScopedQuerysetMixin


class PolicyGQLType(DjangoObjectType):
    sum_premiums = graphene.Float(source="sum_premiums")

    def resolve_family(self, info):
        if not info.context.user.has_perms(PolicyConfig.gql_query_policies_perms):
            raise PermissionDenied(_("unauthorized"))
        if "family_loader" in info.context.dataloaders and self.family_id:
            return info.context.dataloaders["family_loader"].load(self.family_id)
        return self.family

    def resolve_product(self, info):
        if not info.context.user.has_perms(PolicyConfig.gql_query_policies_perms):
            raise PermissionDenied(_("unauthorized"))
        if "product_loader" in info.context.dataloaders and self.product_id:
            return info.context.dataloaders["product_loader"].load(self.product_id)
        return self.product

    class Meta:
        model = Policy
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "uuid": ["exact"],
            "enroll_date": ["exact", "lt", "lte", "gt", "gte"],
            "start_date": ["exact", "lt", "lte", "gt", "gte"],
            "effective_date": ["exact", "lt", "lte", "gt", "gte"],
            "expiry_date": ["exact", "lt", "lte", "gt", "gte"],
            "stage": ["exact"],
            "status": ["exact", "lt", "lte", "gt", "gte"],
            "value": ["exact", "lt", "lte", "gt", "gte"],
            "insuree_policies__insuree__chf_id": ["exact", "icontains"],
            "insuree_policies__insuree__last_name": ["icontains"],
            "insuree_policies__insuree__other_names": ["icontains"],
            **prefix_filterset("product__", ProductGQLType._meta.filter_fields),
            **prefix_filterset("officer__", OfficerGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        # Row security without the validity filter: `policies(showHistory: true)`
        # returns superseded versions, which the resolver selects itself.
        queryset = Policy.get_scoped_queryset(queryset, info.context.user)
        # Prevent duplicate Policy records when filtering by chfId through
        # insuree_policies__insuree__chf_id. This type of filter creates a JOIN
        # that can return multiple rows per Policy if multiple InsureePolicies exist.
        return queryset.distinct()


class PolicyRenewalGQLType(ScopedQuerysetMixin, DjangoObjectType):
    class Meta:
        model = PolicyRenewal
        interfaces = (graphene.relay.Node,)
        filter_fields = {
        }
        connection_class = ExtendedConnection


class PolicyAndWarningsGQLType(graphene.ObjectType):
    policy = graphene.Field(PolicyGQLType)
    warnings = graphene.List(graphene.String)


class PolicyByFamilyOrInsureeGQLType(graphene.ObjectType):
    class Meta:
        interfaces = (graphene.relay.Node,)

    policy_id = graphene.Int()
    policy_uuid = graphene.String()
    policy_value = graphene.Float()
    product_code = graphene.String()
    product_name = graphene.String()
    start_date = graphene.Date()
    enroll_date = graphene.Date()
    effective_date = graphene.Date()
    expiry_date = graphene.Date()
    officer_code = graphene.String()
    officer_name = graphene.String()
    status = graphene.Int()
    ded = graphene.Float()
    ded_in_patient = graphene.Float()
    ded_out_patient = graphene.Float()
    ceiling = graphene.Float()
    ceiling_in_patient = graphene.Float()
    ceiling_out_patient = graphene.Float()
    balance = graphene.Float()
    validity_from = graphene.Date()
    validity_to = graphene.Date()
    max_installments = graphene.Int()
    contribution_plan_code = graphene.String()
    contribution_plan_name = graphene.String()


class PolicyByFamilyOrInsureeConnection(ExtendedRelayConnection):
    class Meta:
        node = PolicyByFamilyOrInsureeGQLType


class EligibilityGQLType(graphene.ObjectType):
    prod_id = graphene.String()
    total_admissions_left = graphene.Int()
    total_visits_left = graphene.Int()
    total_consultations_left = graphene.Int()
    total_surgeries_left = graphene.Int()
    total_deliveries_left = graphene.Int()
    total_antenatal_left = graphene.Int()
    consultation_amount_left = graphene.Float()
    surgery_amount_left = graphene.Float()
    delivery_amount_left = graphene.Float()
    hospitalization_amount_left = graphene.Float()
    antenatal_amount_left = graphene.Float()
    min_date_service = graphene.types.datetime.Date()
    min_date_item = graphene.types.datetime.Date()
    service_left = graphene.Int()
    item_left = graphene.Int()
    is_item_ok = graphene.Boolean()
    is_service_ok = graphene.Boolean()
