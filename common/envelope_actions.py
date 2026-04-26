"""Canonical action constants for RELAIS Envelope.action field.

Each brick that publishes to a Redis stream MUST set envelope.action
to one of these constants before calling xadd(). The action field is
self-describing (not used for routing — stream names handle routing).
"""

# Incoming messages from external channels
ACTION_MESSAGE_INCOMING = "message.incoming"

# After Portail enrichment (user resolved, llm_profile stamped)
ACTION_MESSAGE_VALIDATED = "message.validated"

# Normal message routed to Atelier by Sentinelle
ACTION_MESSAGE_TASK = "message.task"

# Slash command routed to Commandant by Sentinelle
ACTION_MESSAGE_COMMAND = "message.command"

# Atelier response, pending outgoing guardrail check by Sentinelle
ACTION_MESSAGE_OUTGOING_PENDING = "message.outgoing_pending"

# After Sentinelle outgoing guardrail — ready for delivery
ACTION_MESSAGE_OUTGOING = "message.outgoing"

# Streaming progress token emitted by Atelier
ACTION_MESSAGE_PROGRESS = "message.progress"

# Memory requests (Atelier → Souvenir, Commandant → Souvenir)
ACTION_MEMORY_ARCHIVE = "memory.archive"
ACTION_MEMORY_CLEAR = "memory.clear"
ACTION_MEMORY_FILE_WRITE = "memory.file_write"
ACTION_MEMORY_FILE_READ = "memory.file_read"
ACTION_MEMORY_FILE_LIST = "memory.file_list"
ACTION_MEMORY_SESSIONS = "memory.sessions"
ACTION_MEMORY_RESUME = "memory.resume"
ACTION_MEMORY_HISTORY_READ = "memory.history_read"

# Skill execution trace (Atelier → Forgeron)
ACTION_SKILL_TRACE = "skill.trace"

# Forgeron — automatic creation of a new skill from recurring sessions (auto-creation pipeline)
ACTION_SKILL_CREATED = "skill.created"

# Horloger — scheduled job trigger fired into the incoming channel stream
ACTION_HORLOGER_TRIGGER = "horloger.trigger"

# Atelier control operations (Commandant → Atelier via relais:atelier:control)
ACTION_ATELIER_COMPACT = "atelier.compact"
