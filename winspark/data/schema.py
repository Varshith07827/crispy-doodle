"""Port of WinSpark.Infrastructure.Data.DatabaseInitializer.

Base schema only (schema_version 1 in this port) — the .NET app has migrated
this to version 17 with additional AI/connector/WhatsApp tables that are not
yet ported. See docs/PYTHON_PORT_NOTES.md.
"""

STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS Applications (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        ProcessName TEXT NOT NULL,
        FriendlyName TEXT NOT NULL,
        ExecutablePath TEXT NOT NULL DEFAULT '',
        FirstSeenUtc TEXT NOT NULL,
        LastSeenUtc TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS IX_Applications_ProcessName ON Applications(ProcessName)",
    """
    CREATE TABLE IF NOT EXISTS ApplicationSnapshots (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        ApplicationId INTEGER NULL,
        WindowHandle INTEGER NOT NULL,
        WindowTitle TEXT NOT NULL,
        ProcessName TEXT NOT NULL,
        ProcessId INTEGER NOT NULL,
        MemoryBytes INTEGER NOT NULL DEFAULT 0,
        CpuPercent REAL NOT NULL DEFAULT 0,
        WindowState INTEGER NOT NULL DEFAULT 0,
        IsActive INTEGER NOT NULL DEFAULT 0,
        CapturedAtUtc TEXT NOT NULL,
        FOREIGN KEY (ApplicationId) REFERENCES Applications(Id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_ApplicationSnapshots_CapturedAtUtc ON ApplicationSnapshots(CapturedAtUtc DESC)",
    "CREATE INDEX IF NOT EXISTS IX_ApplicationSnapshots_WindowHandle ON ApplicationSnapshots(WindowHandle)",
    """
    CREATE TABLE IF NOT EXISTS Events (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        EventType INTEGER NOT NULL,
        ProcessName TEXT NOT NULL,
        ProcessId INTEGER NOT NULL,
        WindowHandle INTEGER NOT NULL DEFAULT 0,
        WindowTitle TEXT NOT NULL DEFAULT '',
        Details TEXT NOT NULL DEFAULT '',
        OccurredAtUtc TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_Events_OccurredAtUtc ON Events(OccurredAtUtc DESC)",
    """
    CREATE TABLE IF NOT EXISTS Logs (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        Level TEXT NOT NULL,
        Message TEXT NOT NULL,
        Source TEXT NOT NULL DEFAULT '',
        Exception TEXT NULL,
        TimestampUtc TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_Logs_TimestampUtc ON Logs(TimestampUtc DESC)",
    """
    CREATE TABLE IF NOT EXISTS Settings (
        Key TEXT PRIMARY KEY,
        Value TEXT NOT NULL,
        UpdatedAtUtc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS AutomationRules (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        Name TEXT NOT NULL,
        Description TEXT NOT NULL DEFAULT '',
        IsEnabled INTEGER NOT NULL DEFAULT 1,
        TriggerTypeId TEXT NOT NULL,
        TriggerConfigJson TEXT NOT NULL DEFAULT '{}',
        ConditionsJson TEXT NOT NULL DEFAULT '[]',
        ActionsJson TEXT NOT NULL DEFAULT '[]',
        CreatedAtUtc TEXT NOT NULL,
        UpdatedAtUtc TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_AutomationRules_IsEnabled ON AutomationRules(IsEnabled)",
    """
    CREATE TABLE IF NOT EXISTS AutomationAuditTrail (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        RuleId INTEGER NULL,
        RuleName TEXT NOT NULL DEFAULT '',
        TriggeredBy TEXT NOT NULL DEFAULT '',
        ActionName TEXT NOT NULL,
        TargetWindowHandle INTEGER NOT NULL DEFAULT 0,
        TargetProcessName TEXT NOT NULL DEFAULT '',
        TargetWindowTitle TEXT NOT NULL DEFAULT '',
        ExecutedAtUtc TEXT NOT NULL,
        Result INTEGER NOT NULL DEFAULT 0,
        Success INTEGER NOT NULL DEFAULT 0,
        Details TEXT NOT NULL DEFAULT '',
        ErrorMessage TEXT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_AutomationAuditTrail_ExecutedAtUtc ON AutomationAuditTrail(ExecutedAtUtc DESC)",
    """
    CREATE TABLE IF NOT EXISTS Notifications (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        ApplicationName TEXT NOT NULL,
        Title TEXT NOT NULL DEFAULT '',
        Message TEXT NOT NULL DEFAULT '',
        AppUserModelId TEXT NOT NULL DEFAULT '',
        NotificationId TEXT NOT NULL,
        ReceivedAtUtc TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS IX_Notifications_NotificationId ON Notifications(NotificationId)",
    "CREATE INDEX IF NOT EXISTS IX_Notifications_ReceivedAtUtc ON Notifications(ReceivedAtUtc DESC)",
    """
    CREATE TABLE IF NOT EXISTS ApplicationProfiles (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        ProcessName TEXT NOT NULL,
        FriendlyName TEXT NOT NULL,
        ExecutablePath TEXT NOT NULL DEFAULT '',
        ProcessInformationJson TEXT NOT NULL DEFAULT '{}',
        DetectedControlsJson TEXT NOT NULL DEFAULT '[]',
        DetectedPatternsJson TEXT NOT NULL DEFAULT '[]',
        SupportedActionsJson TEXT NOT NULL DEFAULT '[]',
        AutomationCapabilitiesJson TEXT NOT NULL DEFAULT '[]',
        ProfiledAtUtc TEXT NOT NULL,
        UpdatedAtUtc TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS IX_ApplicationProfiles_ProcessName ON ApplicationProfiles(ProcessName)",
    """
    CREATE TABLE IF NOT EXISTS WhatsAppFetchBindings (
        BindingId TEXT PRIMARY KEY,
        GroupName TEXT NOT NULL,
        FetchUrl TEXT NOT NULL,
        ApiKey TEXT NOT NULL DEFAULT '',
        PollIntervalSeconds INTEGER NOT NULL DEFAULT 3,
        IsEnabled INTEGER NOT NULL DEFAULT 1,
        LastFetchUtc TEXT NULL,
        LastFetchState TEXT NOT NULL DEFAULT '',
        LastMessageReceivedUtc TEXT NULL,
        LastSendUtc TEXT NULL,
        TotalPolls INTEGER NOT NULL DEFAULT 0,
        TotalSent INTEGER NOT NULL DEFAULT 0,
        LastError TEXT NOT NULL DEFAULT '',
        ReplySource TEXT NOT NULL DEFAULT 'web',
        AiMode TEXT NOT NULL DEFAULT 'reply',
        AiPrompt TEXT NOT NULL DEFAULT '',
        TriggerText TEXT NOT NULL DEFAULT '',
        ReplyText TEXT NOT NULL DEFAULT '',
        CreatedAtUtc TEXT NOT NULL,
        UpdatedAtUtc TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS WhatsAppFetchRelayMessages (
        MessageId TEXT PRIMARY KEY,
        BindingId TEXT NOT NULL,
        ExternalId TEXT NULL,
        MessageText TEXT NOT NULL,
        ContentHash TEXT NOT NULL,
        State INTEGER NOT NULL DEFAULT 0,
        FetchUtc TEXT NOT NULL,
        SentUtc TEXT NULL,
        NextRetryUtc TEXT NULL,
        ParseStrategy TEXT NOT NULL DEFAULT '',
        LastError TEXT NOT NULL DEFAULT '',
        AttemptCount INTEGER NOT NULL DEFAULT 0,
        MemoryKey TEXT NOT NULL DEFAULT '',
        MemoryRecordedAtUtc TEXT NULL,
        FOREIGN KEY (BindingId) REFERENCES WhatsAppFetchBindings(BindingId)
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_WhatsAppFetchRelayMessages_BindingId ON WhatsAppFetchRelayMessages(BindingId)",
    "CREATE INDEX IF NOT EXISTS IX_WhatsAppFetchRelayMessages_FetchUtc ON WhatsAppFetchRelayMessages(FetchUtc DESC)",
    """
    CREATE TABLE IF NOT EXISTS WhatsAppChatMemory (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        GroupName TEXT NOT NULL,
        Role TEXT NOT NULL,
        Sender TEXT NOT NULL DEFAULT '',
        MessageText TEXT NOT NULL,
        MediaKind TEXT NOT NULL DEFAULT '',
        MediaNote TEXT NOT NULL DEFAULT '',
        MediaPath TEXT NOT NULL DEFAULT '',
        MessageTime TEXT NOT NULL DEFAULT '',
        CreatedAtUtc TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS IX_WhatsAppChatMemory_Group ON WhatsAppChatMemory(GroupName, Id)",
    """
    CREATE TABLE IF NOT EXISTS ScreenWatchers (
        WatcherId TEXT PRIMARY KEY,
        ProcessName TEXT NOT NULL,
        WindowTitleHint TEXT NOT NULL DEFAULT '',
        AppDisplayName TEXT NOT NULL DEFAULT '',
        WatchText TEXT NOT NULL,
        ActionKind TEXT NOT NULL DEFAULT 'notify',
        WhatsAppChat TEXT NOT NULL DEFAULT '',
        WhatsAppMessage TEXT NOT NULL DEFAULT '',
        PollIntervalSeconds INTEGER NOT NULL DEFAULT 10,
        IsEnabled INTEGER NOT NULL DEFAULT 1,
        Status TEXT NOT NULL DEFAULT '',
        LastError TEXT NOT NULL DEFAULT '',
        MatchedSnippet TEXT NOT NULL DEFAULT '',
        CreatedAtUtc TEXT NOT NULL,
        UpdatedAtUtc TEXT NOT NULL
    )
    """,
)

# Additive column migrations for databases created before a column existed.
# Each entry is (table, column, column-definition). Applied idempotently after
# STATEMENTS by only adding columns PRAGMA table_info reports as missing — the
# base schema above already includes them for fresh databases.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("WhatsAppFetchBindings", "ReplySource", "TEXT NOT NULL DEFAULT 'web'"),
    ("WhatsAppFetchBindings", "AiMode", "TEXT NOT NULL DEFAULT 'reply'"),
    ("WhatsAppFetchBindings", "AiPrompt", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppFetchBindings", "TriggerText", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppFetchBindings", "ReplyText", "TEXT NOT NULL DEFAULT ''"),
    # Attachment metadata for chat memory (photos/voice notes/documents the
    # accessibility tree names but can't hand us the bytes of), plus the
    # message's own timestamp label. Added for databases created before them.
    ("WhatsAppChatMemory", "MediaKind", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppChatMemory", "MediaNote", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppChatMemory", "MediaPath", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppChatMemory", "MessageTime", "TEXT NOT NULL DEFAULT ''"),
    # A delivered message is only half-recorded until it is also in the chat's
    # memory, and those are two separate writes with a gap between them: die in
    # that gap and the message is sent but never remembered. MemoryKey is the
    # chat-memory key the send resolved (stored BEFORE sending, since resolving
    # it needs WhatsApp open and a restart may not have that), and
    # MemoryRecordedAtUtc is stamped once memory actually holds it. SENT with no
    # MemoryRecordedAtUtc is exactly the set to repair.
    ("WhatsAppFetchRelayMessages", "MemoryKey", "TEXT NOT NULL DEFAULT ''"),
    ("WhatsAppFetchRelayMessages", "MemoryRecordedAtUtc", "TEXT NULL"),
)
