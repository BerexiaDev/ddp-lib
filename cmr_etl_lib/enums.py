from enum import Enum

class UserRoleEnum(Enum):
    ADMIN = 'ADMIN'
    DATA_OWNER = 'DATA_OWNER'
    DATA_STEWARD = 'DATA_STEWARD'
    SPONSOR = 'SPONSOR'
    PERMANENT_CONTROL = 'PERMANENT_CONTROL'
    MANAGEMENT_CONTROL = 'MANAGEMENT_CONTROL'

class AppModuleEnum(Enum):
    USERS = 'USERS'
    CONNECTORS = 'CONNECTORS'
    REFERENCES = 'REFERENCES'
    POPULATIONS = 'POPULATIONS'
    DOMAINS = 'DOMAINS'
    SUB_DOMAINS = 'SUB_DOMAINS'
    RULES = 'RULES'
    ISSUE_TRACKER = 'ISSUE_TRACKER'
    REMEDIATION_ACTIONS = 'REMEDIATION_ACTIONS'
    AUDIT_LOG = 'AUDIT_LOG'
    DATA_CLEANING = 'DATA_CLEANING'
    DATAMART_LOGS = 'DATAMART_LOGS'
    QUEUE = 'QUEUE'
    DATA_ENGINEERING = 'DATA_ENGINEERING'

class UserActionEnum(Enum):
    READ = 'READ'
    WRITE = 'WRITE'
    EDIT_RULE_QUERY = 'EDIT_RULE_QUERY'



class ExecutionStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"   # User requested cancellation, termination in progress
    CANCELLED = "CANCELLED"     # Successfully stopped by user
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JoinType(Enum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"

class AggregationFunction(Enum):
    NONE = ""
    COUNT = "COUNT"
    DISTINCT = "DISTINCT"
    COUNT_DISTINCT = "COUNT(DISTINCT)"
    SUM = "SUM"
    COUNT_ALL = "COUNT(*)"
    AVERAGE = "AVG"
    MINIMUM = "MIN"
    MAXIMUM = "MAX"

class QueryOperator(Enum):
    # Common operators
    EQUALS = "="
    NOT_EQUALS = "!="
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"

    # String-specific operators
    CONTAINS = "CONTAINS"
    NOT_CONTAINS = "NOT CONTAIN"
    STARTS_WITH = "STARTS WITH"
    ENDS_WITH = "ENDS WITH"
    MATCHES = "MATCHES"
    NOT_MATCHES = "NOT MATCHES"


    # Number and Date specific operators
    GREATER_THAN = ">"
    LESS_THAN = "<"
    GREATER_THAN_OR_EQUAL = ">="
    LESS_THAN_OR_EQUAL = "<="
    BETWEEN = "BETWEEN"
    NOT_BETWEEN = "NOT BETWEEN"
    IN = "IN"
    NOT_IN = "NOT IN"
    LIST_CONTAINS = 'LIST CONTAINS'
    LIST_NOT_CONTAINS = 'LIST NOT CONTAINS'


class SelectType(Enum):
    Normal = "NORMAL"
    aggregate = "AGGREGATE"

class HavingConditionType(Enum):
  FIELD = 'Field'
  COUNT_ALL = 'Count(*)'
  COUNT_COLUMN = 'Count(Column)'


class DateUnit(Enum):
    Day = 'DAY'
    Month = 'MONTH'
    Year = 'YEAR'


class ColumnType(Enum):
    String = 'string'
    Number = 'number'
    Date = 'Date'
    Boolean = 'boolean'
    Datetime = 'Datetime'
    List = 'list'
    Set = 'set'
    MultiSet = 'multiset'


class ComparisonType(Enum):
    Value = 'VALUE'   # Compare with a fixed value
    Column = 'COLUMN'  # Compare with another column


class RuleSeverityCoefficient(Enum):
    INFO = 0
    PRUDENCE = 1
    WARNING = 2
    URGENT = 3
    CRITICAL = 4


class PopulationStatus(Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED" # the population is created but no query has been set yet
    CREATING    = "CREATING"     # in the process of being created
    AVAILABLE   = "AVAILABLE"    # successfully created and ready to use
    REFRESHING  = "REFRESHING"   # materialized view is being refreshed
    FAILED      = "FAILED"       # last create/refresh attempt failed


class DataMartSchemas(Enum):
    DATA_CLEANING = "data_cleaning" # to store cleaned tables
    IMPORT_DATA = "imported_files" # to store imported data
    POPULATION = "populations"

class CleaningStatus(Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    STARTED    = "STARTED"
    COMPLETED   = "COMPLETED"
    FAILED      = "FAILED"
    
class SyncStatus(Enum):
    NOT_STARTED   = "NOT_STARTED"    # No sync has been triggered yet
    QUEUED        = "QUEUED"         # Sync is scheduled but not started
    RUNNING       = "RUNNING"        # Sync is actively in progress
    COMPLETED     = "COMPLETED"      # Sync finished successfully
    FAILED        = "FAILED"         # Sync finished with errors
    SCRIPT_ERROR  = "SCRIPT_ERROR"   # Sync aborted due to script error
    SKIPPED       = "SKIPPED"        # Sync was skipped
    CANCELLED     = "CANCELLED"      # Sync was manually stopped or aborted
    PARTIAL       = "PARTIAL"        # Sync completed partially (some tables/rows failed)


class QueuedJobAction(Enum):
    FULL_REBUILD   = "FULL_REBUILD"
    FULL_REFRESH   = "FULL_REFRESH"
    FULL_SYNC      = "FULL_SYNC"
    INCREMENTAL   = "INCREMENTAL"
    LOG_TABLE   = "LOG_TABLE"
    NO_SYNC   = "NO_SYNC"
    
class TableType(Enum):
    CLEANED_TABLE = "CLEANED TABLE"
    POPULATION = "POPULATION"
    DICTIONARY  = "DICTIONARY"
    REFERENCE     = "REFERENCE"
    
class Priority(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
