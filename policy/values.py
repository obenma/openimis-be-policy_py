from django.utils.translation import gettext as _
import datetime as py_datetime
from decimal import Decimal
from .models import Policy
from calculation.services import run_calculation_rules
from core.apps import CoreConfig
from dateutil.relativedelta import relativedelta
from contribution_plan.models import ContributionPlan
from policy.utils import get_validity, earliest_member_start


def _as_plain_date(value):
    if value is None:
        return None
    return value.date() if isinstance(value, py_datetime.datetime) else value


def cycle_start(product, cycle, ref_date):
    c = getattr(product, "start_cycle_%s" % (cycle + 1), None)
    if not c:
        return None
    if CoreConfig.secondary_calendar == "Nepal":
        import nepali_datetime

        nepali_start = nepali_datetime.datetime.strptime(
            "%s-%s" % (c, nepali_datetime.date.today().year), "%d-%m-%Y"
        )
        start = nepali_start.to_datetime_date()
    else:
        start = py_datetime.datetime.strptime("%s-%s" % (c, ref_date.year), "%d-%m-%Y")
    if ref_date <= start:
        return start


def set_start_date(policy, earliest_start=None):
    """
    `earliest_start`: the earliest start date of the covered members (their validity,
    OP-3321): when later than the enrolment, the policy starts from it (cycles apply
    after).
    """
    from core import datetime, datetimedelta

    product = policy.product
    ref_enroll_date = policy.enroll_date
    if earliest_start is not None and earliest_start > _as_plain_date(ref_enroll_date):
        ref_enroll_date = earliest_start
    if policy.stage == Policy.STAGE_NEW and product.administration_period:
        ref_enroll_date = (
            datetime.date.from_ad_date(ref_enroll_date)
            + datetimedelta(months=product.administration_period)
        ).to_ad_date()

    if not product.has_cycle():
        policy.start_date = ref_enroll_date
        return

    grace = 0
    if policy.stage == Policy.STAGE_NEW and product.grace_period_enrolment:
        grace = product.grace_period_enrolment
    elif policy.stage == Policy.STAGE_RENEWED and product.grace_period_renewal:
        grace = product.grace_period_renewal

    ref_date = (
        datetime.date.from_ad_date(ref_enroll_date) - datetimedelta(months=grace)
    ).to_ad_date()
    for i in range(4):
        start = cycle_start(product, i, ref_date)
        if start:
            policy.start_date = datetime.date.from_ad_date(start)
            return
    policy.start_date = datetime.date.from_ad_date(
        py_datetime.datetime.strptime(
            "%s-%s" % (product.start_cycle_1, ref_date.year + 1), "%d-%m-%Y"
        )
    )


