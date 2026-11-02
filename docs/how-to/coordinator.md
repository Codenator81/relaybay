# Coordinate many projects

!!! note
    A full guide is coming in 0.1.0. In short: one session acts as the coordinator. It creates
    projects and tasks with `project_upsert` and `task_create`, binds project sessions with
    `task_bind_session`, catches up with `events_since`, and reads each project through
    `project_snapshot`. Project sessions follow the working contract in the MCP instructions
    ([SPEC §3](../SPEC.md#spec-mcp-instructions)).
