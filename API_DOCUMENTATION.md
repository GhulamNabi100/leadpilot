# API (JSON, `Authorization: Bearer <token>`; roles: viewer < agent < admin)
POST /api/auth/register {org_name,email,password} · POST /api/auth/login {email,password} · POST /api/users (admin) {email,password,role}
GET /api/leads?search&status&min_score&sort&order&page&per_page · POST /api/leads (agent) · PATCH /api/leads/<id> (agent: status,priority,notes,assigned_to)
POST /api/leads/bulk-status (agent) {ids,status} · GET /api/leads/<id>/activity · GET /api/leads/export.csv · POST /api/leads/import (agent, text/csv or {csv})
GET /api/dashboard · POST /api/demo/seed (admin)
GET/POST /api/workflows (POST admin) · PATCH /api/workflows/<id> (admin {enabled}) · POST /api/workflows/<id>/run (agent) · GET /api/executions · POST /api/executions/<id>/retry (agent)
GET /api/notifications · POST /api/notifications/read
Errors: {"error": "..."} with 400 validation, 401 auth, 402 plan limit, 403 role, 404 not found/other org, 409 duplicate/disabled, 429 throttled.
