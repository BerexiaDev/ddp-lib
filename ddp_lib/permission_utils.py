from typing import List, Dict

from cmr_etl_lib.enums import AppModuleEnum, UserActionEnum, UserRoleEnum

PERMISSION_MATRIX: Dict[AppModuleEnum, Dict[UserActionEnum, List[UserRoleEnum]]] = {
    AppModuleEnum.USERS.value: {
        UserActionEnum.WRITE.value: [UserRoleEnum.ADMIN.value, UserRoleEnum.DATA_OWNER.value],
        UserActionEnum.READ.value: [UserRoleEnum.ADMIN.value, UserRoleEnum.DATA_OWNER.value],
    },
    AppModuleEnum.CONNECTORS.value: {
        UserActionEnum.WRITE.value: [UserRoleEnum.ADMIN.value],
        UserActionEnum.READ.value: [UserRoleEnum.ADMIN.value],
    },
    AppModuleEnum.REFERENCES.value: {
        UserActionEnum.WRITE.value: [UserRoleEnum.ADMIN.value],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.POPULATIONS.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.DATA_CLEANING.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.DOMAINS.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.SPONSOR.value
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value,
            UserRoleEnum.SPONSOR.value
        ],
    },
    AppModuleEnum.SUB_DOMAINS.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.RULES.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value
        ],
        UserActionEnum.EDIT_RULE_QUERY.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.DATA_STEWARD.value,
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.ISSUE_TRACKER.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.REMEDIATION_ACTIONS.value: {
        UserActionEnum.WRITE.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value
        ],
        UserActionEnum.READ.value: [
            UserRoleEnum.ADMIN.value,
            UserRoleEnum.DATA_OWNER.value,
            UserRoleEnum.DATA_STEWARD.value,
            UserRoleEnum.SPONSOR.value,
            UserRoleEnum.PERMANENT_CONTROL.value,
            UserRoleEnum.MANAGEMENT_CONTROL.value
        ],
    },
    AppModuleEnum.AUDIT_LOG.value: {
        UserActionEnum.WRITE.value: [UserRoleEnum.ADMIN.value],
        UserActionEnum.READ.value: [UserRoleEnum.ADMIN.value],
    },
    AppModuleEnum.DATAMART_LOGS.value: {
        UserActionEnum.WRITE.value: [UserRoleEnum.ADMIN.value],
        UserActionEnum.READ.value: [UserRoleEnum.ADMIN.value],
    }
}


def get_allowed_roles_for(module: AppModuleEnum, user_action: UserActionEnum) -> List[UserRoleEnum]:
    """
        Return the list of roles that are permitted to perform `action`
        on `module`, or an empty list if none.
    """
    return PERMISSION_MATRIX.get(module.value, {}).get(user_action.value, [])
