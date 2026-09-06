# NervOS Sessions

## Two distinct session types

NervOS uses "session" in two separate domains. Never merge the concepts.

## Authentication session

Stage A implements browser/dashboard login sessions.

Target behavior:

- cryptographically random opaque token
- token sent in HttpOnly cookie
- only token hash stored in DB
- server-side expiration/revocation
- SameSite explicitly set
- Secure flag in production/HTTPS mode
- no authentication token in localStorage/sessionStorage

## Agent conversation session

Future feature representing one ongoing interactive conversation between a user and one AgentInstance.

```text
Research Agent
  Session "Solar paper"
    - message
    - Run 1
    - message
    - Run 2

  Session "Battery research"
    - Run 3
```

## Session != Run

One conversation session can contain many runs. A Run is one execution.

Background scheduled runs may use `session_id = NULL` because they are not conversational.

## Future conversation-session fields

Likely:

- id
- user_id
- agent_instance_id
- title
- status
- created_at
- last_active_at
- archived_at

Messages likely include id, session_id, role/type, content, metadata, and created_at.

## Context strategy

Do not send unlimited historical messages to the model. Use a session summary, recent window, relevant long-term memory, and current request.

## Multi-agent note

An agent handoff does not require merging two conversation sessions. Use explicit task/handoff data or shared workspace memory.