def set_expiry_date(policy):
    product = policy.product
    from core import datetime, datetimedelta

    insurance_period = (
        datetimedelta(months=product.insurance_period)
        if product.insurance_period % 12 != 0
        else datetimedelta(years=product.insurance_period // 12)
    )
    policy.expiry_date = (
        datetime.date.from_ad_date(policy.start_date)
        + insurance_period
        - datetimedelta(days=1)
    ).to_ad_date()


def count_member(members, majority_date, not_related=False, older=True):
    return len(
        list(
            filter(
                lambda person: (person.relationship != 7) ^ not_related
                and (person.dob >= majority_date) ^ older,
                members,
            )
        )
    )


def family_counts(product, members=None):

    extra_adults = 0
    extra_children = 0
    date_threshold = py_datetime.date.today() - relativedelta(
        years=CoreConfig.age_of_majority
    )
    adults = count_member(members, date_threshold)
    children = count_member(members, date_threshold, older=False)
    other_children = count_member(
        members, date_threshold, not_related=True, older=False
    )
    other_adults = count_member(members, date_threshold, not_related=True)

    over_children = 0
    over_adults = 0
    over_other_children = 0
    over_other_adults = 0

    if product.max_members:
        over_adults = max(0, adults - product.max_members)
        over_children = max(0, adults + children - over_adults - product.max_members)
        over_other_children = max(
            0,
            adults
            + children
            + other_children
            - over_adults
            - over_children
            - product.max_members,
        )
        over_other_adults = max(
            0,
            adults
            + other_adults
            + children
            + other_children
            - over_adults
            - over_other_children
            - over_children
            - product.max_members,
        )

    # remove over from count
    children -= over_children
    adults -= over_adults
    other_children -= over_other_children
    other_adults -= over_other_adults
    if product.threshold:
        extra_adults = max(0, adults - product.threshold)
        extra_children = max(0, children + adults - extra_adults - product.threshold)

    return {
        "adults": adults - extra_adults,  # adult part of the "lump sum"
        "extra_adults": extra_adults,  # adult not part of the "lump sum" because of threshold
        "other_adults": other_adults,  # adult never of the "lump sum"
        "children": children - extra_children,  # children part of the "lump sum"
        "extra_children": extra_children,  # children never part of the "lump sum" because of threshold
        "other_children": other_children,  # children never part of the "lump sum"
        "total": adults + other_adults + children + other_children,
    }


def get_attr(product, attr):
    #  getattr(product, attr, 0)... returns None if the attr is there (with None as value!)
    value = getattr(product, attr, 0)
    return value if value else 0


def sum_contributions(product, f_counts):
    contributions = 0
    premium_adult = get_attr(product, "premium_adult")
    premium_child = get_attr(product, "premium_child")
    if product.lump_sum:
        contributions = product.lump_sum
        contributions += f_counts["extra_adults"] * premium_adult
        contributions += f_counts["extra_children"] * premium_child
    else:
        contributions += f_counts["adults"] * premium_adult
        contributions += f_counts["children"] * premium_child
    contributions += f_counts["other_adults"] * premium_adult
    contributions += f_counts["other_children"] * premium_child
    return contributions


def sum_general_assemblies(product, f_counts):
    if product.general_assembly_lump_sum:
        return product.general_assembly_lump_sum
    return f_counts["total"] * get_attr(product, "general_assembly_fee")


def sum_registrations(policy, product, f_counts):
    if policy.stage != Policy.STAGE_NEW:
        return 0
    if product.registration_lump_sum:
        return product.registration_lump_sum
    return f_counts["total"] * get_attr(product, "registration_fee")


def discount_new(policy):
    product = policy.product
    if product.has_enrolment_discount() and product.has_cycle():
        from core import datetime, datetimedelta

        min_discount_date = (
            datetime.date.from_ad_date(policy.start_date)
            - datetimedelta(months=product.enrolment_discount_period)
        ).to_ad_datetime()
        if policy.enroll_date <= min_discount_date:
            policy.value -= policy.value * product.enrolment_discount_perc / 100


def discount_renew(policy, prev_policy):
    product = policy.product
    if product.has_renewal_discount():
        from core import datetime, datetimedelta

        min_discount_date = (
            datetime.date.from_ad_date(prev_policy.expiry_date)
            + datetimedelta(days=1)
            - datetimedelta(months=product.renewal_discount_period)
        ).to_ad_datetime()
        if policy.enroll_date <= min_discount_date:
            policy.value -= policy.value * product.renewal_discount_perc / 100


def discount(policy, prev_policy):
    if policy.stage == Policy.STAGE_NEW:
        discount_new(policy)
    elif policy.stage == Policy.STAGE_RENEWED:
        discount_renew(policy, prev_policy)


def set_value(policy, members, prev_policy, user):
    product = policy.product
    f_counts = family_counts(policy.product, members)
    contributions = sum_contributions(product, f_counts)
    general_assembly = sum_general_assemblies(product, f_counts)
    registration = sum_registrations(policy, product, f_counts)
    policy.value = Decimal(contributions + general_assembly + registration)
    discount(policy, prev_policy)
    # try to get policy value from calcrule
    instance = (
        ContributionPlan.objects.filter(uuid=policy.contribution_plan).first()
        if policy.contribution_plan
        else None
    )
    policy_value = (
        run_calculation_rules(
            instance=instance,
            user=user,
            context="policy_value",
            family=policy.family,
        )
        if instance
        else None
    )
    # to allow 0 as policy value
    if policy_value is not None:
        policy.value = Decimal(policy_value)
    else:
        set_legacy_policy_value(product, policy, prev_policy, f_counts)


def set_legacy_policy_value(product, policy, prev_policy, f_counts):
    contributions = sum_contributions(product, f_counts)
    general_assembly = sum_general_assemblies(product, f_counts)
    registration = sum_registrations(policy, product, f_counts)
    policy.value = Decimal(contributions + general_assembly + registration)
    discount(policy, prev_policy)


def policy_values(policy, family, prev_policy, user, members=None):
    """
    The members covered (with their validity, policy.utils.get_validity) are counted;
    the policy starts no earlier than the first of them.
    """
    validity = get_validity(policy, family, user, members)
    members = [member for member, _ in validity]
    max_members = policy.product.max_members
    above_max = max(0, len(members or []) - max_members)
    warnings = []
    if above_max:
        warnings.append(
            _("policy.validation.members_count_above_max")
            % {"max": max_members, "count": len(members)}
        )
    set_start_date(policy, earliest_start=earliest_member_start(validity))
    set_expiry_date(policy)
    set_value(policy, members, prev_policy, user)
    return policy, warnings
