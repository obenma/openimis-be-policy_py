from django.db.models import Func, DateTimeField
from calculation.services import run_calculation_rules
from contribution_plan.models import ContributionPlan


class MonthsAdd(Func):
    """
    Custom function that is suitable for MS SQL and Postgresql. If using different database it is possible that
    it might need an update with custom resolve.
    Usage: Foo.objects.annotate(end_date=MonthsAdd('start_date', 'duration')).filter(end_date__gt=datetime.now)
    """

    # https://stackoverflow.com/questions/33981468/using-dateadd-in-django-filter

    arg_joiner = " + CAST("
    template = "%(expressions)s || 'months' as INTERVAL)"

    template_mssql = "%(function)s(MONTH, %(expressions)s)"
    function_mssql = "DATEADD"

    output_field = DateTimeField()
    arity = 2
    COUNTER = 0

    def as_sql(self, compiler, connection, **extra_context):

        if connection.vendor == "microsoft":
            self.arg_joiner = ", "
            self.source_expressions = (
                self.get_source_expressions()[::-1]
                if self.COUNTER == 0
                else self.get_source_expressions()
            )
            self.template = self.template_mssql
            self.function = self.function_mssql
        self.COUNTER += 1
        return super().as_sql(compiler, connection, **extra_context)


def get_queryset_valid_at_date(queryset, date):
    filtered_qs = queryset.filter(validity_to__gte=date, validity_from__lte=date)
    if len(filtered_qs) > 0:
        return filtered_qs
    return queryset.filter(validity_from__date__lte=date, validity_to__isnull=True)


def get_members(policy, family, user, members=None):
    if members:
        return members
    # get the current policy members

    # look in calculation rules
    if policy.contribution_plan:
        instance = (
            ContributionPlan.objects.filter(uuid=str(policy.contribution_plan)).first()
            if policy.contribution_plan
            else None
        )
        if instance:
            members = run_calculation_rules(
                instance=instance,
                user=user,
                context="members",
                family=family,
            )
    if not members and policy.insuree_policies:
        members = [
            ip.insuree
            for ip in policy.insuree_policies.filter(validity_to__isnull=True)
        ]
    # fallback on family
    if not members:
        members = family.members.filter(validity_to__isnull=True).all()
    return members


def _contribution_plan(policy):
    """The contribution plan of `policy` (the instance the calculation rules answer for)."""
    return getattr(policy, "contribution_plan", None)


# the validity a calculation rule gives a contract (calcrule_contribution_legacy,
# context "validity"): one set of dates for every member
SINGLE_VALIDITY_KEYS = {"enroll_date", "start_date", "effective_date", "expiry_date"}


def _is_single_validity(validity):
    return bool(validity) and set(validity) <= SINGLE_VALIDITY_KEYS


def get_validity(policy, family, user, members=None):
    """
    The members `policy` covers, each with their own validity (OP-3321):
    [(member, {"start_date", "effective_date", "expiry_date"} or None)].

    The calculation rules are asked (context "validity"; instance: the policy's
    contribution plan, or the policy itself as calling context; kwargs: `policy`,
    `family` and the `members` of get_members). A rule may answer:
    - a map member (or member id) -> dates: a member it gives no dates to is not
      covered any more;
    - a single validity (the former structure, {enroll_date, start_date,
      effective_date, expiry_date}): applied to every member of get_members.
    Without an answer (no rule handles it) every member of get_members is covered with
    the policy dates (None). Missing keys mean the policy dates; `member_dates` keeps
    them within the policy period.
    """
    members = list(get_members(policy, family, user, members) or [])
    instance = _contribution_plan(policy) or policy
    validity = run_calculation_rules(
        instance=instance,
        user=user,
        context="validity",
        policy=policy,
        family=family,
        members=members,
    )
    if not isinstance(validity, dict) or not validity:
        return [(member, None) for member in members]
    if _is_single_validity(validity):
        return [(member, dict(validity)) for member in members]
    by_id = {getattr(key, "id", key): dates for key, dates in validity.items()}
    return [(member, by_id[member.id]) for member in members if by_id.get(member.id)]


def _as_date(value):
    if value is None:
        return None
    return value.date() if hasattr(value, "date") and callable(value.date) else value


def earliest_member_start(validity):
    """The earliest start the rule gives the covered members, None without any."""
    starts = [_as_date(dates.get("start_date")) for _, dates in validity if dates and dates.get("start_date")]
    return min(starts) if starts else None


def member_dates(policy, dates, activation_date=None):
    """
    The InsureePolicy dates of a covered member: their validity (`dates`, None = the
    policy ones) kept within the policy period. The effective date is the activation
    (`activation_date`, the policy effective date by default) but never before the
    member starts: a member, as a policy, is not active before their start date.
    """
    dates = dates or {}
    start = _as_date(policy.start_date)
    expiry = _as_date(policy.expiry_date)
    member_start = _as_date(dates.get("start_date")) or start
    if start and member_start and member_start < start:
        member_start = start
    member_expiry = _as_date(dates.get("expiry_date")) or expiry
    if expiry and member_expiry and member_expiry > expiry:
        member_expiry = expiry
    effective = _as_date(activation_date if activation_date is not None else policy.effective_date)
    if dates.get("effective_date") and activation_date is None:
        effective = _as_date(dates["effective_date"])
    if effective is not None and member_start and effective < member_start:
        effective = member_start
    return {"start_date": member_start, "effective_date": effective, "expiry_date": member_expiry}
