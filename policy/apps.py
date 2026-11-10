from django.apps import AppConfig

from core.rights_declaration import RightsDeclaration
from django.conf import settings


settings.SCHEDULER_JOBS.append(
    {
        "method": "policy.tasks.get_policies_for_renewal",
        "args": ["cron"],
        "kwargs": {"id": "openimis_renewal_batch", "hour": 8, "minute": 30, "replace_existing": True},
    }
)

MODULE_NAME = "policy"


# Rights, by entity then by action.
#
# 101201 is shared by design: reading policies, those of a family, those of an insuree
# or checking an eligibility are the same read, and the openIMIS catalogue expresses it
# that way. `queryOfficers` was attached to it because it held [], and so was open to
# everybody.
#
# 101203 covers modify and suspend: suspending a policy is a modification of its state.
# 101205 covers running a renewal and consulting the queue - for want of a dedicated
# read right; creating one is the better ending, not a rename.
DJANGO_PERMS = {
    "policy": {
        "query": ("policy.view_policy", 101201),
        "queryByFamily": ("policy.view_policy_by_family", 101201),
        "queryByInsuree": ("policy.view_policy_by_insuree", 101201),
        "queryEligibility": ("policy.view_eligibility", 101201),
        "queryOfficers": ("policy.view_policy_officer", 101201),
        "create": ("policy.add_policy", 101202),
        "update": ("policy.change_policy", 101203),
        "suspend": ("policy.suspend_policy", 101203),
        "delete": ("policy.delete_policy", 101204),
        "renew": ("policy.renew_policy", 101205),
        "queryRenewals": ("policy.view_policy_renewal", 101205),
    },
}

_PERM_CFG = {
    "gql_query_policies_perms": ("policy", "query"),
    "gql_query_policies_by_family_perms": ("policy", "queryByFamily"),
    "gql_query_policies_by_insuree_perms": ("policy", "queryByInsuree"),
    "gql_query_eligibilities_perms": ("policy", "queryEligibility"),
    "gql_query_policy_officers_perms": ("policy", "queryOfficers"),
    "gql_mutation_create_policies_perms": ("policy", "create"),
    "gql_mutation_edit_policies_perms": ("policy", "update"),
    "gql_mutation_suspend_policies_perms": ("policy", "suspend"),
    "gql_mutation_delete_policies_perms": ("policy", "delete"),
    "gql_mutation_renew_policies_perms": ("policy", "renew"),
    "gql_query_policy_renewals_perms": ("policy", "queryRenewals"),
}

RIGHTS = RightsDeclaration(MODULE_NAME, DJANGO_PERMS, _PERM_CFG)

perms = RIGHTS.perms
django_perms = RIGHTS.django_perm_names
configured_perms = RIGHTS.configured
require = RIGHTS.require


DEFAULT_CFG = {
    # Was [] - and `has_perms([])` returns True, so this query was open to every
    # authenticated user. Aliased onto gql_query_policies_perms (101201), the read right for the
    # entity it belongs to: no new id and no role to grant, and it narrows the
    # query from everyone to that entity's readers. A dedicated id would narrow it
    # further and is the better end state.
    # `policyRenewals` is a read, and it was gated by the *mutation* right above -
    # the right to perform a renewal, not to look at the queue of them. This names
    # the read, deliberately **aliased onto the same id (101205)** so that who can
    # see the queue does not change today. Pointing it at
    # gql_query_policies_perms (101201) instead would have widened it to everyone who
    # can read a policy; giving it an id of its own is the real fix and needs a new
    # right in this block plus a migration.
    "policy_renewal_interval": 14,  # Notify renewal nb of days before expiry date
    "policy_location_via": "family",  # ... or product
    "default_eligibility_disabled": False,
    "activation_option": 1,
    "ACTIVATION_OPTION_CONTRIBUTION": 1,
    "CTIVATION_OPTION_PAYMENT": 2,
    "ACTIVATION_OPTION_READY": 3,
    "contribution_receipt_length": 5,
}


class PolicyConfig(AppConfig):
    name = MODULE_NAME

    # Rights: constants, no longer overridable. They go neither through DEFAULT_CFG
    # nor through ready(): `ModuleConfiguration.get_or_default` now ignores any
    # `_perms` key stored in the database.
    gql_query_policies_perms = RIGHTS.perms("policy", "query")
    gql_query_policy_officers_perms = RIGHTS.perms("policy", "queryOfficers")
    gql_query_policies_by_insuree_perms = RIGHTS.perms("policy", "queryByInsuree")
    gql_query_policies_by_family_perms = RIGHTS.perms("policy", "queryByFamily")
    gql_query_eligibilities_perms = RIGHTS.perms("policy", "queryEligibility")
    gql_mutation_create_policies_perms = RIGHTS.perms("policy", "create")
    gql_mutation_renew_policies_perms = RIGHTS.perms("policy", "renew")
    gql_query_policy_renewals_perms = RIGHTS.perms("policy", "queryRenewals")
    gql_mutation_edit_policies_perms = RIGHTS.perms("policy", "update")
    gql_mutation_suspend_policies_perms = RIGHTS.perms("policy", "suspend")
    gql_mutation_delete_policies_perms = RIGHTS.perms("policy", "delete")
    policy_renewal_interval = None
    policy_location_via = None
    default_eligibility_disabled = None
    ACTIVATION_OPTION_CONTRIBUTION = None
    ACTIVATION_OPTION_PAYMENT = None
    ACTIVATION_OPTION_READY = None
    activation_option = None
    contribution_receipt_length = None

    def __load_config(self, cfg):
        for field in cfg:
            if hasattr(PolicyConfig, field):
                setattr(PolicyConfig, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration

        cfg = ModuleConfiguration.get_or_default(MODULE_NAME, DEFAULT_CFG)
        self.__load_config(cfg)
