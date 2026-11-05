"""ORM models, split by domain. `from app.db.models import X` keeps working: every public
name of the former single module is re-exported here, and importing the package
registers every table on `Base.metadata` (Alembic relies on that)."""

from app.db.base import Base  # noqa: F401  (historical re-export: `from app.db.models import Base`)
from app.db.models.application_documents import (  # noqa: F401
    ApplicationPackageReviewRecord,
    ApplicationPackageReviewRevisionRecord,
    BewerbungDraftRecord,
    CandidateCVDraftRecord,
    CandidateJobMatchRecord,
)
from app.db.models.applications import (  # noqa: F401
    _APPLICATION_EVENT_IMMUTABILITY_DROP,
    _OPERATOR_DECISION_IMMUTABILITY_DROP,
    APPLICATION_CHECKS,
    APPLICATION_EVENT_CHECKS,
    APPLICATION_EVENT_IMMUTABILITY_DDL,
    APPLICATION_STATUS_VALUES,
    OPERATOR_DECISION_CHECKS,
    OPERATOR_DECISION_IMMUTABILITY_DDL,
    OPERATOR_DECISION_VALUES,
    ApplicationEventImmutableError,
    ApplicationEventRecord,
    ApplicationOperatorDecisionRecord,
    ApplicationRecord,
    OperatorDecisionImmutableError,
    _refuse_decision_delete,
    _refuse_decision_update,
    _refuse_event_delete,
    _refuse_event_update,
)
from app.db.models.automation import (  # noqa: F401
    AutomationMailProgressRecord,
    AutomationRunRecord,
    AutomationScheduleRecord,
)
from app.db.models.candidate_profile import (  # noqa: F401
    _CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DROP,
    CANDIDATE_PROFILE_REVISION_IMMUTABILITY_DDL,
    CandidateCertificationRecord,
    CandidateEducationRecord,
    CandidateExperienceRecord,
    CandidateJobPreferencesRecord,
    CandidateLanguageRecord,
    CandidateProfileRecord,
    CandidateProfileRevisionImmutableError,
    CandidateProfileRevisionRecord,
    CandidateProjectRecord,
    CandidateSkillRecord,
    _refuse_revision_delete,
    _refuse_revision_update,
)
from app.db.models.common import (  # noqa: F401
    _in_list,
)
from app.db.models.company_research import (  # noqa: F401
    CompanyResearchIdentityAlias,
    CompanyResearchRecord,
)
from app.db.models.email_collectors import (  # noqa: F401
    ProcessedEmailMessage,
    XingScanProgressRecord,
)
from app.db.models.follow_ups import (  # noqa: F401
    FollowUpApprovalRecord,
    FollowUpProposalRecord,
    FollowUpSendRecord,
)
from app.db.models.gmail import (  # noqa: F401
    GMAIL_MAILBOX_IDENTITY_MAX_LENGTH,
    GMAIL_THREAD_LOCK_HOLDER_MAX_LENGTH,
    GmailMessageAnalysisRecord,
    GmailMessageIdClaimRecord,
    GmailMessageRecord,
    GmailPermanentSkipRecord,
    GmailThreadRecord,
)
from app.db.models.gmail_drafts import (  # noqa: F401
    GMAIL_DRAFT_CHECKS,
    GMAIL_DRAFT_MARKER_LENGTH,
    GMAIL_DRAFT_UID_MAX,
    GmailApplicationDraftRecord,
)
from app.db.models.jobs import (  # noqa: F401
    JobCommuteCheckRecord,
    JobFitAssessmentRecord,
    JobRecord,
    JobReferenceTokenRecord,
    UserProfile,
)
from app.db.models.response_drafts import (  # noqa: F401
    LIVE_SEND_STATUS_PREDICATE,
    LIVE_SEND_STATUSES,
    ResponseDraftApprovalRecord,
    ResponseDraftRecord,
    ResponseDraftSendRecord,
)
from app.db.models.telegram import (  # noqa: F401
    LIFECYCLE_NOTIFICATION_CHECKS,
    OPERATOR_CARD_CHECKS,
    TELEGRAM_DELIVERY_STATE_VALUES,
    VACANCY_REVIEW_STATES,
    TelegramBewerbungApprovalRecord,
    TelegramBewerbungPreparationRecord,
    TelegramDigestDeliveryRecord,
    TelegramLifecycleNotificationRecord,
    TelegramOperatorCardRecord,
    TelegramVacancyReviewRecord,
)
