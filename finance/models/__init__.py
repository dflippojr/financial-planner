"""The finance app's models, split by area (#759).

Each submodule holds one area's models. This package re-exports every name the
old finance/models.py module exposed, so callers keep `from finance.models
import X` and `app_label` stays "finance". Import order follows the submodules'
dependencies; importing this package loads every model, as Django's app
registry and the ready() hooks expect.

Historical migrations import these names by path from finance.models, so their
re-exports are permanent (tests/test_models_package.py): validate_json_object,
validate_bulk_edit_snapshot, validate_saved_filter_query, validate_reason_list,
validate_header_name_list, validate_stored_csv_headers and _detach_draw_downs.
"""
# Re-exports for callers and migrations; nothing here is used locally.
from fractions import Fraction  # noqa: F401

from django.conf import settings  # noqa: F401
from django.core.exceptions import ImproperlyConfigured, ValidationError  # noqa: F401
from django.core.validators import RegexValidator  # noqa: F401
from django.db.models import F, Q  # noqa: F401
from django.db.models.functions import Lower  # noqa: F401

from ..ai_types import UNKNOWN_BACKEND_LABEL, backend_display_name  # noqa: F401
from ..input_limits import AUTO_NOTE_MAX_CHARS, BUDGET_MONTH_FLOOR, MAX_DESCRIPTION_CHARS  # noqa: F401
from ..money_display import format_minor  # noqa: F401
from .base import (  # noqa: F401
    actor_display_name,
    ArchivableModel,
    FORMER_MEMBER_LABEL,
    receipt_stored_name_validator,
    SAVED_FILTER_QUERY_KEYS,
    sha256_validator,
    validate_bulk_edit_snapshot,
    validate_header_name_list,
    validate_json_object,
    validate_reason_list,
    validate_saved_filter_query,
    validate_stored_csv_headers,
)
from .people import (  # noqa: F401
    Household,
    Invitation,
    LoginThrottle,
    MemberSecurityEvent,
    MemberSession,
    Membership,
    Passkey,
    PendingSignInSession,
    Person,
    PrivacyPolicyAcceptance,
    PrivacyPolicyVersion,
    RecoveryCode,
)
from .categories import (  # noqa: F401
    Category,
    Tag,
)
from .accounts import (  # noqa: F401
    Account,
    AccountQuerySet,
    AccountSortOrder,
    AccountSortSettings,
    clear_invalid_loan_pairings,
    ImportBatch,
    loan_asset_pairing_allowed,
    SavedCsvMapping,
)
from .ledger import (  # noqa: F401
    BulkEditUndo,
    Receipt,
    RefundLink,
    RefundLinkDrop,
    SavedTransactionFilter,
    Transaction,
    TransactionCorrectionHistory,
    TransactionSplit,
    TransactionTag,
    TransferPair,
)
from .recurring import (  # noqa: F401
    MONTH_STEPS,
    monthly_from_annual_minor,
    PERIODS_PER_YEAR,
    RecurringExclusion,
    RecurringPriceAcknowledgement,
    RecurringSeries,
    RecurringSeriesMember,
    WEEK_STEP_DAYS,
)
from .sync import (  # noqa: F401
    AccountLink,
    BalanceSnapshot,
    SimpleFinClaim,
    SimpleFinConnection,
)
from .planning import (  # noqa: F401
    Budget,
    BudgetAmount,
    BudgetQuerySet,
    BudgetRolloverReset,
    FundEntry,
    PlannedItem,
    SavingsGoal,
    SavingsGoalQuerySet,
)
from .rules import (  # noqa: F401
    CategoryRule,
    RuleApplication,
    RuleApplicationEntry,
)
from .ai import (  # noqa: F401
    AiChatAttachment,
    AiConversation,
    AiConversationMessage,
    AiJob,
    AiPlanLink,
    AiProposal,
    AiProviderConnection,
    AiUsageEvent,
    CategorySuggestion,
)
from .alerts import (  # noqa: F401
    Alert,
    AlertSettings,
    NTFY_TOPIC_PATTERN,
    NTFY_TOPIC_VALIDATOR,
)
from .reports import (  # noqa: F401
    BillsCalendarSettings,
    MonthlyReview,
    ReconciliationRun,
    SheetComparisonSettings,
    SheetMonthTotal,
)

# Private names callers and migrations still import from finance.models. The
# submodules expose them under public names because the import contract
# (tests/test_import_contract.py) records every cross-module `_name` import.
from .people import person_for as _person_for  # noqa: F401
from .people import PrivateOrHouseholdQuerySet as _PrivateOrHouseholdQuerySet  # noqa: F401
from .accounts import ACCOUNT_SORT_VALUES as _ACCOUNT_SORT_VALUES  # noqa: F401
from .planning import detach_draw_downs as _detach_draw_downs  # noqa: F401

# Register the separate metadata-only audit model with Django.
from ..audit_models import AuditEvent  # noqa: F401
